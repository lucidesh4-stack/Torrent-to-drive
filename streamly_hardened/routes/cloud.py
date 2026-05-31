from __future__ import annotations

from flask import Blueprint, jsonify, current_app
from ..auth_utils import current_client
from ..security import (
    csrf_required,
    rate_limited,
    require_json_body,
    validate_item_type,
    validate_positive_int,
    validate_magnet,
)
from ..cloud_service import CloudService, format_size, _safe_int

cloud_bp = Blueprint("cloud", __name__)

@cloud_bp.get("/fs/folder/<folder_id>/items")
@rate_limited(cost=1.0)
def list_items(folder_id: str):
    config = current_app.config
    folder = validate_positive_int(folder_id, name="folder_id", maximum=config.max_folder_id)
    cloud = getattr(current_app, "cloud", None)
    try:
        data = cloud.list_items(current_client(), folder)
    except (ConnectionError, TimeoutError) as e:
        current_app.logger.warning("Provider error on list: %s", e)
        from ..security import json_error
        return json_error(502, "provider_error", "Provider unavailable or failed to list items")
    for item in data["folders"] + data["files"]:
        item["size_str"] = format_size(item["size"])
    return jsonify(data)

@cloud_bp.post("/api/delete")
@rate_limited(cost=2.0)
@csrf_required
def delete_item():
    config = current_app.config
    data = require_json_body(config)
    item_type = validate_item_type(data.get("type"))
    item_id = validate_positive_int(data.get("id"), name="id", maximum=config.max_file_id)
    cloud = getattr(current_app, "cloud", None)
    try:
        cloud.delete_item(current_client(), item_type, item_id)
    except (ConnectionError, TimeoutError) as e:
        current_app.logger.warning("Provider error on delete: %s", e)
        from ..security import json_error
        return json_error(502, "provider_error", "Provider rejected the request or is unavailable")
    return jsonify({"success": True})

@cloud_bp.post("/api/zip")
@rate_limited(cost=2.0)
@csrf_required
def zip_item():
    config = current_app.config
    data = require_json_body(config)
    item_type = validate_item_type(data.get("type"))
    item_id = validate_positive_int(data.get("id"), name="id", maximum=config.max_file_id)
    cloud = getattr(current_app, "cloud", None)
    try:
        url = cloud.get_zip_url(current_client(), item_type, item_id)
    except (ConnectionError, TimeoutError) as e:
        current_app.logger.warning("Provider error on zip: %s", e)
        from ..security import json_error
        return json_error(502, "provider_error", "Failed to create zip — provider unavailable")
    return jsonify({"success": bool(url), "url": url})

@cloud_bp.post("/api/delete/bulk")
@rate_limited(cost=3.0)
@csrf_required
def delete_bulk():
    config = current_app.config
    data = require_json_body(config)
    items = data.get("items")
    if not isinstance(items, list) or not items:
        from ..security import json_error
        return json_error(400, "bad_request", "items must be a non-empty list")
    if len(items) > 100:
        from ..security import json_error
        return json_error(400, "bad_request", "Too many items (max 100)")
    
    cloud = getattr(current_app, "cloud", None)
    client = current_client()
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            item_type = validate_item_type(item.get("type"))
            item_id = validate_positive_int(item.get("id"), name="id", maximum=config.max_file_id)
            cloud.delete_item(client, item_type, item_id)
            results.append({"id": item_id, "type": item_type, "ok": True})
        except (ConnectionError, TimeoutError) as exc:
            current_app.logger.warning("Bulk delete item failed: %s", exc)
            results.append({"id": item.get("id"), "type": item.get("type"), "ok": False, "error": "Provider unavailable"})
        except ValidationError as exc:
            results.append({"id": item.get("id"), "type": item.get("type"), "ok": False, "error": "Invalid item data"})
        except Exception as exc:
            current_app.logger.exception("Unexpected error during bulk delete item: %s", exc)
            results.append({"id": item.get("id"), "type": item.get("type"), "ok": False, "error": "Internal error occurred"})
    return jsonify({"success": True, "results": results})

@cloud_bp.post("/api/zip/bulk")
@rate_limited(cost=3.0)
@csrf_required
def zip_bulk():
    config = current_app.config
    data = require_json_body(config)
    items = data.get("items")
    if not isinstance(items, list) or not items:
        from ..security import json_error
        return json_error(400, "bad_request", "items must be a non-empty list")
    if len(items) > 100:
        from ..security import json_error
        return json_error(400, "bad_request", "Too many items (max 100)")
    
    validated = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = validate_item_type(item.get("type"))
        item_id = validate_positive_int(item.get("id"), name="id", maximum=config.max_file_id)
        validated.append({"type": item_type, "id": item_id})
    
    if not validated:
        from ..security import json_error
        return json_error(400, "bad_request", "No valid items")
    
    cloud = getattr(current_app, "cloud", None)
    try:
        url = cloud.get_zip_url_bulk(current_client(), validated)
    except (ConnectionError, TimeoutError) as e:
        current_app.logger.warning("Bulk zip failed: %s", e)
        from ..security import json_error
        return json_error(502, "provider_error", "Failed to create zip — provider unavailable")
    return jsonify({"success": bool(url), "url": url})

@cloud_bp.post("/api/add")
@rate_limited(cost=2.0)
@csrf_required
def add_magnet():
    config = current_app.config
    data = require_json_body(config)
    magnet = validate_magnet(data.get("magnet"), config)
    raw_size = data.get("size")
    
    cloud = getattr(current_app, "cloud", None)
    if raw_size is not None:
        size_bytes = _safe_int(raw_size)
        if size_bytes > 0:
            try:
                storage = cloud.list_items(current_client(), 0)
                used = max(0, _safe_int(storage.get("used")))
                maximum = max(1, _safe_int(storage.get("max")))
                if used + size_bytes > maximum:
                    from ..security import json_error
                    return json_error(
                        400,
                        "storage_full",
                        "Not enough space. Clear some files from Seedr before adding."
                    )
            except (ConnectionError, TimeoutError) as e:
                current_app.logger.warning("Storage check failed before add, proceeding anyway: %s", e)
    try:
        cloud.add_magnet(current_client(), magnet)
    except (ConnectionError, TimeoutError) as e:
        current_app.logger.warning("Provider error on add: %s", e)
        from ..security import json_error
        return json_error(502, "provider_error", "Provider rejected the request (e.g. storage full) or is unavailable")
    return jsonify({"success": True})

@cloud_bp.get("/api/url")
@rate_limited(cost=1.0)
def get_url():
    config = current_app.config
    file_id = validate_positive_int(current_app.request.args.get("file_id"), name="file_id", maximum=config.max_file_id)
    cloud = getattr(current_app, "cloud", None)
    try:
        url = cloud.get_stream_url(current_client(), file_id)
    except (ConnectionError, TimeoutError) as e:
        current_app.logger.warning("Provider error on get_url: %s", e)
        from ..security import json_error
        return json_error(502, "provider_error", "Failed to get stream URL — provider unavailable")
    if not url:
        from ..security import json_error
        return json_error(404, "not_found", "Stream URL not available for this file")
    return jsonify({"success": True, "url": url})
