# removed future annotations

import json as _json
import asyncio
import logging
import time
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel

from .auth import verify_csrf
from ..security import rate_limited
from ..cloud_service import format_size, _safe_int, _safe_float

log = logging.getLogger(__name__)
queue_router = APIRouter()

# Cap on non-storage-related add_magnet retries before a queued item is dropped instead
# of being re-queued forever. Storage-full re-queues (moved to the BACK of the queue,
# giving other items a chance first) are NOT subject to this cap -- that path is already
# safe by construction since it only blocks once other items ahead of it are tried.
_MAX_NON_STORAGE_RETRIES = 5


class CancelQueuePayload(BaseModel):
    task_id: str


async def add_to_history_backend(rs, magnet: str, name: str | None, size_bytes: int):
    if not rs:
        return

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
            except Exception as e:
                log.warning("Corrupted history blob in Redis, resetting to empty: %s", e)
        items = [it for it in items if it.get("magnet") != magnet]
        items.insert(0, new_item)

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
            except Exception as e:
                log.debug("Failed to update seedr_active_monitor heartbeat: %s", e)

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
                else:
                    t_progress = _safe_float(t.get("progress", 0.0))
                    t_speed = _safe_float(t.get("download_rate", 0.0))
                    is_loading = t_status.startswith("loading") or "seeder" in t_status or "collecting" in t_status or "stuck" in t_status
                    is_stuck = is_loading and (t_progress < 1.0) and (t_speed == 0.0)
                    
                    if is_stuck:
                        stuck_key = f"streamly:stuck_torrent:{t_id}"
                        try:
                            first_seen_str = await rs.get(stuck_key)

                            now = time.time()
                            if not first_seen_str:
                                await rs.set(stuck_key, str(now), ex=1800)
                            else:
                                try:
                                    first_seen = float(first_seen_str)
                                except ValueError:
                                    first_seen = now
                                if now - first_seen >= 300:
                                    log.warning("Torrent '%s' is stuck loading for 5+ mins. Re-queuing to the end of the queue...", t_name)
                                    magnet = None
                                    try:
                                        magnet_bytes = await rs._execute("HGET", "streamly:magnet_mapping", t_name.lower())
                                        if magnet_bytes:
                                            magnet = magnet_bytes.decode("utf-8") if isinstance(magnet_bytes, bytes) else magnet_bytes
                                    except Exception as me:
                                        log.debug("Failed to HGET magnet mapping: %s", me)
                                    
                                    if not magnet:
                                        try:
                                            history = await rs.get_history("global_history")
                                            for hist_item in history:
                                                h_title = hist_item.get("title", "")
                                                if h_title and (h_title.lower() == t_name.lower() or t_name.lower() in h_title.lower() or h_title.lower() in t_name.lower()):
                                                    magnet = hist_item.get("magnet")
                                                    if magnet:
                                                        break
                                        except Exception as he:
                                            log.debug("Failed to find magnet in history: %s", he)
                                            
                                    if magnet:
                                        await rs._execute("DEL", stuck_key)
                                        try:
                                            await cloud.delete_transfer(client, t_id)
                                            active_changed = True
                                        except Exception as err:
                                            log.error("Failed to delete stuck transfer: %s", err)
                                        
                                        queued_item = {
                                            "task_id": f"stk-{t_id}",
                                            "magnet": magnet,
                                            "name": t_name,
                                            "size": t_size,
                                            "time": int(time.time()),
                                            "retries": 0
                                        }
                                        try:
                                            await rs._execute("RPUSH", "streamly:seedr_queue", _json.dumps(queued_item))
                                            log.info("Re-queued stuck torrent '%s' to the back of the queue successfully.", t_name)
                                        except Exception as qe:
                                            log.error("Failed to re-queue stuck torrent: %s", qe)
                        except Exception as stuck_err:
                            log.error("Error processing stuck check for transfer %s: %s", t_id, stuck_err)
                    else:
                        try:
                            await rs._execute("DEL", f"streamly:stuck_torrent:{t_id}")
                        except Exception:
                            pass
                        
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
                try:
                    await rs._execute("HSET", "streamly:magnet_mapping", item_name.lower(), item.get("magnet"))
                except Exception as hm_err:
                    log.warning("Failed to store magnet mapping in daemon: %s", hm_err)
                await cloud.add_magnet(client, item.get("magnet"))
            except Exception as e:
                log.error("Failed to add magnet popped from queue: %s", e)
                err_msg = str(e).lower()
                is_storage_full = "too large" in err_msg or "space" in err_msg or "413" in err_msg or "storage" in err_msg
                
                if is_storage_full:
                    # Move to BACK of queue (RPUSH). Not subject to the retry cap: this
                    # path only re-blocks once every OTHER queued item has been tried
                    # first, so it can't create a permanent head-of-line-blocking loop
                    # the way an immediate front-of-queue retry could.
                    try:
                        await rs._execute("RPUSH", "streamly:seedr_queue", _json.dumps(item))
                        log.warning("STORAGE_FULL error! Re-queued torrent at BACK of queue: %s", item_name)
                    except Exception as re_err:
                        log.error("Failed to re-queue torrent to back of queue: %s", re_err)
                else:
                    # Other (non-storage) errors: these might be transient (a momentary
                    # Seedr API hiccup) or permanent (a malformed/dead torrent Seedr will
                    # reject identically forever). Re-queuing at the FRONT unconditionally
                    # previously created an unbounded infinite retry loop for the
                    # permanent-failure case: the same poisoned item would be popped again
                    # on every daemon cycle (15-60s), forever, blocking every item queued
                    # behind it and continuously hammering the Seedr API. Cap retries and
                    # drop the item instead of retrying forever once the cap is hit.
                    retries = _safe_int(item.get("retries", 0)) + 1
                    if retries > _MAX_NON_STORAGE_RETRIES:
                        log.error(
                            "Giving up on queued torrent '%s' after %d failed attempts (last error: %s). "
                            "Dropping from queue instead of retrying forever.",
                            item_name, retries - 1, e,
                        )
                        # Item is already removed from the queue (LPOP'd above) -- simply
                        # do not re-queue it. Falls through to the next daemon cycle.
                    else:
                        item["retries"] = retries
                        try:
                            await rs._execute("LPUSH", "streamly:seedr_queue", _json.dumps(item))
                            log.info("Re-queued failed torrent at front of queue (attempt %d/%d): %s", retries, _MAX_NON_STORAGE_RETRIES, item_name)
                        except Exception as re_err:
                            log.error("Failed to re-queue failed torrent: %s", re_err)
                
        except Exception as e:
            log.exception("Error in SeedrQueueDaemon loop: %s", e)
        finally:
            if lock_held:
                try:
                    await rs._execute("DEL", "streamly:seedr_queue_daemon_lock")
                except Exception as e:
                    # Not fatal (the lock carries a 20s TTL and will expire on its own),
                    # but worth knowing about since it delays the next daemon cycle.
                    log.warning("Failed to release seedr_queue_daemon_lock: %s", e)


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
            except Exception as e:
                log.warning("Skipping corrupted seedr_queue entry in get_queue: %s", e)
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
            except Exception as e:
                # If a queue entry is corrupted, this cancel request will fall through
                # to the "not found" 404 below with no other clue -- log it so that
                # isn't a silent mystery when debugging a cancel that mysteriously fails.
                log.warning("Skipping corrupted seedr_queue entry while scanning for task_id=%s: %s", task_id, e)
 
        if removed_item and removed_raw is not None:
            await rs._execute("LREM", "streamly:seedr_queue", "0", removed_raw)
            return {"success": True, "message": f"Cancelled queued item: {removed_item.get('name', 'torrent')}"}
 
        raise HTTPException(status_code=404, detail="Queued item not found")
    except Exception as e:
        log.exception("Failed to cancel queued Seedr item")
        raise HTTPException(status_code=500, detail=str(e))
