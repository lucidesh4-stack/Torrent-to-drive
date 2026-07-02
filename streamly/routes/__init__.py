from fastapi import APIRouter
from . import search, cloud, auth, history, queue, telegram

def register_routes(app):
    app.include_router(auth.router)
    app.include_router(search.router)
    app.include_router(cloud.router)
    app.include_router(history.router)
    app.include_router(queue.router)
    app.include_router(telegram.router)
