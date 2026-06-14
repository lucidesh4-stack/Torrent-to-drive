from __future__ import annotations

from flask import Blueprint, jsonify, current_app, request
from ..auth_utils import current_client
from ..security import (
    csrf_required,
    rate_limited,
    require_json_body,
    validate_item_type,
    validate_positive_int,
    validate_magnet,
)
from ..cloud_service import format_size, _safe_int

cloud_bp = Blueprint("cloud", __name__)

@cloud_bp.get("/fs/folder/<folder_id>/items")
@rate_limited(cost=1.0)
def list_items(folder_id: str):
    config = current_app.config
    folder = validate_positive_int(folder_id, name="folder_id", maximum=config.get("max_folder_id", 1_000_000_000))
    cloud = getattr(current_app, "cloud", None)
    try:
        data = cloud.list_items(current_client(), folder)
    except (ConnectionError, TimeoutError) as e:
        current_app.logger.warning("Provider error on list: %s", e)
        from ..security import json_error
        return json_error(502, "provider_error", "Provider unavailable or failed to list items")
    for item in data["folders"] + data["files"]:
        item["size_str"] = format_size(item["size"])
    for transfer in data.get("transfers", []):
        transfer["size_str"] = format_size(transfer.get("size", 0))
        transfer["download_rate_str"] = format_size(transfer.get("download_rate", 0)) + "/s"
        
    # Inject queue items directly for root folder to prevent client-side timing lags / vanishing queue list
    if folder == 0:
        rs = getattr(current_app, "rs", None)
        queue_items = []
        if rs:
            try:
                import json as _json
                raw_items = rs._execute("LRANGE", "streamly:seedr_queue", "0", "-1") or []
                for raw in raw_items:
                    try:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")
                        queue_items.append(_json.loads(raw))
                    except Exception:
                        pass
            except Exception as q_err:
                current_app.logger.warning("Failed to fetch local queue for list_items: %s", q_err)
        data["queue"] = queue_items
        
    return jsonify(data)


@cloud_bp.post("/api/transfer/cancel")
@rate_limited(cost=2.0)
@csrf_required
def cancel_transfer():
    config = current_app.config
    data = require_json_body(config)
    transfer_id = validate_positive_int(data.get("id"), name="id", maximum=config.get("max_file_id", 1_000_000_000))
    cloud = getattr(current_app, "cloud", None)
    try:
        cloud.delete_transfer(current_client(), transfer_id)
    except (ConnectionError, TimeoutError) as e:
        current_app.logger.warning("Provider error on transfer cancel: %s", e)
        from ..security import json_error
        return json_error(502, "provider_error", "Provider rejected the cancel request or is unavailable")
    return jsonify({"success": True})

@cloud_bp.post("/api/delete")
@rate_limited(cost=2.0)
@csrf_required
def delete_item():
    config = current_app.config
    data = require_json_body(config)
    item_type = validate_item_type(data.get("type"))
    item_id = validate_positive_int(data.get("id"), name="id", maximum=config.get("max_file_id", 1_000_000_000))
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
    item_id = validate_positive_int(data.get("id"), name="id", maximum=config.get("max_file_id", 1_000_000_000))
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
            item_id = validate_positive_int(item.get("id"), name="id", maximum=config.get("max_file_id", 1_000_000_000))
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
        item_id = validate_positive_int(item.get("id"), name="id", maximum=config.get("max_file_id", 1_000_000_000))
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
    rs = getattr(current_app, "rs", None)
    config = current_app.config
    data = require_json_body(config)
    magnet = validate_magnet(data.get("magnet"), config)
    raw_size = data.get("size")
    size_bytes = _safe_int(raw_size) if raw_size is not None else 0
    name = data.get("name")
    
    # Try parsing torrent name from 'dn' parameter in magnet link
    if not name:
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(magnet)
            qs = parse_qs(parsed.query)
            dn = qs.get("dn")
            if dn:
                name = dn[0]
        except Exception:
            pass
    if not name:
        name = "Unknown Magnet"
        
    # Always write to backend history first
    from .queue import add_to_history_backend
    add_to_history_backend(rs, magnet, name, size_bytes)
    
    # Reject files over 4.5 GB limit immediately
    if size_bytes > 4.5 * 1024 * 1024 * 1024:
        from ..security import json_error
        return json_error(
            400,
            "payload_too_large",
            "File exceeds 4.5 GB limit and cannot be downloaded."
        )
        
    cloud = getattr(current_app, "cloud", None)
    
    # Check if we should queue the item
    should_queue = False
    
    # 1. If there is a local queue in Redis, we must always queue to preserve order
    try:
        if rs:
            q_len = rs._execute("LLEN", "streamly:seedr_queue")
            if q_len and int(q_len) > 0:
                should_queue = True
    except Exception as e:
        current_app.logger.error("Queue check failure before adding: %s", e)

    # 2. Check if a Seedr addition is currently locked/in-progress
    try:
        if not should_queue and rs:
            adding_locked = rs._execute("GET", "streamly:seedr_adding_lock")
            if adding_locked:
                should_queue = True
    except Exception as e:
        current_app.logger.error("Adding lock check failure: %s", e)

    # 3. Check Seedr active downloads and space quota
    if not should_queue:
        try:
            storage = cloud.list_items(current_client(), 0)
            used = max(0, _safe_int(storage.get("used")))
            maximum = max(1, _safe_int(storage.get("max")))
            transfers = storage.get("transfers", [])
            
            if len(transfers) > 0:
                should_queue = True
            elif size_bytes > 0 and (used + size_bytes > maximum):
                should_queue = True
        except Exception as e:
            current_app.logger.error("Storage check failure before adding: %s", e)

    # 4. Attempt to acquire the adding lock if we plan to add directly
    if not should_queue and rs:
        try:
            # Lock for 10 seconds to throttle concurrent adds
            acquired = rs._execute("SET", "streamly:seedr_adding_lock", "1", "EX", "10", "NX")
            if acquired != "OK":
                should_queue = True
        except Exception as e:
            current_app.logger.error("Failed to acquire seedr adding lock: %s", e)

    if should_queue:
        if rs:
            import uuid
            import time
            import json as _json
            queued_item = {
                "task_id": str(uuid.uuid4())[:8],
                "magnet": magnet,
                "name": name,
                "size": size_bytes,
                "time": int(time.time())
            }
            rs._execute("RPUSH", "streamly:seedr_queue", _json.dumps(queued_item))
            return jsonify({"success": True, "queued": True})
        else:
            from ..security import json_error
            return json_error(503, "redis_unavailable", "Redis is required for queue storage")
            
    try:
        cloud.add_magnet(current_client(), magnet)
    except Exception as e:
        # Fallback: if adding directly to Seedr fails (transient error, rate limit, quota rejection, etc.),
        # do not reject; automatically queue it instead!
        current_app.logger.warning("Direct Seedr addition failed for '%s': %s. Falling back to local queue.", name, e)
        if rs:
            import uuid
            import time
            import json as _json
            queued_item = {
                "task_id": str(uuid.uuid4())[:8],
                "magnet": magnet,
                "name": name,
                "size": size_bytes,
                "time": int(time.time())
            }
            rs._execute("RPUSH", "streamly:seedr_queue", _json.dumps(queued_item))
            # Clean up adding lock since it failed
            try:
                rs._execute("DEL", "streamly:seedr_adding_lock")
            except Exception:
                pass
            return jsonify({"success": True, "queued": True, "fallback": True})
        else:
            from ..security import json_error
            msg = str(e) or "Provider rejected the request and Redis fallback is unavailable"
            return json_error(502, "provider_error", msg)
            
    return jsonify({"success": True})

@cloud_bp.get("/api/url")
@rate_limited(cost=1.0)
def get_url():
    config = current_app.config
    file_id = validate_positive_int(request.args.get("file_id"), name="file_id", maximum=config.get("max_file_id", 1_000_000_000))
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
