from __future__ import annotations

from flask import Blueprint, jsonify, current_app, request
from ..security import (
    rate_limited,
    validate_query,
    validate_category,
    validate_sort,
    validate_order,
)
from ..cloud_service import format_size, _safe_int
from ..search_service import group_series_results, build_packs, _dedup_by_infohash, group_by_quality

search_bp = Blueprint("search", __name__)

ALLOWED_QUALITIES = ["2160p", "1080p", "720p"]   # 4K / 1080p / 720p
PRESET_ENCODERS = ["ELiTE", "PSA", "MeGusta"]
SERIES_MAX_REQUESTS = 12   # quota guard: hard ceiling on bitsearch calls per series search

_CATEGORY_LABELS = {
    "1": "Other", "2": "Movies", "3": "TV Shows", "4": "Anime", "5": "Software",
    "6": "Games", "7": "Music", "8": "Audiobooks", "9": "Ebooks", "10": "Adult",
}


def _normalize_rows(raw_items):
    """Convert raw bitsearch items into the canonical row shape used by the UI."""
    rows = []
    for item in raw_items:
        infohash = str(item.get("infohash", ""))[:128]
        title = str(item.get("title", ""))[:512]
        if not infohash or not title:
            continue
        raw_category = str(item.get("category", "Other"))[:64]
        raw_size = item.get("size")
        rows.append(
            {
                "name": title,
                "size": format_size(_safe_int(raw_size)),
                "size_bytes": max(0, _safe_int(raw_size)),
                "seeds": int(item.get("seeders", 0) or 0),
                "leeches": int(item.get("leecher", 0) or 0),
                "date": str(item.get("createdAt", "")).split("T")[0][:32],
                "category": _CATEGORY_LABELS.get(raw_category, raw_category or "Other"),
                "magnet": f"magnet:?xt=urn:btih:{infohash}&dn={title}",
                "infohash": infohash,
            }
        )
    return rows


def _csv(value):
    """Split a comma-separated query param into a clean, de-duplicated list."""
    out = []
    for part in str(value or "").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def _extract_items(raw_payload, page):
    """Pull (items, pagination, took) out of a bitsearch payload (dict or list)."""
    if isinstance(raw_payload, dict):
        items = raw_payload.get("results", []) if isinstance(raw_payload.get("results", []), list) else []
        pagination = raw_payload.get("pagination", {}) if isinstance(raw_payload.get("pagination", {}), dict) else {}
        took = raw_payload.get("took")
    else:
        items = raw_payload if isinstance(raw_payload, list) else []
        pagination = {"page": page, "perPage": len(items), "total": len(items), "totalPages": 1, "hasNext": False, "hasPrev": page > 1}
        took = None
    return items, pagination, took

@search_bp.get("/api/suggest")
@rate_limited(cost=0.5)
def suggest():
    config = current_app.config
    q = validate_query(request.args.get("q"), config)
    search = getattr(current_app, "search", None)
    return jsonify(search.imdb_suggestions(q))

@search_bp.get("/api/search")
@rate_limited(cost=1.0)
def search_route():
    config = current_app.config
    q = validate_query(request.args.get("q"), config)
    category = validate_category(request.args.get("category"), config)
    sort = validate_sort(request.args.get("sort"), config)
    order = validate_order(request.args.get("order"), config)
    page = validate_positive_int_local(request.args.get("page", 1), maximum=10_000)
    page = max(1, page)
    # Dedup defaults ON; client sends dedup=0 to disable. Absent param ⇒ True.
    dedup = request.args.get("dedup", "1").strip().lower() not in ("0", "false", "no", "off")
    mode = request.args.get("mode", "").strip().lower()

    search = getattr(current_app, "search", None)

    # --- Series Mode v2: targeted queries (packs + per encoder×quality) ---
    if mode == "series":
        # Parse & sanitize quality (multi) and encoder (multi) selections.
        qualities = [x for x in _csv(request.args.get("quality", "1080p")) if x in ALLOWED_QUALITIES]
        if not qualities:
            qualities = ["1080p"]
        encoders = [e for e in _csv(request.args.get("encoders", "")) if e in PRESET_ENCODERS]

        # Quota guard: count planned requests = packs(2 per quality) + encoders(N*Q).
        planned = (2 * len(qualities)) + (len(encoders) * len(qualities))
        if planned > SERIES_MAX_REQUESTS:
            from ..security import json_error
            return json_error(
                400, "too_many_requests",
                f"This selection needs {planned} searches (limit {SERIES_MAX_REQUESTS}). "
                "Reduce the number of qualities or encoders.",
            )

        def run(query_text, srt="seeders", ordr="desc"):
            try:
                payload = search.bitsearch(query_text, category, srt, ordr, 1, dedup=False)
            except TypeError:
                payload = search.bitsearch(query_text, category, srt, ordr)
            items, _, _ = _extract_items(payload, 1)
            return _normalize_rows(items)

        used = 0

        # --- Season Packs: <title> <q> x265 + <title> <q> hevc, sort size desc ---
        pack_rows = []
        for ql in qualities:
            pack_rows += run(f"{q} {ql} x265", "size", "desc"); used += 1
            pack_rows += run(f"{q} {ql} hevc", "size", "desc"); used += 1
        pack_rows = _dedup_by_infohash(pack_rows)
        packs = build_packs(pack_rows)

        # --- Encoders: <title> <q> <ENCODER> per combination ---
        enc_rows = []
        for enc in encoders:
            for ql in qualities:
                enc_rows += run(f"{q} {ql} {enc}"); used += 1
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

    # --- Normal Mode: one bitsearch per selected quality, grouped by quality ---
    qualities = [x for x in _csv(request.args.get("quality", "1080p")) if x in ALLOWED_QUALITIES]
    if not qualities:
        qualities = ["1080p"]

    def run_normal(query_text):
        # Normal mode fetches by seeders (most-seeded/relevant 50 per quality);
        # the UI displays them grouped by quality, size-ascending by default.
        try:
            payload = search.bitsearch(query_text, category, "seeders", "desc", 1, dedup=False)
        except TypeError:
            payload = search.bitsearch(query_text, category, "seeders", "desc")
        items, _, _ = _extract_items(payload, 1)
        return _normalize_rows(items)

    all_rows = []
    for ql in qualities:
        all_rows += run_normal(f"{q} {ql}")
    all_rows = _dedup_by_infohash(all_rows)
    quality_groups = group_by_quality(all_rows)
    return jsonify({"mode": "normal_grouped", "quality_groups": quality_groups})

def validate_positive_int_local(value: Any, *, name: str = "value", maximum: int = 10_000) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        from ..security import ValidationError
        raise ValidationError(f"{name} must be an integer") from None
    if parsed < 0 or parsed > maximum:
        from ..security import ValidationError
        raise ValidationError(f"{name} out of range")
    return parsed
