from __future__ import annotations
import logging
import os
import asyncio
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .core.session import SessionMiddleware
from .redis_store import RedisStore
from .security import install_security_headers
from .services.seedr_service import SeedrService
from .services.search_service import SearchService
from .routes import register_routes

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

def create_app() -> FastAPI:
    app = FastAPI(title="CloudFlow Optimized & Stable")
    
    # Middlewares
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_ttl=settings.session_ttl_seconds)
    
    # Redis
    rs = None
    if settings.upstash_redis_url and settings.upstash_redis_token:
        rs = RedisStore(settings.upstash_redis_url, settings.upstash_redis_token)
    app.state.rs = rs

    # BACKGROUND TASK TRACKER (Fixes: "Task was destroyed but it is pending!")
    app.state.background_tasks = set()

    # Static & Templates
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    app.state.templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

    # Services
    app.state.search_service = SearchService(settings)
    app.state.seedr_service = SeedrService(settings.seedr_email, settings.seedr_password)

    # Routes
    register_routes(app)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "env": settings.app_env}

    return app

async def run_background_task(app, coro):
    """Wrapper to track background tasks and prevent ghost-task destruction."""
    task = asyncio.create_task(coro)
    app.state.background_tasks.add(task)
    try:
        await task
    finally:
        app.state.background_tasks.discard(task)
