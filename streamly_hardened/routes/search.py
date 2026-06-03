from __future__ import annotations

import re

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
    provider_order = ("apibay", "bitsearch", "torrents-csv") if mode == "series" else None

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
    provider_fallback = {"mode": None}

    def _relevant(r):
        info = parse_release(str(r.get("name", "")))
        # Episode => exact title match (drops spin-offs); movie/pack => prefix match.
        return matches_query(q, info["series"], is_episode=info["episode"] is not None)

    def _tokens(value):
        tokens = [t for t in re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split() if t]
        if len(tokens) > 1 and tokens[-1] in {"us", "uk", "ca", "au", "nz"}:
            tokens.pop()
        return tokens

    def _without_articles(tokens):
        return [t for t in tokens if t not in {"the", "a", "an"}]

    query_core = _without_articles(_tokens(q))

    def _series_primary_relevant(row):
        info = parse_release(str(row.get("name", "")))
        if matches_query(q, info["series"], is_episode=info["episode"] is not None):
            return True
        series_core = _without_articles(_tokens(info["series"]))
        return bool(query_core) and series_core == query_core

    def _series_loose_relevant(row):
        info = parse_release(str(row.get("name", "")))
        series_core = _without_articles(_tokens(info["series"]))
        return bool(query_core) and all(t in series_core for t in query_core)

    def round_search(query_text, extra_filter=None):
        def _combined(row):
            if not _relevant(row):
                return False
            return bool(extra_filter(row)) if extra_filter else True

        rows, winner, attempts, fallback_mode = search.multi_search_filtered(
            query_text,
            _combined,
            prefer=locked["provider"],
            strict_prefer=locked["provider"] is not None,
            allow_raw_fallback=False,
            order_override=provider_order,
        )
        provider_attempts.extend(attempts)
        if fallback_mode and provider_fallback["mode"] is None:
            provider_fallback["mode"] = fallback_mode
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

        series_less_relevant: list[dict] = []
        series_other: list[dict] = []
        main_hashes: set[str] = set()

        def _remember_series_fallback(raw_rows, primary_rows):
            primary_hashes = {str(r.get("infohash", "")).lower() for r in primary_rows}
            for row in raw_rows:
                ih = str(row.get("infohash", "")).lower()
                if ih and ih in primary_hashes:
                    continue
                info = parse_release(str(row.get("name", "")))
                if _series_loose_relevant(row):
                    series_less_relevant.append(row)
                elif not info.get("parsed") or not _series_primary_relevant(row):
                    series_other.append(row)

        def series_round_search(query_text):
            order = [locked["provider"]] if locked["provider"] else list(provider_order or search._provider_order())
            first_raw_provider = None
            first_raw_rows: list[dict] = []
            for provider in order:
                raw_rows = _dedup_by_infohash(search._run_provider(provider, query_text))
                primary_rows = [r for r in raw_rows if _series_primary_relevant(r)]
                less_count = sum(1 for r in raw_rows if r not in primary_rows and _series_loose_relevant(r))
                other_count = max(0, len(raw_rows) - len(primary_rows) - less_count)
                provider_attempts.append({
                    "provider": provider,
                    "raw": len(raw_rows),
                    "filtered": len(primary_rows),
                    "less_relevant": less_count,
                    "other": other_count,
                })
                if raw_rows and first_raw_provider is None:
                    first_raw_provider = provider
                    first_raw_rows = raw_rows
                if primary_rows:
                    if locked["provider"] is None:
                        locked["provider"] = provider
                    _remember_series_fallback(raw_rows, primary_rows)
                    return primary_rows
            if locked["provider"] is None and first_raw_provider is not None:
                locked["provider"] = first_raw_provider
                if provider_fallback["mode"] is None:
                    provider_fallback["mode"] = "other"
                _remember_series_fallback(first_raw_rows, [])
            return []

        # --- Broad: a single <title>-only query first (catches releases the
        #     narrow per-quality/encoder queries miss). Merged into both packs
        #     and episodes below. ---
        broad_rows = series_round_search(q)

        # --- Season Packs: <title> <q> x265 + <title> <q> hevc ---
        pack_rows = list(broad_rows)
        for ql in qualities:
            pack_rows += series_round_search(f"{q} {ql} x265")
            pack_rows += series_round_search(f"{q} {ql} hevc")
        pack_rows = _dedup_by_infohash(pack_rows)
        packs = build_packs(pack_rows)

        # --- Encoders: <title> <q> <ENCODER> per combination, merged w/ broad ---
        enc_rows = list(broad_rows)
        for enc in encoders:
            for ql in qualities:
                enc_rows += series_round_search(f"{q} {ql} {enc}")
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
        main_hashes = {str(r.get("infohash", "")).lower() for r in packs + enc_rows if r.get("infohash")}
        series_less_relevant = [r for r in _dedup_by_infohash(series_less_relevant) if str(r.get("infohash", "")).lower() not in main_hashes]
        less_hashes = {str(r.get("infohash", "")).lower() for r in series_less_relevant if r.get("infohash")}
        series_other = [
            r for r in _dedup_by_infohash(series_other)
            if str(r.get("infohash", "")).lower() not in main_hashes and str(r.get("infohash", "")).lower() not in less_hashes
        ]
        return jsonify({
            "mode": "series",
            "packs": packs,
            "encoders": groups["encoders"],
            "less_relevant": series_less_relevant,
            "other": series_other,
            "stats": groups["stats"],
            "requests_used": len(provider_attempts),
            "provider": locked["provider"],
            "provider_attempts": provider_attempts,
            "provider_fallback": provider_fallback["mode"],
            "qualities": qualities,
            "encoders_selected": encoders,
        })

    # --- Normal Mode: ONE broad query -> filter (quality + encoder) -> quality sections ---
    # Relevance no longer discards rows outright. The provider selection prefers
    # relevant rows, but non-relevant rows from the winning provider are shown in
    # a separate "Less relevant" section.
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

    chosen_provider = None
    matched_rows: list[dict] = []
    less_relevant_rows: list[dict] = []
    fallback_mode = None
    first_less_provider = None
    first_less_rows: list[dict] = []

    for provider in search._provider_order():
        raw_rows = _dedup_by_infohash(search._run_provider(provider, q))
        eligible = [r for r in raw_rows if _normal_filter(r)]
        relevant = [r for r in eligible if _relevant(r)]
        less = [r for r in eligible if not _relevant(r)]
        provider_attempts.append({
            "provider": provider,
            "raw": len(raw_rows),
            "eligible": len(eligible),
            "filtered": len(relevant),
            "less_relevant": len(less),
        })
        if eligible and first_less_provider is None:
            first_less_provider = provider
            first_less_rows = eligible
        if relevant:
            chosen_provider = provider
            matched_rows = relevant
            less_relevant_rows = less
            break

    if chosen_provider is None and first_less_provider is not None:
        chosen_provider = first_less_provider
        less_relevant_rows = first_less_rows
        fallback_mode = "less_relevant"

    locked["provider"] = chosen_provider
    quality_groups = group_by_quality(_dedup_by_infohash(matched_rows), only_qualities=qualities or None, cap=None)
    if less_relevant_rows:
        less_relevant_rows = _dedup_by_infohash(less_relevant_rows)
        less_relevant_rows.sort(key=lambda r: r.get("size_bytes", 0) or 0)
        quality_groups.append({
            "quality": "less_relevant",
            "label": "Less relevant",
            "count": len(less_relevant_rows),
            "rows": less_relevant_rows,
        })
    return jsonify({
        "mode": "normal_grouped",
        "quality_groups": quality_groups,
        "provider": locked["provider"],
        "provider_attempts": provider_attempts,
        "provider_fallback": fallback_mode,
    })
