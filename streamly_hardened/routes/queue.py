from __future__ import annotations

import json as _json
import time
import uuid
import threading
from flask import Blueprint, jsonify, current_app, request
from ..security import csrf_required, rate_limited, require_json_body
from ..cloud_service import format_size, _safe_int

queue_bp = Blueprint("queue", __name__)

def add_to_history_backend(rs, magnet: str, name: str | None, size_bytes: int):
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
        raw = rs.get("streamly:history:global_history")
        items = []
        if raw:
            try:
                items = _json.loads(raw)
            except Exception:
                pass
        items = [it for it in items if it.get("magnet") != magnet]
        items.insert(0, new_item)
        items = items[:50]
        rs.set("streamly:history:global_history", _json.dumps(items))
    except Exception as e:
        current_app.logger.warning("Failed to save history in backend: %s", e)

def get_daemon_client(app):
    rs = getattr(app, "rs", None)
    cloud = getattr(app, "cloud", None)
    if not rs or not cloud:
        return None
        
    rt = rs.get_refresh_token()
    if rt:
        try:
            client, _ = cloud.login_with_saved_token(rt)
            new_rt = cloud.serialize_token(client)
            if new_rt:
                rs.set_refresh_token(new_rt)
            return client
        except Exception:
            app.logger.warning("Daemon failed to restore client session from saved token")
            
    email = app.config.get("SEEDR_EMAIL")
    pw = app.config.get("SEEDR_PASSWORD")
    if email and pw:
        try:
            client, _ = cloud.login(email, pw)
            rt = cloud.serialize_token(client)
            if rt:
                rs.set_refresh_token(rt)
            return client
        except Exception as e:
            app.logger.error("Daemon failed to login using credentials: %s", e)
            
    return None

def seedr_queue_daemon_loop(app):
    while True:
        time.sleep(15)
        rs = getattr(app, "rs", None)
        cloud = getattr(app, "cloud", None)
        if not rs or not cloud:
            continue
            
        lock_held = False
        try:
            # Acquire distributed lock to prevent multi-worker concurrency conflicts
            acquired = rs._execute("SET", "streamly:seedr_queue_daemon_lock", "1", "EX", "20", "NX")
            if acquired != "OK":
                continue
            lock_held = True
            
            client = get_daemon_client(app)
            if not client:
                continue
                
            storage = cloud.list_items(client, 0)
            transfers = storage.get("transfers", [])
            used = max(0, _safe_int(storage.get("used")))
            maximum = max(1, _safe_int(storage.get("max")))
            
            # Check and prune oversized/invalid active downloads
            active_changed = False
            
            # Build list of completed folder and file names (case-insensitive) to prune ghosts
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
                
                # 1. Cancel downloads exceeding 4.5 GB limit
                if t_size > 4.5 * 1024 * 1024 * 1024:
                    app.logger.warning("Active torrent '%s' resolved to size %s (> 4.5 GB). Cancelling.", t_name, t_size)
                    try:
                        cloud.delete_transfer(client, t_id)
                        active_changed = True
                    except Exception as err:
                        app.logger.error("Failed to cancel oversized torrent: %s", err)
                # 2. Cancel downloads exceeding user's total quota
                elif t_size > maximum:
                    app.logger.warning("Active torrent '%s' resolved to size %s which exceeds storage quota %s. Cancelling.", t_name, t_size, maximum)
                    try:
                        cloud.delete_transfer(client, t_id)
                        active_changed = True
                    except Exception as err:
                        app.logger.error("Failed to cancel quota-exceeding torrent: %s", err)
                # 3. Cancel ghost transfers (download finished but transfer is still active on Seedr)
                elif t_name and t_name.lower() in completed_names:
                    app.logger.warning("Ghost transfer detected for completed torrent '%s'. Deleting duplicate transfer.", t_name)
                    try:
                        cloud.delete_transfer(client, t_id)
                        active_changed = True
                    except Exception as err:
                        app.logger.error("Failed to delete ghost transfer: %s", err)
                # 4. Cancel stalled/stopped/failed transfers
                elif t_stopped or "error" in t_status or "failed" in t_status:
                    app.logger.warning("Transfer '%s' is stopped/failed (status: %s). Cancelling so it doesn't block the queue.", t_name, t_status)
                    try:
                        cloud.delete_transfer(client, t_id)
                        active_changed = True
                    except Exception as err:
                        app.logger.error("Failed to delete stopped/failed transfer: %s", err)
                        
            if active_changed:
                storage = cloud.list_items(client, 0)
                transfers = storage.get("transfers", [])
                used = max(0, _safe_int(storage.get("used")))
                maximum = max(1, _safe_int(storage.get("max")))
                
            # If an item is already downloading, do not start a new one
            if len(transfers) > 0:
                continue
                
            # Check queue list
            queue_len = rs._execute("LLEN", "streamly:seedr_queue")
            if not queue_len or int(queue_len) == 0:
                continue
                
            raw_item = rs._execute("LINDEX", "streamly:seedr_queue", "0")
            if not raw_item:
                continue
                
            if isinstance(raw_item, bytes):
                raw_item = raw_item.decode("utf-8")
            item = _json.loads(raw_item)
            
            item_name = item.get("name") or "torrent"
            item_magnet = item.get("magnet")
            
            # Check if this queued item is already downloading or in active transfers
            already_active = False
            for t in transfers:
                if (item_magnet and t.get("magnet") == item_magnet) or (item_name.lower() == (t.get("name") or "").lower()):
                    already_active = True
                    break
                    
            if already_active:
                rs._execute("LPOP", "streamly:seedr_queue")
                app.logger.warning("Queued item '%s' is already active on Seedr. Removing from queue.", item_name)
                continue
            
            item_size = _safe_int(item.get("size"))
            if item_size > 0:
                # Extra size validation check before attempting addition
                if item_size > 4.5 * 1024 * 1024 * 1024:
                    rs._execute("LPOP", "streamly:seedr_queue")
                    app.logger.warning("Discarded queued item '%s' because size %s > 4.5 GB", item_name, item_size)
                    continue
                    
                free_space = maximum - used
                if item_size > free_space:
                    # Space is not yet sufficient; wait in the queue
                    continue
                    
            # Sufficient space (or unknown size raw magnet). LPOP and add.
            rs._execute("LPOP", "streamly:seedr_queue")
            app.logger.info("Popping and adding queued torrent: %s", item_name)
            
            # Acquire adding lock during addition to block concurrent manual adds
            rs._execute("SET", "streamly:seedr_adding_lock", "1", "EX", "10")
            try:
                cloud.add_magnet(client, item.get("magnet"))
            except Exception as e:
                app.logger.error("Failed to add magnet popped from queue: %s", e)
                # Re-queue it at the front of the queue so it is not lost
                try:
                    rs._execute("LPUSH", "streamly:seedr_queue", _json.dumps(item))
                    app.logger.info("Re-queued failed torrent at front of queue: %s", item_name)
                except Exception as re_err:
                    app.logger.error("Failed to re-queue failed torrent: %s", re_err)
                
        except Exception as e:
            app.logger.exception("Error in SeedrQueueDaemon loop: %s", e)
        finally:
            if lock_held:
                try:
                    rs._execute("DEL", "streamly:seedr_queue_daemon_lock")
                except Exception:
                    pass

def trigger_seedr_queue(app):
    def run():
        with app.app_context():
            seedr_queue_daemon_loop(app)
    t = threading.Thread(target=run, name="SeedrQueueDaemon", daemon=True)
    t.start()

@queue_bp.get("/api/queue")
@rate_limited(cost=0.5)
def get_queue():
    rs = getattr(current_app, "rs", None)
    if not rs:
        return jsonify({"success": True, "items": []})
        
    try:
        raw_items = rs._execute("LRANGE", "streamly:seedr_queue", "0", "-1") or []
        items = []
        for raw in raw_items:
            try:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                items.append(_json.loads(raw))
            except Exception:
                pass
        return jsonify({"success": True, "items": items})
    except Exception as e:
        current_app.logger.warning("Failed to fetch Seedr queue from Redis: %s", e)
        return jsonify({"success": True, "items": []})


@queue_bp.post("/api/queue/cancel")
@rate_limited(cost=1.0)
@csrf_required
def cancel_queued_item():
    rs = getattr(current_app, "rs", None)
    if not rs:
        from ..security import json_error
        return json_error(503, "redis_unavailable", "Redis is required to manage the queue")
        
    data = require_json_body(current_app.config)
    task_id = data.get("task_id")
    if not task_id:
        from ..security import json_error
        return json_error(400, "bad_request", "Missing task_id")
        
    task_id = str(task_id).strip()
    
    try:
        raw_items = rs._execute("LRANGE", "streamly:seedr_queue", "0", "-1") or []
        remaining = []
        removed_item = None
        for raw in raw_items:
            try:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                item = _json.loads(raw)
                if item.get("task_id") == task_id:
                    removed_item = item
                else:
                    remaining.append(raw)
            except Exception:
                pass
                
        if removed_item:
            rs._execute("DEL", "streamly:seedr_queue")
            if remaining:
                rs._execute("RPUSH", "streamly:seedr_queue", *remaining)
            return jsonify({"success": True, "message": f"Cancelled queued item: {removed_item.get('name', 'torrent')}"})
            
        from ..security import json_error
        return json_error(404, "not_found", "Queued item not found")
    except Exception as e:
        current_app.logger.exception("Failed to cancel queued Seedr item")
        from ..security import json_error
        return json_error(500, "internal_error", str(e))

