from __future__ import annotations

import logging
import secrets
import os
import uuid
from logging.handlers import RotatingFileHandler
from typing import Any

from flask import Flask, jsonify, render_template, request, session, g, render_template_string, send_file
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


class RequestIDFilter(logging.Filter):
    """Injects the current request ID into every log record."""
    def filter(self, record):
        try:
            record.request_id = g.get("request_id", "system")
        except RuntimeError:
            # No application/request context (e.g. worker boot, background logging).
            record.request_id = "system"
        return True


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
        # Allowed lists
        allowed_categories=config.allowed_categories,
        allowed_sorts=config.allowed_sorts,
        allowed_orders=config.allowed_orders,
    )
    install_security_headers(app)

    # --- Logging Configuration ---
    # Root logger setup
    root_log = logging.getLogger()
    root_log.setLevel(logging.INFO)

    # Format: Timestamp | Level | RequestID | Module:Line | Message
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | [%(request_id)s] | %(name)s:%(lineno)d | %(message)s"
    )

    # File Handler (10MB per file, keep 5 backups)
    file_handler = RotatingFileHandler(
        "streamly.log", maxBytes=10*1024*1024, backupCount=5
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(RequestIDFilter())
    root_log.addHandler(file_handler)

    # Console Handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(RequestIDFilter())
    root_log.addHandler(console_handler)
    # ----------------------------

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

    @app.before_request
    def set_request_id():
        g.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:8]

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
        # Use the Request ID from g for consistency
        rid = getattr(g, "request_id", secrets.token_hex(4))
        log.exception("Unhandled error request_id=%s", rid)
        response = json_error(500, "internal_error", "Internal server error")
        response.headers["X-Request-ID"] = rid
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

    @app.get("/api/logs")
    def logs_gate():
        """Renders the secure log access credential form."""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Streamly | Log Access</title>
            <style>
                body { font-family: system-ui, sans-serif; display: flex; justify-content: center; 
                       align-items: center; height: 100vh; margin: 0; background: #f4f7f6; }
                .card { background: white; padding: 2rem; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.1); 
                        width: 100%; max-width: 350px; text-align: center; }
                h2 { margin-top: 0; color: #333; }
                p { color: #666; font-size: 0.9rem; margin-bottom: 1.5rem; }
                input { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 6px; box-sizing: border-box; }
                button { width: 100%; padding: 12px; background: #007bff; color: white; border: none; 
                        border-radius: 6px; cursor: pointer; font-weight: bold; transition: background 0.2s; }
                button:hover { background: #0056b3; }
            </style>
        </head>
        <body>
            <div class="card">
                <h2>System Logs</h2>
                <p>Enter Seedr credentials to download</p>
                <form method="POST" action="/api/logs">
                    <input type="email" name="email" placeholder="Email" required>
                    <input type="password" name="password" placeholder="Password" required>
                    <button type="submit">Download streamly.log</button>
                </form>
            </div>
        </body>
        </html>
        """
        return render_template_string(html)

    @app.post("/api/logs")
    def logs_download():
        """Verifies credentials and serves the log file."""
        email = request.form.get("email")
        password = request.form.get("password")
        
        if email == app.config.get("SEEDR_EMAIL") and password == app.config.get("SEEDR_PASSWORD"):
            log_path = "streamly.log"
            if os.path.exists(log_path):
                return send_file(log_path, as_attachment=True, download_name="streamly.log")
            log.warning("Log download requested but streamly.log does not exist")
            return json_error(404, "not_found", "Log file not yet created")
        
        log.warning("Unauthorized log access attempt from %s", request.remote_addr)
        return json_error(403, "forbidden", "Invalid credentials")

    return app


if __name__ == "__main__":
    # Base config for local run
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    port = int(os.getenv("PORT", "5000"))
    host = os.getenv("HOST", "127.0.0.1")
    create_app().run(host=host, port=port, debug=False)
