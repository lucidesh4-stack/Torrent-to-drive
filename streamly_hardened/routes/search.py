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
from ..search_service import group_series_results

search_bp = Blueprint("search", __name__)

SERIES_PAGES = 3  # pages fetched in Series Mode (quota-friendly)

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

    # --- Series Mode: fetch a few pages, dedup, group encoder→quality→season→ep ---
    if mode == "series":
        all_items = []
        for p in range(1, SERIES_PAGES + 1):
            try:
                payload = search.bitsearch(q, category, sort, order, p, dedup=False)
            except TypeError:
                payload = search.bitsearch(q, category, sort, order)
            items, _, _ = _extract_items(payload, p)
            all_items.extend(items)
        rows = _normalize_rows(all_items)
        if dedup:
            from ..search_service import _dedup_by_infohash
            rows = _dedup_by_infohash(rows)
        groups = group_series_results(rows)
        return jsonify({"mode": "series", "groups": groups, "pages_fetched": SERIES_PAGES})

    # --- Normal Mode (unchanged behavior) ---
    try:
        raw_payload = search.bitsearch(q, category, sort, order, page, dedup=dedup)
    except TypeError:
        raw_payload = search.bitsearch(q, category, sort, order)

    raw_items, pagination, took = _extract_items(raw_payload, page)
    rows = _normalize_rows(raw_items)
    return jsonify({"results": rows, "pagination": pagination, "took": took})

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
