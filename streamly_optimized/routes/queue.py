from __future__ import annotations

import json as _json
import asyncio
import uuid
import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel

from .auth import verify_csrf
from ..security import rate_limited
from ..cloud_service import format_size, _safe_int

log = logging.getLogger(__name__)
queue_router = APIRouter()


class CancelQueuePayload(BaseModel):
    task_id: str


async def add_to_history_backend(rs, magnet: str, name: str | None, size_bytes: int):
    if not rs:
        return
    import time
    size_str = format_size(size_bytes) if size_bytes else ""
    new_item = {
        "magnet": magnet,
        "title": name or "Unknown Magnet",
        "size": size_str,
        "time": time.strftime("%d/%m/%Y, %H:%M:%S")
    }
    try:
        raw = await rs.get("streamly:history:global_history")
        items = []
        if raw:
            try:
                items = _json.loads(raw)
            except Exception:
                pass
        items = [it for it in items if it.get("magnet") != magnet]
        items.insert(0, new_item)
        items = items[:50]
        await rs.set("streamly:history:global_history", _json.dumps(items))
    except Exception as e:
        log.warning("Failed to save history in backend: %s", e)


async def get_daemon_client(app):
    rs = getattr(app.state, "rs", None)
    cloud = getattr(app.state, "cloud", None)
    if not rs or not cloud:
        return None
        
    rt = await rs.get_refresh_token()
    if rt:
        try:
            client, _ = await cloud.login_with_saved_token(rt)
            new_rt = cloud.serialize_token(client)
            if new_rt:
                await rs.set_refresh_token(new_rt)
            return client
        except Exception:
            log.warning("Daemon failed to restore client session from saved token")
            
    email = app.state.config.seedr_email
    pw = app.state.config.seedr_password
    if email and pw:
        try:
            client, _ = await cloud.login(email, pw)
            rt = cloud.serialize_token(client)
            if rt:
                await rs.set_refresh_token(rt)
            return client
        except Exception as e:
            log.error("Daemon failed to login using credentials: %s", e)
            
    return None


async def seedr_queue_daemon_loop(app):
    idle_cycles = 0
    while True:
        # Adaptive sleep using async sleep
        await asyncio.sleep(60 if idle_cycles >= 3 else 15)
        rs = getattr(app.state, "rs", None)
        cloud = getattr(app.state, "cloud", None)
        if not rs or not cloud:
            continue
            
        try:
            queue_len = await rs._execute("LLEN", "streamly:seedr_queue")
            has_queue = bool(queue_len) and int(queue_len) > 0
        except Exception:
            has_queue = True

        active_marker = None
        if not has_queue:
            try:
                active_marker = await rs._execute("GET", "streamly:seedr_active_monitor")
            except Exception:
                active_marker = None

        if not has_queue and not active_marker:
            idle_cycles += 1
            continue
        idle_cycles = 0

        lock_held = False
        try:
            acquired = await rs._execute("SET", "streamly:seedr_queue_daemon_lock", "1", "EX", "20", "NX")
            if acquired != "OK":
                continue
            lock_held = True
            
            client = await get_daemon_client(app)
            if not client:
                continue
                
            storage = await cloud.list_items(client, 0)
            transfers = storage.get("transfers", [])
            
            try:
                if transfers:
                    await rs._execute("SET", "streamly:seedr_active_monitor", "1", "EX", "120")
                else:
                    await rs._execute("DEL", "streamly:seedr_active_monitor")
            except Exception:
                pass

            used = max(0, _safe_int(storage.get("used")))
            maximum = max(1, _safe_int(storage.get("max")))
            
            active_changed = False
            completed_names = set()
            for f in storage.get("folders", []):
                if f.get("name"):
                    completed_names.add(f.get("name").lower())
            for f in storage.get("files", []):
                if f.get("name"):
                    completed_names.add(f.get("name").lower())

            for t in list(transfers):
                t_id = t.get("id")
                t_name = t.get("name") or ""
                t_size = max(0, _safe_int(t.get("size", 0)))
                t_status = str(t.get("status", "")).lower()
                t_stopped = bool(t.get("stopped"))
                
                if t_size > 4.5 * 1024 * 1024 * 1024:
                    log.warning("Active torrent '%s' resolved to size %s (> 4.5 GB). Cancelling.", t_name, t_size)
                    try:
                        await cloud.delete_transfer(client, t_id)
                        active_changed = True
                    except Exception as err:
                        log.error("Failed to cancel oversized torrent: %s", err)
                elif t_size > maximum:
                    log.warning("Active torrent '%s' resolved to size %s which exceeds storage quota %s. Cancelling.", t_name, t_size, maximum)
                    try:
                        await cloud.delete_transfer(client, t_id)
                        active_changed = True
                    except Exception as err:
                        log.error("Failed to cancel quota-exceeding torrent: %s", err)
                elif t_name and t_name.lower() in completed_names:
                    log.warning("Ghost transfer detected for completed torrent '%s'. Deleting duplicate transfer.", t_name)
                    try:
                        await cloud.delete_transfer(client, t_id)
                        active_changed = True
                    except Exception as err:
                        log.error("Failed to delete ghost transfer: %s", err)
                elif t_stopped or "error" in t_status or "failed" in t_status:
                    log.warning("Transfer '%s' is stopped/failed (status: %s). Cancelling so it doesn't block the queue.", t_name, t_status)
                    try:
                        await cloud.delete_transfer(client, t_id)
                        active_changed = True
                    except Exception as err:
                        log.error("Failed to delete stopped/failed transfer: %s", err)
                        
            if active_changed:
                storage = await cloud.list_items(client, 0)
                transfers = storage.get("transfers", [])
                used = max(0, _safe_int(storage.get("used")))
                maximum = max(1, _safe_int(storage.get("max")))
                
            if len(transfers) > 0:
                continue
                
            queue_len = await rs._execute("LLEN", "streamly:seedr_queue")
            if not queue_len or int(queue_len) == 0:
                continue
                
            raw_item = await rs._execute("LINDEX", "streamly:seedr_queue", "0")
            if not raw_item:
                continue
                
            if isinstance(raw_item, bytes):
                raw_item = raw_item.decode("utf-8")
            item = _json.loads(raw_item)
            
            item_name = item.get("name") or "torrent"
            item_magnet = item.get("magnet")
            
            already_active = False
            for t in transfers:
                if (item_magnet and t.get("magnet") == item_magnet) or (item_name.lower() == (t.get("name") or "").lower()):
                    already_active = True
                    break
                    
            if already_active:
                await rs._execute("LPOP", "streamly:seedr_queue")
                log.warning("Queued item '%s' is already active on Seedr. Removing from queue.", item_name)
                continue
            
            item_size = _safe_int(item.get("size"))
            if item_size > 0:
                if item_size > 4.5 * 1024 * 1024 * 1024:
                    await rs._execute("LPOP", "streamly:seedr_queue")
                    log.warning("Discarded queued item '%s' because size %s > 4.5 GB", item_name, item_size)
                    continue
                    
                free_space = maximum - used
                if item_size > free_space:
                    continue
                    
            await rs._execute("LPOP", "streamly:seedr_queue")
            log.info("Popping and adding queued torrent: %s", item_name)
            
            await rs._execute("SET", "streamly:seedr_adding_lock", "1", "EX", "10")
            try:
                await cloud.add_magnet(client, item.get("magnet"))
            except Exception as e:
                log.error("Failed to add magnet popped from queue: %s", e)
                err_msg = str(e).lower()
                is_storage_full = "too large" in err_msg or "space" in err_msg or "413" in err_msg or "storage" in err_msg
                
                if is_storage_full:
                    # Move to BACK of queue (RPUSH)
                    try:
                        await rs._execute("RPUSH", "streamly:seedr_queue", _json.dumps(item))
                        log.warning("STORAGE_FULL error! Re-queued torrent at BACK of queue: %s", item_name)
                    except Exception as re_err:
                        log.error("Failed to re-queue torrent to back of queue: %s", re_err)
                else:
                    # Other transient errors: re-queue at front (LPUSH)
                    try:
                        await rs._execute("LPUSH", "streamly:seedr_queue", _json.dumps(item))
                        log.info("Re-queued failed torrent at front of queue: %s", item_name)
                    except Exception as re_err:
                        log.error("Failed to re-queue failed torrent: %s", re_err)
                
        except Exception as e:
            log.exception("Error in SeedrQueueDaemon loop: %s", e)
        finally:
            if lock_held:
                try:
                    await rs._execute("DEL", "streamly:seedr_queue_daemon_lock")
                except Exception:
                    pass


def trigger_seedr_queue(app):
    async def run():
        await seedr_queue_daemon_loop(app)
    # Track task in app.state.background_tasks
    task = asyncio.create_task(run())
    app.state.background_tasks.add(task)
    task.add_done_callback(app.state.background_tasks.discard)


@queue_router.get("/api/queue")
@rate_limited(cost=0.5)
async def get_queue(request: Request):
    rs = getattr(request.app.state, "rs", None)
    if not rs:
        return {"success": True, "items": []}
        
    try:
        raw_items = await rs._execute("LRANGE", "streamly:seedr_queue", "0", "-1") or []
        items = []
        for raw in raw_items:
            try:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                items.append(_json.loads(raw))
            except Exception:
                pass
        return {"success": True, "items": items}
    except Exception as e:
        log.warning("Failed to fetch Seedr queue from Redis: %s", e)
        return {"success": True, "items": []}


@queue_router.post("/api/queue/cancel")
@rate_limited(cost=1.0)
async def cancel_queued_item(request: Request, payload: CancelQueuePayload, _csrf = Depends(verify_csrf)):
    rs = getattr(request.app.state, "rs", None)
    if not rs:
        raise HTTPException(status_code=503, detail="Redis is required to manage the queue")
        
    task_id = payload.task_id.strip()
    
    try:
        raw_items = await rs._execute("LRANGE", "streamly:seedr_queue", "0", "-1") or []
        removed_item = None
        removed_raw = None
        for raw in raw_items:
            try:
                decoded = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                item = _json.loads(decoded)
                if item.get("task_id") == task_id:
                    removed_item = item
                    removed_raw = decoded
                    break
            except Exception:
                pass
 
        if removed_item and removed_raw is not None:
            await rs._execute("LREM", "streamly:seedr_queue", "0", removed_raw)
            return {"success": True, "message": f"Cancelled queued item: {removed_item.get('name', 'torrent')}"}
 
        raise HTTPException(status_code=404, detail="Queued item not found")
    except Exception as e:
        log.exception("Failed to cancel queued Seedr item")
        raise HTTPException(status_code=500, detail=str(e))
