from __future__ import annotations

from flask import Blueprint, jsonify, current_app, session
from ..auth_utils import current_client, ensure_sid
from ..security import (
    csrf_required,
    rate_limited,
    require_json_body,
    require_str,
    validate_email,
    validate_password,
    get_csrf_token,
)
from ..cloud_service import CloudService

auth_bp = Blueprint("auth", __name__)

@auth_bp.get("/api/csrf")
@rate_limited(cost=0.2)
def csrf():
    ensure_sid()
    return jsonify({"success": True, "csrfToken": get_csrf_token()})

@auth_bp.get("/api/status")
@rate_limited(cost=0.2)
def status_route():
    current_client()
    return jsonify({"success": True, "authenticated": True, "username": session.get("username", "")})

@auth_bp.post("/api/login")
@rate_limited(cost=5.0)
@csrf_required
def login():
    config = current_app.config
    data = require_json_body(config)
    email = validate_email(require_str(data, "email", max_len=320))
    password = validate_password(data.get("password", data.get("pass")))
    sid = ensure_sid()
    
    cloud = getattr(current_app, "cloud", None)
    client, username = cloud.login(email, password)
    
    store = getattr(current_app, "store", None)
    store.put(sid, client)
    session["username"] = username
    
    rs = getattr(current_app, "rs", None)
    if rs:
        rt = CloudService.serialize_token(client)
        if rt:
            rs.set_refresh_token(rt)
    return jsonify({"success": True, "username": username})

@auth_bp.post("/api/login/silent")
@rate_limited(cost=1.0)
def login_silent():
    try:
        current_client()
        return jsonify({"success": True, "username": session.get("username", "")})
    except NotAuthenticated:
        from ..security import json_error
        return json_error(401, "no_refresh_token", "No valid refresh token stored")
    except Exception:
        current_app.logger.exception("Unexpected error during silent login")
        from ..security import json_error
        return json_error(500, "internal_error", "Internal server error")
