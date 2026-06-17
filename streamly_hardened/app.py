from __future__ import annotations

import logging
import secrets
import hmac
import os
import uuid
import datetime
import threading
import atexit
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, g, render_template_string, session, make_response
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import AppConfig
from .redis_store import RedisStore
from .security import (
    ValidationError,
    ensure_sid,
    get_csrf_token,
    install_security_headers,
    json_error,
    TokenBucketRateLimiter,
    rate_limited,
)
from .cloud_service import CloudService
from .search_service import SearchService
from .store import NotAuthenticated, TTLStore
from .routes import register_routes
from .routes.trailers import trailers_bp, start_trailer_daemon
from . import extensions

log = logging.getLogger(__name__)

SITE_LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>Cloudflow | Protected Space</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        :root {
            --bg: #0d1117;
            --panel: rgba(22, 27, 34, 0.85);
            --line: rgba(255, 255, 255, 0.08);
            --accent: #2f9cf0;
            --text: #f0f6fc;
            --muted: #8b949e;
        }
        body {
            font-family: system-ui, -apple-system, sans-serif;
            background: var(--bg);
            color: var(--text);
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
            padding: 20px;
            box-sizing: border-box;
        }
        .card {
            background: var(--panel);
            backdrop-filter: blur(24px) saturate(160%);
            -webkit-backdrop-filter: blur(24px) saturate(160%);
            border: 1px solid var(--line);
            border-radius: 16px;
            padding: 32px;
            width: 100%;
            max-width: 380px;
            box-shadow: 0 16px 40px rgba(0, 0, 0, 0.45);
            text-align: center;
        }
        h2 { margin: 0 0 8px 0; font-size: 24px; font-weight: 700; }
        p { color: var(--muted); font-size: 14px; margin: 0 0 24px 0; }
        input {
            width: 100%;
            padding: 12px 16px;
            background: rgba(0, 0, 0, 0.2);
            border: 1px solid var(--line);
            border-radius: 8px;
            color: var(--text);
            box-sizing: border-box;
            font-size: 15px;
            margin-bottom: 16px;
            outline: none;
            transition: border-color 0.2s;
        }
        input:focus { border-color: var(--accent); }
        button {
            width: 100%;
            padding: 12px;
            background: var(--accent);
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 15px;
            font-weight: 700;
            cursor: pointer;
            transition: filter 0.2s, transform 0.1s;
        }
        button:hover { filter: brightness(1.1); }
        button:active { transform: scale(0.98); }
        .error { color: #ff7b72; font-size: 13px; margin-bottom: 16px; text-align: left; }
    </style>
</head>
<body>
    <div class="card">
        <h2>Protected Space</h2>
        <p>Enter the site password to access Cloudflow</p>
        <form method="POST" action="/site-login">
            {% if error %}
            <div class="error">{{ error }}</div>
            {% endif %}
            <input type="password" name="password" placeholder="Password" autofocus required>
            <button type="submit">Unlock</button>
        </form>
    </div>
</body>
</html>
"""

class RequestIDFilter(logging.Filter):
    """Injects the current request ID into every log record."""
    def filter(self, record):
        try:
            record.request_id = g.get("request_id", "system")
        except RuntimeError:
            # No application/request context (e.g. worker boot, background logging).
            record.request_id = "system"
        return True


class RedisLogHandler(logging.Handler):
    """Buffers log lines in memory and flushes them to Redis in BATCHES.

    Why: writing each line immediately cost 2 Redis commands (LPUSH+LTRIM) — the
    single biggest Redis-cost driver. Instead we collect lines in memory (RAM is
    abundant) and flush them as ONE batched LPUSH on a few cheap triggers:
      * a periodic background timer (default once/day),
      * buffer overflow (safety: a very busy period can't balloon memory or lose
        a huge batch),
      * before a log download (so the download is always fresh),
      * on process shutdown / pre-deploy (atexit).
    Console (stdout) still gets every line live, so hard-crash context remains
    visible in the host's log viewer even if the unflushed buffer is lost.

    Crash-proof and non-recursive:
      * never raises out of emit()/flush();
      * skips records from redis_store to avoid an infinite logging loop;
      * thread-safe buffer.
    """

    _SKIP_PREFIX = "streamly_hardened.redis_store"

    def __init__(self, redis_store, max_buffer: int = 5000):
        super().__init__()
        self._rs = redis_store
        self._max_buffer = max_buffer
        self._buf: list[str] = []
        self._lock = threading.Lock()

    def emit(self, record):
        # Skip our own Redis-layer logs to prevent a flush->log->flush loop.
        if record.name.startswith(self._SKIP_PREFIX):
            return
        try:
            line = self.format(record)
        except Exception:
            return
        overflow = False
        with self._lock:
            self._buf.append(line)
            if len(self._buf) >= self._max_buffer:
                overflow = True
        if overflow:
            # Safety flush: never let the buffer grow unbounded.
            self.flush_to_redis()

    def flush_to_redis(self) -> int:
        """Flush the in-memory buffer to Redis in one batch. Returns # flushed."""
        with self._lock:
            if not self._buf:
                return 0
            pending = self._buf
            self._buf = []
        try:
            ok = self._rs.push_logs(pending)
            if not ok:
                # Re-queue on failure so we don't silently drop logs.
                with self._lock:
                    self._buf[:0] = pending
                    # but cap so a persistent Redis outage can't OOM us
                    if len(self._buf) > self._max_buffer:
                        self._buf = self._buf[-self._max_buffer:]
                return 0
            return len(pending)
        except Exception:
            return 0


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
    
    is_hf = "SPACE_ID" in os.environ
    app.config.update(
        SECRET_KEY=config.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="None" if is_hf else "Lax",
        SESSION_COOKIE_SECURE=True if is_hf else (config.environment == "production"),
        MAX_CONTENT_LENGTH=config.max_json_bytes,
        JSON_SORT_KEYS=False,
        PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=365),
        # Export env vars for easier access in blueprints
        SEEDR_EMAIL=config.seedr_email,
        SEEDR_PASSWORD=config.seedr_password,
        TELEGRAM_API_ID=config.telegram_api_id,
        TELEGRAM_API_HASH=config.telegram_api_hash,
        TELEGRAM_PHONE=config.telegram_phone,
        TELEGRAM_CHAT_ID=config.telegram_chat_id,
        CLOUDFLARE_WORKER_PROXY=config.cloudflare_worker_proxy,
        TMDB_API_KEY=os.getenv("TMDB_API_KEY", ""),
        # Validation limits
        max_folder_id=config.max_folder_id,
        max_file_id=config.max_file_id,
        max_json_bytes=config.max_json_bytes,
        max_query_length=config.max_query_length,
        max_magnet_length=config.max_magnet_length,
    )
    install_security_headers(app)

    # Initialize Redis first — the logging system persists to it (see below).
    rs: RedisStore | None = None
    if config.upstash_redis_url and config.upstash_redis_token:
        rs = RedisStore(config.upstash_redis_url, config.upstash_redis_token)
    app.rs = rs

    # --- Logging Configuration ---
    # Root logger setup
    root_log = logging.getLogger()
    root_log.setLevel(logging.INFO)

    # SECURITY: silence HTTP client request logs. httpx logs the full request URL
    # at INFO, and Seedr's API passes the OAuth token in the query string
    # (?access_token=...), so an httpx INFO line leaks a live credential into the
    # console AND the batched Redis logs (downloadable via /api/logs). Raise these
    # to WARNING so request URLs are never logged. This previously only ran in the
    # __main__ block, so it did NOT apply under gunicorn on HF — hence the leak.
    for _noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    # Format: Timestamp | Level | RequestID | Module:Line | Message
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | [%(request_id)s] | %(name)s:%(lineno)d | %(message)s"
    )

    # Console Handler — captured by the host's dashboard log viewer.
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(RequestIDFilter())
    root_log.addHandler(console_handler)

    # Redis Handler — persists the most recent log lines to Upstash so they
    # survive restarts and can be downloaded via /api/logs. Disk is ephemeral
    # on the host, so we deliberately do NOT use a file handler.
    if rs is not None:
        redis_handler = RedisLogHandler(rs)
        redis_handler.setFormatter(formatter)
        redis_handler.addFilter(RequestIDFilter())
        root_log.addHandler(redis_handler)
        # Expose so the log-download route can flush-before-serve.
        app.redis_log_handler = redis_handler

        # Periodic flush (default once/day; override with LOG_FLUSH_SECONDS).
        flush_interval = int(os.getenv("LOG_FLUSH_SECONDS", str(24 * 60 * 60)))

        def _periodic_flush():
            while True:
                import time as _t
                _t.sleep(flush_interval)
                try:
                    redis_handler.flush_to_redis()
                except Exception:
                    pass

        _flush_thread = threading.Thread(target=_periodic_flush, name="LogFlush", daemon=True)
        _flush_thread.start()

        # Flush on clean shutdown / pre-deploy so nothing buffered is lost.
        atexit.register(redis_handler.flush_to_redis)
    # ----------------------------

    if rs is not None:
        try:
            rs.get("streamly:health_check_test")
            log.info("Upstash Redis reachable — history, token & log persistence active")
            
            # Start Seedr Queue Daemon background thread
            try:
                from .routes.queue import trigger_seedr_queue
                trigger_seedr_queue(app)
                log.info("Seedr Queue Daemon started successfully.")
            except Exception as daemon_init_err:
                log.warning("Failed to start Seedr Queue Daemon: %s", daemon_init_err)
                
            try:
                start_trailer_daemon(app)
                log.info("Trailer daemon started successfully.")
            except Exception as td_err:
                log.warning("Failed to start Trailer daemon: %s", td_err)
                
            try:
                # Use a startup lock to ensure only one worker clears the global active lock
                # and triggers the next transfer when the container boots up.
                acquired = rs._execute("SET", "streamly:startup_init_lock", "1", "EX", "15", "NX")
                if acquired == "OK":
                    log.info("Startup lock acquired. Initializing active transfer state and triggering next transfer.")
                    rs._execute("DEL", "streamly:active_transfer_global")
                    rs._execute("DEL", "streamly:seedr_queue_daemon_lock")
                    rs._execute("DEL", "streamly:transfer_dispatch_lock")
                    rs._execute("DEL", "streamly:seedr_active_monitor")
                    from .routes.telegram import trigger_next_transfer
                    trigger_next_transfer(rs)
                else:
                    log.info("Startup lock already held by another worker process. Skipping initialization.")
            except Exception as queue_err:
                log.warning("Failed to initialize sequential queue on startup: %s", queue_err)
        except Exception:
            log.warning(
                "Upstash Redis unreachable — history, token & log persistence disabled. "
                "Check UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN in your environment."
            )

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

    # Register Blueprints
    register_routes(app)
    app.register_blueprint(trailers_bp)

    @app.before_request
    def set_request_id():
        g.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:8]

    @app.before_request
    def check_site_auth():
        exempt_routes = ["static", "healthz", "healthz_deep", "site_login"]
        if request.endpoint in exempt_routes:
            return
            
        site_password = os.getenv("SITE_PASSWORD")
        if not site_password:
            return
            
        if not session.get("site_auth"):
            if request.path.startswith("/api/") or request.path.startswith("/fs/"):
                return json_error(401, "site_auth_required", "Site password required")
            return render_template_string(SITE_LOGIN_HTML)

    @app.route("/site-login", methods=["GET", "POST"])
    def site_login():
        if request.method == "POST":
            site_password = os.getenv("SITE_PASSWORD")
            password = request.form.get("password")
            
            p_len = len(password) if password else 0
            sp_len = len(site_password) if site_password else 0
            match = False
            if password and site_password:
                match = hmac.compare_digest(password.strip(), site_password.strip())
            
            log.info("Site login attempt: password_len=%d, site_password_len=%d, match=%s", p_len, sp_len, match)
            
            if match:
                session.permanent = True
                session["site_auth"] = True
                from flask import redirect
                return redirect("/")
            return render_template_string(SITE_LOGIN_HTML, error="Incorrect site password")
        return render_template_string(SITE_LOGIN_HTML)

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

    @app.errorhandler(405)
    def handle_405(exc):
        return json_error(405, "method_not_allowed", "Method not allowed")

    @app.errorhandler(413)
    def handle_payload_too_large(exc):
        # Bodies over MAX_CONTENT_LENGTH raise RequestEntityTooLarge (413). Return a
        # clean JSON 413 instead of letting the catch-all log it as a 500.
        return json_error(413, "payload_too_large", "Request body too large")

    @app.errorhandler(ConnectionError)
    def handle_connection_error(exc: ConnectionError):
        return json_error(502, "bad_gateway", "Upstream provider error")

    @app.errorhandler(Exception)
    def handle_exception(exc: Exception):
        # Let Werkzeug HTTP exceptions (404/405/413/...) keep their real status and
        # be handled by their own handlers — never re-label a 4xx as a logged 500.
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return exc
        # Use the Request ID from g for consistency
        rid = getattr(g, "request_id", secrets.token_hex(4))
        log.exception("Unhandled error request_id=%s", rid)
        response = json_error(500, "internal_error", "Internal server error")
        response.headers["X-Request-ID"] = rid
        return response

    @app.get("/")
    def index():
        ensure_sid()
        static_dir = os.path.join(os.path.dirname(__file__), "static")
        try:
            asset_ver = int(max(
                os.path.getmtime(os.path.join(root, f))
                for root, _dirs, files in os.walk(static_dir)
                for f in files
            ))
        except ValueError:
            asset_ver = 1
        response = make_response(render_template(
            "index.html", csrf_token=get_csrf_token(), asset_ver=asset_ver
        ))
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.get("/healthz")
    def healthz():
        # Liveness ONLY — always 200 so the probe never restart-loops on a Redis hiccup.
        return jsonify({"ok": True})

    @app.get("/healthz/deep")
    def healthz_deep():
        """Readiness/diagnostics probe — reports dependency status, gates NOTHING."""
        checks: dict[str, str] = {}
        rs = getattr(app, "rs", None)
        if rs is None:
            checks["redis"] = "not_configured"
        else:
            try:
                pong = rs._execute("PING")
                checks["redis"] = "ok" if pong else "unreachable"
            except Exception:
                checks["redis"] = "unreachable"
        checks["seedr"] = "configured" if app.config.get("SEEDR_EMAIL") else "not_configured"
        checks["telegram"] = "configured" if app.config.get("TELEGRAM_API_ID") else "not_configured"
        degraded = checks.get("redis") == "unreachable"
        return jsonify({"ok": not degraded, "checks": checks}), (503 if degraded else 200)

    @app.post("/api/client-log")
    @rate_limited(cost=1.0)
    def client_log():
        """Rate-limited client-side error logger (Phase 4)."""
        try:
            from .security import require_json_body
            data = require_json_body(app.config)

            def _clean(v, limit):
                # Strip CR/LF so client input can't forge extra log lines, and cap
                # length so it can't bloat the persisted Redis log.
                return str(v).replace("\r", " ").replace("\n", " ")[:limit]

            msg = _clean(data.get("message", ""), 500)
            url = _clean(data.get("url", ""), 300)
            line = _clean(data.get("line", ""), 16)
            col = _clean(data.get("column", ""), 16)
            stack = _clean(data.get("stack", ""), 1000)
            
            rs = getattr(app, "rs", None)
            if rs:
                from flask import session
                key_id = session.get("sid") or (request.remote_addr or "unknown")
                log_count_key = f"streamly:client_log_count:{key_id}"
                count = rs._execute("INCR", log_count_key)
                if count == 1:
                    rs._execute("EXPIRE", log_count_key, "60")
                if count and int(count) > 10:
                    return jsonify({"success": False, "error": "rate_limited"}), 429
                
            log.warning("Client-side error: %s | URL: %s | Line: %s | Col: %s | Stack: %s", msg, url, line, col, stack)
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400

    @app.get("/api/logs")
    @rate_limited(cost=1.0)
    def logs_gate():
        """Renders the secure log access credential form."""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>CloudFlow | Log Access</title>
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
                    <button type="submit">Download cloudflow.log</button>
                </form>
            </div>
        </body>
        </html>
        """
        return render_template_string(html)

    @app.post("/api/logs")
    @rate_limited(cost=3.0)
    def logs_download():
        """Verifies credentials and serves recent logs from Upstash Redis."""
        email = request.form.get("email")
        password = request.form.get("password")

        cfg_email = app.config.get("SEEDR_EMAIL")
        cfg_password = app.config.get("SEEDR_PASSWORD")

        if (
            email and password and
            cfg_email and cfg_password and
            hmac.compare_digest(email, cfg_email) and
            hmac.compare_digest(password, cfg_password)
        ):
            rs = getattr(app, "rs", None)
            if rs is None:
                log.warning("Log download requested but Redis log persistence is unavailable")
                return json_error(503, "unavailable", "Log persistence is not configured")
            # Flush the in-memory buffer first so the download includes the latest lines.
            handler = getattr(app, "redis_log_handler", None)
            if handler is not None:
                try:
                    handler.flush_to_redis()
                except Exception:
                    pass
            lines = rs.get_logs()
            if not lines:
                return json_error(404, "not_found", "No logs recorded yet")
            body = "\n".join(lines) + "\n"
            return Response(
                body,
                mimetype="text/plain",
                headers={"Content-Disposition": 'attachment; filename="cloudflow.log"'},
            )

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
