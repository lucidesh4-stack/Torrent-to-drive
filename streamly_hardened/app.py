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
    ensure_sid,
    get_csrf_token,
    install_security_headers,
    json_error,
    require_json_body,
    TokenBucketRateLimiter,
)
from .cloud_service import CloudService
from .search_service import SearchService
from .store import NotAuthenticated, TTLStore
from .routes import register_routes
from . import extensions

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
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config.update(
        SECRET_KEY=config.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.environment == "production",
        MAX_CONTENT_LENGTH=config.max_json_bytes,
        JSON_SORT_KEYS=False,
        # Export env vars for easier access in blueprints
        SEEDR_EMAIL=config.seedr_email,
        SEEDR_PASSWORD=config.seedr_password,
        # Validation limits
        max_folder_id=config.max_folder_id,
        max_file_id=config.max_file_id,
        max_json_bytes=config.max_json_bytes,
        max_query_length=config.max_query_length,
        max_magnet_length=config.max_magnet_length,
    )
    install_security_headers(app)

    # Initialize Rate Limiter
    limiter = TokenBucketRateLimiter(config.rate_limit_capacity, config.rate_limit_refill_per_second)
    extensions.limiter = limiter

    # Initialize Services
    cloud = cloud_service or CloudService(config)
    search = search_service or SearchService(config)
    store = client_store or TTLStore[Any](config.session_ttl_seconds, config.client_store_max_entries)

    # Attach services to app for Blueprint access
    app.cloud = cloud
    app.search = search
    app.store = store

    # Initialize Redis
    rs: RedisStore | None = None
    if config.upstash_redis_url and config.upstash_redis_token:
        rs = RedisStore(config.upstash_redis_url, config.upstash_redis_token)
        if rs:
            try:
                rs.get("streamly:health_check_test")
                log.info("Upstash Redis reachable — history and token persistence active")
            except Exception:
                log.warning(
                    "Upstash Redis unreachable — history and token persistence disabled. "
                    "Check UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN in your environment."
                )
    app.rs = rs

    # Register Blueprints
    register_routes(app)

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
        return json_error(502, "bad_gateway", "Upstream provider error")

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
        import os as _os
        static_dir = _os.path.join(_os.path.dirname(__file__), "static")
        try:
            asset_ver = int(max(
                _os.path.getmtime(_os.path.join(root, f))
                for root, _dirs, files in _os.walk(static_dir)
                for f in files
            ))
        except ValueError:
            asset_ver = 1
        return render_template(
            "index.html", csrf_token=get_csrf_token(), asset_ver=asset_ver
        )

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    port = int(os.getenv("PORT", "5000"))
    host = os.getenv("HOST", "127.0.0.1")
    create_app().run(host=host, port=port, debug=False)
