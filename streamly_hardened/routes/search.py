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

search_bp = Blueprint("search", __name__)

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

    search = getattr(current_app, "search", None)
    try:
        raw_payload = search.bitsearch(q, category, sort, order, page, dedup=dedup)
    except TypeError:
        raw_payload = search.bitsearch(q, category, sort, order)
        
    if isinstance(raw_payload, dict):
        raw_items = raw_payload.get("results", []) if isinstance(raw_payload.get("results", []), list) else []
        pagination = raw_payload.get("pagination", {}) if isinstance(raw_payload.get("pagination", {}), dict) else {}
        took = raw_payload.get("took")
    else:
        raw_items = raw_payload if isinstance(raw_payload, list) else []
        pagination = {"page": page, "perPage": len(raw_items), "total": len(raw_items), "totalPages": 1, "hasNext": False, "hasPrev": page > 1}
        took = None
        
    category_labels = {"1": "Other", "2": "Movies", "3": "TV Shows", "4": "Anime", "5": "Software", "6": "Games", "7": "Music", "8": "Audiobooks", "9": "Ebooks", "10": "Adult"}
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
                "category": category_labels.get(raw_category, raw_category or "Other"),
                "magnet": f"magnet:?xt=urn:btih:{infohash}&dn={title}",
            }
        )
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
