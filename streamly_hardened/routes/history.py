from __future__ import annotations

import time
from flask import Blueprint, jsonify, current_app, request
from ..security import (
    csrf_required,
    rate_limited,
    require_json_body,
    validate_magnet,
)

history_bp = Blueprint("history", __name__)

@history_bp.get("/api/history")
@rate_limited(cost=1.0)
def get_history():
    rs = getattr(current_app, "rs", None)
    try:
        items = rs.get_history("global_history") if rs else []
        return jsonify({"success": True, "items": items})
    except (ConnectionError, TimeoutError) as e:
        current_app.logger.warning("Redis error on get_history: %s", e)
        return jsonify({"success": True, "items": [], "_warning": "History temporarily unavailable"})

@history_bp.post("/api/history/add")
@csrf_required
def add_history():
    config = current_app.config
    data = require_json_body(config)
    magnet = validate_magnet(data.get("magnet"), config)
    name = data.get("name") if data.get("name") else "Unknown Magnet"
    if isinstance(name, str):
        name = name[:512]
    else:
        name = "Unknown Magnet"
        
    new_item = {
        "magnet": magnet,
        "title": name,
        "time": time.strftime("%d/%m/%Y, %H:%M:%S")
    }
    rs = getattr(current_app, "rs", None)
    items = rs.get_history("global_history") if rs else []
    items = [it for it in items if it.get("magnet") != magnet]
    items.insert(0, new_item)
    items = items[:50]
    if rs:
        if not rs.save_history("global_history", items):
            current_app.logger.warning("Failed to persist history to Redis")
    return jsonify({"success": True})

@history_bp.post("/api/history/delete")
@csrf_required
def delete_history():
    config = current_app.config
    data = require_json_body(config)
    magnet = data.get("magnet")
    if not magnet or not isinstance(magnet, str):
        from ..security import json_error
        return json_error(400, "bad_request", "Missing magnet link")
    
    rs = getattr(current_app, "rs", None)
    items = rs.get_history("global_history") if rs else []
    new_items = [it for it in items if it.get("magnet") != magnet]
    if len(items) != len(new_items) and rs:
        rs.save_history("global_history", new_items)
    return jsonify({"success": True})

@history_bp.post("/api/history/clear")
@csrf_required
def clear_history():
    rs = getattr(current_app, "rs", None)
    if rs:
        rs.save_history("global_history", [])
    return jsonify({"success": True})
