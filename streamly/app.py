from __future__ import annotations

import logging
import os
import uuid
import asyncio
import hmac
import secrets
from typing import Any
from collections import deque
from pathlib import Path

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from jinja2 import Template

from .config import AppConfig
from .redis_store import RedisStore
from .security import (
    ValidationError,
    TokenBucketRateLimiter,
    rate_limited,
)
from .auth_utils import ensure_sid, get_csrf_token
from .cloud_service import CloudService
from .search_service import SearchService
from .store import NotAuthenticated, TTLStore
from .routes import register_routes
from .routes.telegram_client import manager as tg_manager
from .core.http_client import HttpClientManager

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

# Log buffering system for async architecture
_LOG_QUEUE = deque()


class AsyncRedisLogHandler(logging.Handler):
    """Buffered logger handler pushing logs to an in-memory queue to prevent blocking the event loop."""
    def __init__(self):
        super().__init__()
        self._skip_prefix = "streamly.redis_store"

    def emit(self, record):
        if record.name.startswith(self._skip_prefix):
            return
        try:
            line = self.format(record)
            _LOG_QUEUE.append(line)
        except Exception:
            # Deliberately not logged: this IS the log handler, so a failure here
            # logging to itself would risk infinite recursion. Truly best-effort.
            pass


async def periodic_log_flush_task(rs: RedisStore, interval_seconds: int = 5, max_lines: int = 50000):
    """Async loop flushing logs to Upstash Redis periodically."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            lines = []
            while _LOG_QUEUE:
                lines.append(_LOG_QUEUE.popleft())
            if lines:
                await rs.push_logs(lines, max_lines=max_lines)
        except Exception as e:
            # Note: 'lines' already left _LOG_QUEUE, so a failed push here does lose
            # them -- surfacing that via the console handler (not the Redis handler,
            # to avoid recursing back into the same failing path) is strictly better
            # than silently dropping log history with no trace.
            log.warning("Periodic log flush to Redis failed (some log lines lost): %s", e)


def run_background_task(app: FastAPI, coro) -> asyncio.Task:
    """Helper to run a coroutine in the background and track it in app.state."""
    task = asyncio.create_task(coro)
    app.state.background_tasks.add(task)
    task.add_done_callback(app.state.background_tasks.discard)
    return task


def create_app(
    config: AppConfig | None = None,
    *,
    cloud_service: CloudService | None = None,
    search_service: SearchService | None = None,
    client_store: TTLStore[Any] | None = None,
) -> FastAPI:
    config = config or AppConfig.from_env()
    app = FastAPI(title="CloudFlow", docs_url=None, redoc_url=None)
    
    # Store settings in state
    app.state.config = config
    app.state.background_tasks = set()
    app.state.run_background_task = lambda coro: run_background_task(app, coro)
    
    # Session Middleware moved down below add_security_headers to ensure request.session is available in the security middleware.

    # Initialize Services
    rs: RedisStore | None = None
    if config.upstash_redis_url and config.upstash_redis_token:
        rs = RedisStore(config.upstash_redis_url, config.upstash_redis_token)
    app.state.rs = rs

    # ROOT Logger Configuration
    root_log = logging.getLogger()
    root_log.setLevel(logging.INFO)

    for _noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | [%(request_id)s] | %(name)s:%(lineno)d | %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    
    # Inject request_id context dynamically using a filter
    class RequestIDFilter(logging.Filter):
        def filter(self, record):
            record.request_id = "system"
            return True
            
    console_handler.addFilter(RequestIDFilter())
    root_log.addHandler(console_handler)

    if rs is not None:
        redis_handler = AsyncRedisLogHandler()
        redis_handler.setFormatter(formatter)
        redis_handler.addFilter(RequestIDFilter())
        root_log.addHandler(redis_handler)
        
        # Start periodic log flusher async task
        @app.on_event("startup")
        async def start_log_flusher():
            task = asyncio.create_task(periodic_log_flush_task(rs))
            app.state.background_tasks.add(task)
            task.add_done_callback(app.state.background_tasks.discard)

    # Initialize Rate Limiter
    limiter = TokenBucketRateLimiter(config.rate_limit_capacity, config.rate_limit_refill_per_second)
    app.state.limiter = limiter

    # Initialize Cloud/Search/Store Services
    cloud = cloud_service or CloudService(config)
    search = search_service or SearchService(config)
    store = client_store or TTLStore[Any](config.session_ttl_seconds, config.client_store_max_entries)

    app.state.cloud = cloud
    app.state.search = search
    app.state.store = store

    # Initialize Offcloud Service
    from .offcloud_service import OffcloudService
    app.state.offcloud = OffcloudService(config.offcloud_api_key)

    # Register Routes
    register_routes(app)

    # Mount static & template folders
    HERE = Path(__file__).resolve().parent
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))

    # Security Headers Middleware
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        # Generate dynamic request id
        request.state.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:8]
        
        # Site protection check
        path = request.url.path
        if path != "/healthz" and path != "/healthz/deep" and path != "/site-login" and path != "/api/offcloud/debug-history" and not path.startswith("/static/"):
            site_password = os.getenv("SITE_PASSWORD")
            if site_password and not request.session.get("site_auth"):
                if path.startswith("/api/") or path.startswith("/fs/"):
                    return JSONResponse(
                        status_code=401,
                        content={"success": False, "error": {"code": "site_auth_required", "message": "Site password required"}}
                    )
                template = Template(SITE_LOGIN_HTML)
                return HTMLResponse(content=template.render(error=None))
                
        response = await call_next(request)
        
        is_hf = "SPACE_ID" in os.environ
        response.headers["X-Content-Type-Options"] = "nosniff"
        if not is_hf:
            response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        
        csp_ancestors = "frame-ancestors 'self' https://huggingface.co https://*.hf.space" if is_hf else "frame-ancestors 'none'"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data: https://m.media-amazon.com https://*.media-imdb.com https://*.ytimg.com; "
            "media-src 'self' blob: https:; "
            "connect-src 'self' https://bitsearch.eu https://v3.sg.media-imdb.com https://www.seedr.cc https:; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "script-src 'self'; "
            "frame-src 'self' https://www.youtube.com https://www.youtube-nocookie.com; "
            f"base-uri 'none'; object-src 'none'; {csp_ancestors}"
        )
        
        if path.startswith("/api/") or path.startswith("/fs/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            
        return response

    # Configure Session Middleware (added after security headers to wrap it)
    is_hf = "SPACE_ID" in os.environ
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.secret_key,
        session_cookie="session",
        max_age=365 * 24 * 60 * 60,
        same_site="none" if is_hf else "lax",
        https_only=is_hf or (config.environment == "production")
    )

    # Exception Handlers
    @app.exception_handler(ValidationError)
    async def handle_validation(request: Request, exc: ValidationError):
        return JSONResponse(status_code=400, content={"success": False, "error": {"code": "bad_request", "message": str(exc)}})

    @app.exception_handler(NotAuthenticated)
    async def handle_not_authenticated(request: Request, exc: NotAuthenticated):
        return JSONResponse(status_code=401, content={"success": False, "error": {"code": "unauthorized", "message": "Login required"}})

    @app.exception_handler(PermissionError)
    async def handle_permission(request: Request, exc: PermissionError):
        return JSONResponse(status_code=401, content={"success": False, "error": {"code": "authentication_failed", "message": "Authentication failed or provider unavailable"}})

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"success": False, "error": {"code": "error", "message": exc.detail}})

    @app.exception_handler(Exception)
    async def handle_exception(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", secrets.token_hex(4))
        log.exception("Unhandled error request_id=%s", rid)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": {"code": "internal_error", "message": "Internal server error"}},
            headers={"X-Request-ID": rid}
        )

    # Core Page Routes
    @app.get("/")
    async def index(request: Request):
        ensure_sid(request)
        static_dir = HERE / "static"
        try:
            asset_ver = int(max(
                os.path.getmtime(os.path.join(root, f))
                for root, _dirs, files in os.walk(static_dir)
                for f in files
            ))
        except ValueError:
            asset_ver = 1
            
        response = templates.TemplateResponse(
            request,
            name="index.html",
            context={"csrf_token": get_csrf_token(request), "asset_ver": asset_ver}
        )
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.get("/site-login")
    async def site_login_get():
        template = Template(SITE_LOGIN_HTML)
        return HTMLResponse(content=template.render(error=None))

    @app.post("/site-login")
    @rate_limited(cost=5.0)
    async def site_login_post(request: Request, password: str = Form(...)):
        site_password = os.getenv("SITE_PASSWORD")
        match = False
        if password and site_password:
            match = hmac.compare_digest(password.strip(), site_password.strip())
            
        log.info("Site login attempt: match=%s", match)
        if match:
            request.session["site_auth"] = True
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url="/", status_code=303)
            
        template = Template(SITE_LOGIN_HTML)
        return HTMLResponse(content=template.render(error="Incorrect site password"))

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "env": "production"}

    @app.get("/healthz/deep")
    async def healthz_deep():
        checks: dict[str, str] = {}
        if rs is None:
            checks["redis"] = "not_configured"
        else:
            try:
                pong = await rs._execute("PING")
                checks["redis"] = "ok" if pong else "unreachable"
            except Exception:
                checks["redis"] = "unreachable"
        checks["seedr"] = "configured" if config.seedr_email else "not_configured"
        checks["telegram"] = "configured" if config.telegram_api_id else "not_configured"
        degraded = checks.get("redis") == "unreachable"
        status_code = 503 if degraded else 200
        return JSONResponse(status_code=status_code, content={"ok": not degraded, "checks": checks})

    @app.post("/api/client-log")
    async def client_log(request: Request):
        try:
            data = await request.json()
            def _clean(v, limit):
                return str(v).replace("\r", " ").replace("\n", " ")[:limit]

            msg = _clean(data.get("message", ""), 500)
            url = _clean(data.get("url", ""), 300)
            line = _clean(data.get("line", ""), 16)
            col = _clean(data.get("column", ""), 16)
            stack = _clean(data.get("stack", ""), 1000)
            
            if rs:
                key_id = request.session.get("sid") or (request.client.host if request.client else "unknown")
                log_count_key = f"streamly:client_log_count:{key_id}"
                count = await rs._execute("INCR", log_count_key)
                if count == 1:
                    await rs._execute("EXPIRE", log_count_key, "60")
                if count and int(count) > 10:
                    return JSONResponse(status_code=429, content={"success": False, "error": "rate_limited"})
                
            log.warning("Client-side error: %s | URL: %s | Line: %s | Col: %s | Stack: %s", msg, url, line, col, stack)
            return {"success": True}
        except Exception as e:
            return JSONResponse(status_code=400, content={"success": False, "error": str(e)})

    @app.get("/api/logs")
    async def logs_gate():
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
        return HTMLResponse(content=html)

    @app.post("/api/logs")
    async def logs_download(request: Request, email: str = Form(...), password: str = Form(...)):
        cfg_email = config.seedr_email
        cfg_password = config.seedr_password

        if (
            email and password and
            cfg_email and cfg_password and
            hmac.compare_digest(email, cfg_email) and
            hmac.compare_digest(password, cfg_password)
        ):
            if rs is None:
                log.warning("Log download requested but Redis log persistence is unavailable")
                return JSONResponse(status_code=503, content={"success": False, "error": {"code": "unavailable", "message": "Log persistence is not configured"}})
            
            # Flush log queue
            lines = []
            while _LOG_QUEUE:
                lines.append(_LOG_QUEUE.popleft())
            if lines:
                await rs.push_logs(lines)
                
            logs = await rs.get_logs()
            if not logs:
                return JSONResponse(status_code=404, content={"success": False, "error": {"code": "not_found", "message": "No logs recorded yet"}})
            body = "\n".join(logs) + "\n"
            return Response(
                body,
                media_type="text/plain",
                headers={"Content-Disposition": 'attachment; filename="cloudflow.log"'}
            )

        log.warning("Unauthorized log access attempt from %s", request.client.host if request.client else "unknown")
        return JSONResponse(status_code=403, content={"success": False, "error": {"code": "forbidden", "message": "Invalid credentials"}})

    # App Startup and Shutdown hooks
    @app.on_event("startup")
    async def startup_event():
        # Test Redis Connection
        if rs is not None:
            try:
                await rs.get("streamly:health_check_test")
                log.info("Upstash Redis reachable — history, token & log persistence active")
                
                # Start Queue Daemons as Async Background Tasks
                from .routes.queue import trigger_seedr_queue
                trigger_seedr_queue(app)
                log.info("Seedr Queue Daemon started successfully.")
                
                # Startup Initialization Locks
                acquired = await rs._execute("SET", "streamly:startup_init_lock", "1", "EX", "15", "NX")
                if acquired == "OK":
                    log.info("Startup lock acquired. Initializing active transfer state.")
                    await rs._execute("DEL", "streamly:active_transfer_global")
                    await rs._execute("DEL", "streamly:seedr_queue_daemon_lock")
                    await rs._execute("DEL", "streamly:transfer_dispatch_lock")
                    await rs._execute("DEL", "streamly:seedr_active_monitor")
                    from .routes.telegram import trigger_next_transfer
                    trigger_next_transfer(app)
            except Exception as queue_err:
                log.warning("Failed to initialize queue/locks on startup: %s", queue_err)

    @app.on_event("shutdown")
    async def shutdown_event():
        # Flush final logs
        if rs is not None:
            try:
                lines = []
                while _LOG_QUEUE:
                    lines.append(_LOG_QUEUE.popleft())
                if lines:
                    await rs.push_logs(lines)
            except Exception as e:
                log.warning("Failed to flush final logs to Redis on shutdown: %s", e)
        
        # Cleanup Telegram sessions
        await tg_manager.cleanup_all()
        # Close Singleton HTTP Client
        await HttpClientManager.get_instance().close()

    return app


# Native app instance definition for uvicorn compatibility without --factory flag
app = create_app()
