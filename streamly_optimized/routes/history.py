from __future__ import annotations

import time
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel

from .auth import verify_csrf
from ..security import validate_magnet

history_router = APIRouter()


class AddHistoryPayload(BaseModel):
    magnet: str
    name: str | None = None
    size: str | None = None


class DeleteHistoryPayload(BaseModel):
    magnet: str


@history_router.get("/api/history")
async def get_history(request: Request):
    rs = request.app.state.rs
    try:
        items = await rs.get_history("global_history") if rs else []
        return {"success": True, "items": items}
    except Exception as e:
        request.app.state.logger.warning("Redis error on get_history: %s", e)
        return {"success": True, "items": [], "_warning": "History temporarily unavailable"}


@history_router.post("/api/history/add")
async def add_history(request: Request, payload: AddHistoryPayload, _csrf = Depends(verify_csrf)):
    config = request.app.state.config
    try:
        magnet = validate_magnet(payload.magnet, config)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    name = payload.name if payload.name else "Unknown Magnet"
    name = str(name)[:512]
        
    size = payload.size if payload.size else ""
    size = str(size)[:64]
        
    new_item = {
        "magnet": magnet,
        "title": name,
        "size": size,
        "time": time.strftime("%d/%m/%Y, %H:%M:%S")
    }
    
    rs = request.app.state.rs
    items = await rs.get_history("global_history") if rs else []
    items = [it for it in items if it.get("magnet") != magnet]
    items.insert(0, new_item)
    items = items[:50]
    
    if rs:
        success = await rs.save_history("global_history", items)
        if not success:
            request.app.state.logger.warning("Failed to persist history to Redis")
            
    return {"success": True}


@history_router.post("/api/history/delete")
async def delete_history(request: Request, payload: DeleteHistoryPayload, _csrf = Depends(verify_csrf)):
    magnet = payload.magnet
    if not magnet or not isinstance(magnet, str):
        raise HTTPException(status_code=400, detail="Missing magnet link")
    
    rs = request.app.state.rs
    items = await rs.get_history("global_history") if rs else []
    new_items = [it for it in items if it.get("magnet") != magnet]
    if len(items) != len(new_items) and rs:
        await rs.save_history("global_history", new_items)
        
    return {"success": True}


@history_router.post("/api/history/clear")
async def clear_history(request: Request, _csrf = Depends(verify_csrf)):
    rs = request.app.state.rs
    if rs:
        await rs.save_history("global_history", [])
    return {"success": True}
