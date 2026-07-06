from fastapi import FastAPI
from .auth import auth_router
from .cloud import cloud_router
from .search import search_router
from .history import history_router
from .telegram import telegram_router
from .queue import queue_router
from .offcloud import offcloud_router

def register_routes(app: FastAPI):
    app.include_router(auth_router)
    app.include_router(cloud_router)
    app.include_router(search_router)
    app.include_router(history_router)
    app.include_router(telegram_router)
    app.include_router(queue_router)
    app.include_router(offcloud_router)
