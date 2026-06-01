from __future__ import annotations

from flask import Blueprint, jsonify, current_app, request
from ..security import (
    rate_limited,
    validate_query,
    json_error,
)
from ..search_service import (
    group_series_results,
    build_packs,
    _dedup_by_infohash,
    group_by_quality,
    parse_release,
    matches_query,
    _normalize_encoder,
    _quality_bucket,
)

search_bp = Blueprint("search", __name__)

ALLOWED_QUALITIES = ["2160p", "1080p", "720p"]   # 4K / 1080p / 720p
PRESET_ENCODERS = ["ELiTE", "PSA", "MeGusta"]
SERIES_MAX_REQUESTS = 12   # quota guard: hard ceiling on search rounds per series search


def _csv(value):
    """Split a comma-separated query param into a clean, de-duplicated list."""
    out = []
    for part in str(value or "").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


@search_bp.get("/api/suggest")
@rate_limited(cost=0.5)
def suggest():
    config = current_app.config
    q = validate_query(request.args.get("q"), config)
    search = getattr(current_app, "search", None)
    if search is None:
        return json_error(503, "search_unavailable", "Search service is not available")
    return jsonify(search.imdb_suggestions(q))


@search_bp.get("/api/search")
@rate_limited(cost=1.0)
def search_route():
    config = current_app.config
    q = validate_query(request.args.get("q"), config)
    # Category removed: results are merged from multiple providers with differing
    # category schemes, so a single category filter is no longer meaningful.
    mode = request.args.get("mode", "").strip().lower()

    search = getattr(current_app, "search", None)
    if search is None:
        return json_error(503, "search_unavailable", "Search service is not available")

    # FAILOVER, not merge: each "round" uses the FIRST provider (in priority
    # order) that returns results — so a search draws from ONE source and we
    # avoid cross-source duplicates. `_locked` pins the winning provider for the
    # rest of THIS request (important for multi-round Series searches) so the
    # whole result set stays on a single, consistent source.
    # Results are then filtered for relevance: a row is kept only if every word
    # of the user's query `q` appears in the parsed series name. This drops the
    # unrelated junk providers return for loose substring matches
    # (e.g. searching "Daredevil" must not surface "Bones" / "The Red Green Show").
    locked = {"provider": None}
    provider_attempts: list[dict] = []

    def _relevant(r):
        info = parse_release(str(r.get("name", "")))
        # Episode => exact title match (drops spin-offs); movie/pack => prefix match.
        return matches_query(q, info["series"], is_episode=info["episode"] is not None)

    def round_search(query_text, extra_filter=None):
        def _combined(row):
            if not _relevant(row):
                return False
            return bool(extra_filter(row)) if extra_filter else True

        rows, winner, attempts = search.multi_search_filtered(
            query_text,
            _combined,
            prefer=locked["provider"],
            strict_prefer=locked["provider"] is not None,
        )
        provider_attempts.extend(attempts)
        if winner and locked["provider"] is None:
            locked["provider"] = winner
        return rows

    # --- Series Mode v2: targeted queries (packs + per encoder×quality) ---
    if mode == "series":
        qualities = [x for x in _csv(request.args.get("quality", "1080p")) if x in ALLOWED_QUALITIES]
        if not qualities:
            qualities = ["1080p"]
        encoders = [e for e in _csv(request.args.get("encoders", "")) if e in PRESET_ENCODERS]

        # Quota guard: planned rounds = 1 broad <title> + packs(2 per quality)
        # + encoders(N*Q). The broad query is counted.
        planned = 1 + (2 * len(qualities)) + (len(encoders) * len(qualities))
        if planned > SERIES_MAX_REQUESTS:
            return json_error(
                400, "too_many_requests",
                f"This selection needs {planned} searches (limit {SERIES_MAX_REQUESTS}). "
                "Reduce the number of qualities or encoders.",
            )

        # --- Broad: a single <title>-only query first (catches releases the
        #     narrow per-quality/encoder queries miss). Merged into both packs
        #     and episodes below. ---
        broad_rows = round_search(q)

        # --- Season Packs: <title> <q> x265 + <title> <q> hevc ---
        pack_rows = list(broad_rows)
        for ql in qualities:
            pack_rows += round_search(f"{q} {ql} x265")
            pack_rows += round_search(f"{q} {ql} hevc")
        pack_rows = _dedup_by_infohash(pack_rows)
        packs = build_packs(pack_rows)

        # --- Encoders: <title> <q> <ENCODER> per combination, merged w/ broad ---
        enc_rows = list(broad_rows)
        for enc in encoders:
            for ql in qualities:
                enc_rows += round_search(f"{q} {ql} {enc}")
        enc_rows = _dedup_by_infohash(enc_rows)

        # Encoder filter: the broad query returns ALL release groups; if the user
        # ticked specific encoders, keep only those (case-insensitive). None
        # ticked => keep every encoder found.
        selected_enc = {_normalize_encoder(e) for e in encoders}
        if selected_enc:
            enc_rows = [r for r in enc_rows if r.get("encoder_norm", "") in selected_enc]

        # Any qualifying packs found in encoder results, not already listed,
        # replace the largest in the top-N (list is smallest-first).
        existing = {p.get("infohash") for p in packs}
        extra_packs = [p for p in build_packs(enc_rows, top_n=10_000) if p.get("infohash") not in existing]
        for ep in extra_packs:
            if len(packs) < 20:
                packs.append(ep)
            elif (ep.get("size_bytes", 0) or 0) < (packs[-1].get("size_bytes", 0) or 0):
                packs[-1] = ep
            packs.sort(key=lambda r: r.get("size_bytes", 0) or 0)
        packs = packs[:20]

        groups = group_series_results(enc_rows)
        return jsonify({
            "mode": "series",
            "packs": packs,
            "encoders": groups["encoders"],
            "stats": groups["stats"],
            "requests_used": len(provider_attempts),
            "provider": locked["provider"],
            "provider_attempts": provider_attempts,
            "qualities": qualities,
            "encoders_selected": encoders,
        })

    # --- Normal Mode: ONE broad query -> filter (quality + encoder) -> quality sections ---
    # Provider fallback is decided AFTER relevance/quality/encoder filters. If
    # apibay returns raw rows but all are filtered out, bitsearch is tried, then
    # torrents-csv.
    selected_encoders = {
        e for e in (_normalize_encoder(x) for x in _csv(request.args.get("encoders", "")))
        if e
    }
    qualities = [x for x in _csv(request.args.get("quality", "")) if x in ALLOWED_QUALITIES]
    wanted_qualities = set(qualities)

    def _normal_filter(row):
        if selected_encoders and row.get("encoder_norm", "") not in selected_encoders:
            return False
        if wanted_qualities and _quality_bucket(str(row.get("name", ""))) not in wanted_qualities:
            return False
        return True

    all_rows = _dedup_by_infohash(round_search(q, _normal_filter))
    quality_groups = group_by_quality(all_rows, only_qualities=qualities or None, cap=None)
    return jsonify({
        "mode": "normal_grouped",
        "quality_groups": quality_groups,
        "provider": locked["provider"],
        "provider_attempts": provider_attempts,
    })
