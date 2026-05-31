from __future__ import annotations

from flask import Blueprint, jsonify, current_app, request
from ..security import (
    rate_limited,
    validate_query,
    json_error,
)
from ..search_service import group_series_results, build_packs, _dedup_by_infohash, group_by_quality

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

    # Each "round" fans out to all enabled providers CONCURRENTLY (bitsearch +
    # apibay + torrents-csv), merges and dedups by infohash. If a provider is
    # down it contributes nothing — results still come from the working ones.
    def round_search(query_text):
        return search.multi_search(query_text)

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

    # --- Normal Mode: one search round per selected quality, grouped by quality ---
    qualities = [x for x in _csv(request.args.get("quality", "1080p")) if x in ALLOWED_QUALITIES]
    if not qualities:
        qualities = ["1080p"]

    all_rows = []
    for ql in qualities:
        all_rows += round_search(f"{q} {ql}")
    all_rows = _dedup_by_infohash(all_rows)
    quality_groups = group_by_quality(all_rows)
    return jsonify({"mode": "normal_grouped", "quality_groups": quality_groups})
