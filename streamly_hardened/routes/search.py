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

    def _relevant(r):
        info = parse_release(str(r.get("name", "")))
        # Episode => exact title match (drops spin-offs); movie/pack => prefix match.
        return matches_query(q, info["series"], is_episode=info["episode"] is not None)

    def round_search(query_text):
        rows, winner = search.multi_search(query_text, prefer=locked["provider"])
        if winner and locked["provider"] is None:
            locked["provider"] = winner
        return [r for r in rows if _relevant(r)]

    # --- Series Mode v2: targeted queries (packs + per encoder×quality) ---
    if mode == "series":
        qualities = [x for x in _csv(request.args.get("quality", "1080p")) if x in ALLOWED_QUALITIES]
        if not qualities:
            qualities = ["1080p"]
        encoders = [e for e in _csv(request.args.get("encoders", "")) if e in PRESET_ENCODERS]

        # Quota guard: count planned rounds = packs(2 per quality) + encoders(N*Q).
        planned = (2 * len(qualities)) + (len(encoders) * len(qualities))
        if planned > SERIES_MAX_REQUESTS:
            return json_error(
                400, "too_many_requests",
                f"This selection needs {planned} searches (limit {SERIES_MAX_REQUESTS}). "
                "Reduce the number of qualities or encoders.",
            )

        used = 0

        # --- Season Packs: <title> <q> x265 + <title> <q> hevc ---
        pack_rows = []
        for ql in qualities:
            pack_rows += round_search(f"{q} {ql} x265"); used += 1
            pack_rows += round_search(f"{q} {ql} hevc"); used += 1
        pack_rows = _dedup_by_infohash(pack_rows)
        packs = build_packs(pack_rows)

        # --- Encoders: <title> <q> <ENCODER> per combination ---
        enc_rows = []
        for enc in encoders:
            for ql in qualities:
                enc_rows += round_search(f"{q} {ql} {enc}"); used += 1
        enc_rows = _dedup_by_infohash(enc_rows)

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
            "requests_used": used,
            "qualities": qualities,
            "encoders_selected": encoders,
        })

    # --- Normal Mode: ONE broad query -> filter (quality + encoder) -> quality sections ---
    # 1) Single broad search for the title (gets the provider's full result set,
    #    relevance-filtered + deduped inside round_search). No per-quality queries.
    all_rows = _dedup_by_infohash(round_search(q))

    # 2) Encoder filter: keep only the ticked release groups (none ticked => all).
    selected_encoders = {
        e for e in (_normalize_encoder(x) for x in _csv(request.args.get("encoders", "")))
        if e
    }
    if selected_encoders:
        all_rows = [r for r in all_rows if r.get("encoder_norm", "") in selected_encoders]

    # 3) Quality filter = which sections to show (none ticked => all sections,
    #    incl. Other). Within each section: size-ascending, no cap (keep all).
    qualities = [x for x in _csv(request.args.get("quality", "")) if x in ALLOWED_QUALITIES]
    quality_groups = group_by_quality(all_rows, only_qualities=qualities or None, cap=None)
    return jsonify({"mode": "normal_grouped", "quality_groups": quality_groups})
