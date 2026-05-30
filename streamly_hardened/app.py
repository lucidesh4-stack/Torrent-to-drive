from __future__ import annotations

import logging
import secrets
import os
from typing import Any

from flask import Flask, jsonify, render_template, request, session
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import AppConfig
from .redis_store import RedisStore
from .security import (
    ValidationError,
    csrf_required,
    ensure_sid,
    get_csrf_token,
    install_security_headers,
    json_error,
    rate_limited,
    require_json_body,
    require_str,
    TokenBucketRateLimiter,
    validate_category,
    validate_email,
    validate_item_type,
    validate_magnet,
    validate_order,
    validate_password,
    validate_positive_int,
    validate_query,
    validate_sort,
)
from .services import CloudService, SearchService, format_size, _safe_int
from .store import NotAuthenticated, TTLStore

log = logging.getLogger(__name__)


def create_app(
    config: AppConfig | None = None,
    *,
    cloud_service: CloudService | None = None,
    search_service: SearchService | None = None,
    client_store: TTLStore[Any] | None = None,
) -> Flask:
    config = config or AppConfig.from_env()
    app = Flask(__name__)
    # Behind Render's proxy: trust X-Forwarded-* for correct scheme/IP (rate limiter, secure cookies)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config.update(
        SECRET_KEY=config.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.environment == "production",
        MAX_CONTENT_LENGTH=config.max_json_bytes,
        JSON_SORT_KEYS=False,
    )
    install_security_headers(app)

    limiter = TokenBucketRateLimiter(config.rate_limit_capacity, config.rate_limit_refill_per_second)
    cloud = cloud_service or CloudService(config)
    search = search_service or SearchService(config)
    store = client_store or TTLStore[Any](config.session_ttl_seconds, config.client_store_max_entries)

    # Optional Upstash-backed refresh-token persistence (auto-relogin across restarts)
    rs: RedisStore | None = None
    if config.upstash_redis_url and config.upstash_redis_token:
        rs = RedisStore(config.upstash_redis_url, config.upstash_redis_token)

    def _try_restore_from_refresh(sid: str):
        """Attempt to rebuild a Seedr client from a stored refresh token.
        Returns the restored client (and updates the store/session) or raises NotAuthenticated."""
        if not rs:
            raise NotAuthenticated("Not authenticated")
        rt = rs.get_refresh_token()
        if not rt:
            raise NotAuthenticated("Not authenticated")
        try:
            client, username = cloud.login_with_saved_token(rt)
        except PermissionError:
            # Refresh token is dead — clean it up so we stop trying
            rs.delete_refresh_token()
            raise NotAuthenticated("Refresh token invalid")
        store.put(sid, client)
        session["username"] = username
        # Persist (possibly rotated) refresh token
        new_rt = CloudService.serialize_token(client)
        if new_rt and new_rt != rt:
            rs.set_refresh_token(new_rt)
        log.info("Session restored via global master token for sid=%s...", sid[:8])
        return client

    def current_client():
        sid = session.get("sid")
        if not sid:
            sid = ensure_sid()
        try:
            return store.get(sid)
        except NotAuthenticated:
            # First try restoring from the global master token in Redis
            try:
                return _try_restore_from_refresh(sid)
            except NotAuthenticated:
                pass

            # If that fails and we have env vars, auto-login silently
            if config.seedr_email and config.seedr_password:
                try:
                    client, username = cloud.login(config.seedr_email, config.seedr_password)
                    store.put(sid, client)
                    session["username"] = username
                    if rs:
                        rt = CloudService.serialize_token(client)
                        if rt:
                            rs.set_refresh_token(rt)
                    log.info("Auto-logged in headless mode for sid=%s", sid[:8])
                    return client
                except PermissionError:
                    # Bad credentials in env vars — not recoverable
                    log.error("Headless auto-login failed: invalid SEEDR_EMAIL/SEEDR_PASSWORD")
                except ConnectionError:
                    # Provider network issue — let it propagate to 502 handler
                    raise
                except Exception:
                    log.exception("Unexpected error during headless auto-login")

            # Fallback
            raise NotAuthenticated("Not authenticated")

    @app.errorhandler(ValidationError)
    def handle_validation(exc: ValidationError):
        return json_error(400, "bad_request", str(exc))

    @app.errorhandler(NotAuthenticated)
    def handle_not_authenticated(exc: NotAuthenticated):
        return json_error(401, "unauthorized", "Login required")

    @app.errorhandler(PermissionError)
    def handle_permission(exc: PermissionError):
        return json_error(401, "authentication_failed", "Authentication failed or provider unavailable")

    @app.errorhandler(404)
    def handle_404(exc):
        return json_error(404, "not_found", "Not found")

    @app.errorhandler(ConnectionError)
    def handle_connection_error(exc: ConnectionError):
        return json_error(502, "bad_gateway", str(exc) or "Upstream provider error")

    @app.errorhandler(Exception)
    def handle_exception(exc: Exception):
        request_id = request.headers.get("X-Request-ID") or secrets.token_hex(8)
        log.exception("Unhandled error request_id=%s", request_id)
        response = json_error(500, "internal_error", "Internal server error")
        response.headers["X-Request-ID"] = request_id
        return response

    @app.get("/")
    def index():
        ensure_sid()
        return render_template("index.html", csrf_token=get_csrf_token())

    @app.get("/healthz")
    def healthz():
        # Lightweight endpoint for uptime monitors (UptimeRobot etc.)
        return jsonify({"ok": True})

    @app.get("/api/csrf")
    @rate_limited(limiter, cost=0.2)
    def csrf():
        ensure_sid()
        return jsonify({"success": True, "csrfToken": get_csrf_token()})

    @app.get("/api/status")
    @rate_limited(limiter, cost=0.2)
    def status_route():
        current_client()
        return jsonify({"success": True, "authenticated": True, "username": session.get("username", "")})


    @app.post("/api/login")
    @rate_limited(limiter, cost=5.0)
    @csrf_required
    def login():
        data = require_json_body(config)
        email = validate_email(require_str(data, "email", max_len=320))
        password = validate_password(data.get("password", data.get("pass")))
        sid = ensure_sid()
        client, username = cloud.login(email, password)
        store.put(sid, client)
        session["username"] = username
        # Persist refresh_token for silent re-login across server restarts
        if rs:
            rt = CloudService.serialize_token(client)
            if rt:
                rs.set_refresh_token(rt)
        return jsonify({"success": True, "username": username})

    @app.post("/api/login/silent")
    @rate_limited(limiter, cost=1.0)
    def login_silent():
        """Attempt to restore session from .stored refresh token. No body required."""
        sid = session.get("sid") or ensure_sid()
        
        # In single-account mode, simply calling current_client() will
        # inherently trigger the fallback logic to log in using env vars.
        try:
            current_client()
            return jsonify({"success": True, "username": session.get("username", "")})
        except NotAuthenticated:
            return json_error(401, "no_refresh_token", "No valid refresh token stored")


    @app.get("/fs/folder/<folder_id>/items")
    @rate_limited(limiter, cost=1.0)
    def list_items(folder_id: str):
        folder = validate_positive_int(folder_id, name="folder_id", maximum=config.max_folder_id)
        try:
            data = cloud.list_items(current_client(), folder)
        except Exception as e:
            log.warning("Provider error on list: %s", e)
            return json_error(502, "provider_error", "Provider unavailable or failed to list items")
        for item in data["folders"] + data["files"]:
            item["size_str"] = format_size(item["size"])
        return jsonify(data)

    @app.post("/api/delete")
    @rate_limited(limiter, cost=2.0)
    @csrf_required
    def delete_item():
        data = require_json_body(config)
        item_type = validate_item_type(data.get("type"))
        item_id = validate_positive_int(data.get("id"), name="id", maximum=config.max_file_id)
        try:
            cloud.delete_item(current_client(), item_type, item_id)
        except Exception as e:
            log.warning("Provider error on delete: %s", e)
            return json_error(502, "provider_error", "Provider rejected the request or is unavailable")
        return jsonify({"success": True})

    @app.post("/api/zip")
    @rate_limited(limiter, cost=2.0)
    @csrf_required
    def zip_item():
        data = require_json_body(config)
        item_type = validate_item_type(data.get("type"))
        item_id = validate_positive_int(data.get("id"), name="id", maximum=config.max_file_id)
        url = cloud.get_zip_url(current_client(), item_type, item_id)
        return jsonify({"success": bool(url), "url": url})

    @app.post("/api/delete/bulk")
    @rate_limited(limiter, cost=3.0)
    @csrf_required
    def delete_bulk():
        data = require_json_body(config)
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return json_error(400, "bad_request", "items must be a non-empty list")
        if len(items) > 100:
            return json_error(400, "bad_request", "Too many items (max 100)")
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
            except Exception as exc:
                log.warning("Bulk delete item failed: %s", exc)
                results.append({"id": item.get("id"), "type": item.get("type"), "ok": False, "error": str(exc)[:200]})
        return jsonify({"success": True, "results": results})

    @app.post("/api/zip/bulk")
    @rate_limited(limiter, cost=3.0)
    @csrf_required
    def zip_bulk():
        data = require_json_body(config)
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return json_error(400, "bad_request", "items must be a non-empty list")
        if len(items) > 100:
            return json_error(400, "bad_request", "Too many items (max 100)")
        validated = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = validate_item_type(item.get("type"))
            item_id = validate_positive_int(item.get("id"), name="id", maximum=config.max_file_id)
            validated.append({"type": item_type, "id": item_id})
        if not validated:
            return json_error(400, "bad_request", "No valid items")
        url = cloud.get_zip_url_bulk(current_client(), validated)
        return jsonify({"success": bool(url), "url": url})

    @app.post("/api/add")
    @rate_limited(limiter, cost=2.0)
    @csrf_required
    def add_magnet():
        data = require_json_body(config)
        magnet = validate_magnet(data.get("magnet"), config)
        try:
            cloud.add_magnet(current_client(), magnet)
        except Exception as e:
            log.warning("Provider error on add: %s", e)
            return json_error(502, "provider_error", "Provider rejected the request (e.g. storage full) or is unavailable")
        return jsonify({"success": True})

    # --- History API Routes ---

    @app.get("/api/history")
    @rate_limited(limiter, cost=1.0)
    def get_history():
        # Global history (not tied to specific session IDs)
        try:
            items = rs.get_history("global_history") if rs else []
            return jsonify({"success": True, "items": items})
        except Exception as e:
            return json_error(500, "internal_error", str(e))

    @app.post("/api/history/add")
    @csrf_required
    def add_history():
        data = require_json_body(config)
        magnet = validate_magnet(data.get("magnet"), config)
        name = data.get("name", "Unknown Magnet")
        import time
        
        new_item = {
            "magnet": magnet,
            "title": name,
            "time": time.strftime("%d/%m/%Y, %H:%M:%S")
        }
        
        items = rs.get_history("global_history") if rs else []
        items = [it for it in items if it.get("magnet") != magnet]
        items.insert(0, new_item)
        items = items[:50] # keep last 50
        
        if rs:
            rs.save_history("global_history", items)
            
        return jsonify({"success": True})

    @app.post("/api/history/delete")
    @csrf_required
    def delete_history():
        data = require_json_body(config)
        magnet = data.get("magnet")
        if not magnet:
            return json_error(400, "bad_request", "Missing magnet link")
            
        items = rs.get_history("global_history") if rs else []
        new_items = [it for it in items if it.get("magnet") != magnet]
        if len(items) != len(new_items) and rs:
            rs.save_history("global_history", new_items)
            
        return jsonify({"success": True})
        
    @app.post("/api/history/clear")
    @csrf_required
    def clear_history():
        if rs:
            rs.save_history("global_history", [])
        return jsonify({"success": True})

    @app.get("/api/url")
    @rate_limited(limiter, cost=1.0)
    def get_url():
        file_id = validate_positive_int(request.args.get("file_id"), name="file_id", maximum=config.max_file_id)
        return jsonify({"success": True, "url": cloud.get_stream_url(current_client(), file_id)})

    @app.get("/api/suggest")
    @rate_limited(limiter, cost=0.5)
    def suggest():
        q = validate_query(request.args.get("q"), config)
        return jsonify(search.imdb_suggestions(q))

    @app.get("/api/search")
    @rate_limited(limiter, cost=1.0)
    def search_route():
        q = validate_query(request.args.get("q"), config)
        category = validate_category(request.args.get("category"), config)
        sort = validate_sort(request.args.get("sort"), config)
        order = validate_order(request.args.get("order"), config)
        page = validate_positive_int(request.args.get("page", 1), name="page", maximum=10_000)
        page = max(1, page)
        try:
            raw_payload = search.bitsearch(q, category, sort, order, page)
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
            rows.append(
                {
                    "name": title,
                    "size": format_size(_safe_int(item.get("size"))),
                    "seeds": int(item.get("seeders", 0) or 0),
                    "leeches": int(item.get("leechers", item.get("leeches", 0)) or 0),
                    "date": str(item.get("createdAt", "")).split("T")[0][:32],
                    "category": category_labels.get(raw_category, raw_category or "Other"),
                    "magnet": f"magnet:?xt=urn:btih:{infohash}&dn={title}",
                }
            )
        return jsonify({"results": rows, "pagination": pagination, "took": took})


    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    port = int(os.getenv("PORT", "5000"))
    host = os.getenv("HOST", "127.0.0.1")
    create_app().run(host=host, port=port, debug=False)
