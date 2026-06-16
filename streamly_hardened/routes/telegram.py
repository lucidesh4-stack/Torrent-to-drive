from __future__ import annotations

import logging
import time
import uuid
import json as _json
import threading
import requests
import asyncio
import httpx
import secrets
from concurrent.futures import ThreadPoolExecutor

# Dedicated thread pool for all Redis operations so they never
# share the default executor with other background tasks.
_REDIS_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="redis-io")
from flask import Blueprint, jsonify, current_app, request, Response
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession
from telethon.network import ConnectionTcpIntermediate, MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InvokeWithLayerRequest
from telethon.tl.functions.auth import ExportAuthorizationRequest, ImportAuthorizationRequest
from telethon.tl.types import Channel, Chat
from ..auth_utils import current_client
from ..security import (
    csrf_required,
    rate_limited,
    require_json_body,
    validate_positive_int,
    ensure_sid,
    validate_public_url,
    ValidationError,
)

# --- Queue robustness (additive; happy path unchanged) ---
_ACTIVE_TTL_SECONDS = 90

# --- Layer 2: in-memory live progress (single-worker) ---
# Progress is updated in memory on every callback (free) and only PERSISTED to
# Redis every _PROGRESS_PERSIST_SECONDS (for recovery after restart/refresh).
# Read endpoints check memory first, then fall back to Redis. This cuts the
# dominant Redis-write cost during active transfers by ~5x without losing live
# updates. Safe because the app runs a single gunicorn worker.
_PROGRESS_PERSIST_SECONDS = 20      # how often to SET status to Redis (recovery)
_BANDWIDTH_FLUSH_SECONDS = 60       # how often to flush accumulated bandwidth (INCRBY)
# Let Telethon auto-sleep through flood waits up to this many seconds instead of
# raising FloodWaitError (which is unhandled and would fail a transfer). Telegram
# rarely returns waits beyond a few minutes for upload spam.
_FLOOD_SLEEP_THRESHOLD = 300
_LIVE_PROGRESS: dict[str, dict] = {}
_LIVE_PROGRESS_LOCK = threading.Lock()


def _live_set(task_id: str, data: dict) -> None:
    with _LIVE_PROGRESS_LOCK:
        _LIVE_PROGRESS[task_id] = data


def _live_get(task_id: str):
    with _LIVE_PROGRESS_LOCK:
        v = _LIVE_PROGRESS.get(task_id)
        return dict(v) if v is not None else None


def _live_get_active():
    """Most recent in-memory UPLOADING/QUEUED status, if any."""
    with _LIVE_PROGRESS_LOCK:
        for tid, v in _LIVE_PROGRESS.items():
            if v.get("status") in ("UPLOADING", "QUEUED"):
                out = dict(v)
                out.setdefault("task_id", tid)
                return out
    return None


def _live_clear(task_id: str) -> None:
    with _LIVE_PROGRESS_LOCK:
        _LIVE_PROGRESS.pop(task_id, None)
_DISPATCH_LOCK_KEY = "streamly:transfer_dispatch_lock"
_DISPATCH_LOCK_TTL = 15

# --- Telegram upload limits (standard accounts) ---
_TG_MAX_PARTS = 4000
_TG_PART_SIZE = 512 * 1024
_TG_HARD_MAX = _TG_MAX_PARTS * _TG_PART_SIZE


log = logging.getLogger(__name__)

telegram_bp = Blueprint("telegram", __name__)

def get_projected_bandwidth(rs, ym, current_file_size=0, active_item=None, queue_items=None, bw_bytes=None):
    if bw_bytes is None:
        raw_bw = rs.get(f"streamly:monthly_bandwidth:{ym}")
        bw_bytes = int(raw_bw) if raw_bw and raw_bw.isdigit() else 0
        
    projected = bw_bytes + current_file_size
    
    # 1. Add remaining bytes of the active transfer (if any)
    if active_item is not None:
        try:
            total = int(active_item.get("total_bytes", 0))
            sent = int(active_item.get("sent_bytes", 0))
            projected += max(0, total - sent)
        except Exception:
            pass
    else:
        active_task_id = rs.get("streamly:active_transfer_global")
        if active_task_id:
            if isinstance(active_task_id, bytes):
                active_task_id = active_task_id.decode("utf-8")
            raw_status = rs.get(f"streamly:transfer_status:{active_task_id}")
            if raw_status:
                try:
                    if isinstance(raw_status, bytes):
                        raw_status = raw_status.decode("utf-8")
                    status_data = _json.loads(raw_status)
                    total = int(status_data.get("total_bytes", 0))
                    sent = int(status_data.get("sent_bytes", 0))
                    remaining = max(0, total - sent)
                    projected += remaining
                except Exception:
                    pass
                
    # 2. Add sizes of all transfers in the queue
    if queue_items is not None:
        for item in queue_items:
            try:
                projected += int(item.get("total_bytes", 0))
            except Exception:
                pass
    else:
        queue_task_ids = rs._execute("LRANGE", "streamly:transfer_queue", "0", "-1")
        if queue_task_ids:
            for tid in queue_task_ids:
                try:
                    if isinstance(tid, bytes):
                        tid = tid.decode("utf-8")
                    raw_args = rs.get(f"streamly:task_args:{tid}")
                    if raw_args:
                        if isinstance(raw_args, bytes):
                            raw_args = raw_args.decode("utf-8")
                        args = _json.loads(raw_args)
                        projected += int(args.get("size", 0))
                except Exception:
                    pass
                
    return projected




class ProgressTracker:
    def __init__(self, rs, task_id, filename, total_bytes, loop, cancel_flag, sid=None):
        self.rs = rs
        self.task_id = task_id
        self.filename = filename
        self.total_bytes = total_bytes
        self.loop = loop
        self.cancel_flag = cancel_flag
        self.sid = sid
        self.last_pct = 0.0
        self.last_bandwidth_sent_bytes = 0
        self.last_write_time = time.time()
        self.last_write_bytes = 0
        self.last_speed_mb = None
        self.last_persist_time = 0.0   # force a status persist on the first callback
        self.last_bw_flush_time = time.time()  # bandwidth flushed on its own slower cadence

    def __call__(self, sent_bytes, total_bytes):
        # Check for cancel request via the non-blocking shared list flag
        if self.cancel_flag and self.cancel_flag[0]:
            raise ValueError("Cancelled by user")

        now = time.time()
        tot = total_bytes or self.total_bytes or 1
        pct = round((sent_bytes / tot) * 100, 1)

        # Recompute smoothed speed on the same ~2s cadence as before (cheap, in-proc).
        elapsed = now - self.last_write_time
        if elapsed >= 2.0 or pct >= 100.0 or pct - self.last_pct >= 5.0:
            bytes_sent_since_last_write = sent_bytes - self.last_write_bytes
            speed_bytes_sec = (bytes_sent_since_last_write / elapsed) if elapsed > 0 else 0.0
            raw_speed_mb = speed_bytes_sec / (1024 * 1024)
            if self.last_speed_mb is None:
                self.last_speed_mb = raw_speed_mb
            else:
                self.last_speed_mb = (0.7 * self.last_speed_mb) + (0.3 * raw_speed_mb)
            self.last_write_time = now
            self.last_write_bytes = sent_bytes
            self.last_pct = pct

        speed_mb = round(self.last_speed_mb or 0.0, 2)
        status = "COMPLETED" if pct >= 100.0 else "UPLOADING"

        # ---- ALWAYS update in-memory live state (free; read by status endpoints/SSE) ----
        _live_set(self.task_id, {
            "progress": pct,
            "status": status,
            "filename": self.filename,
            "sent_bytes": sent_bytes,
            "total_bytes": tot,
            "speed_mb": speed_mb,
            "error": None,
            "sid": self.sid,
        })

        # ---- Decide which Redis writes (if any) are due this callback ----
        # Layer 3 (write reduction): status persist and bandwidth flush run on
        # SEPARATE, slower cadences so we minimise billable WRITE commands.
        #   * status SET: 1 write, every _PROGRESS_PERSIST_SECONDS (recovery)
        #   * bandwidth: INCRBY + 2x EXPIRE, only every _BANDWIDTH_FLUSH_SECONDS
        # Both are forced once at 100% so final state + full bandwidth are recorded.
        finished = pct >= 100.0
        do_status = finished or (now - self.last_persist_time) >= _PROGRESS_PERSIST_SECONDS
        do_bw = finished or (now - self.last_bw_flush_time) >= _BANDWIDTH_FLUSH_SECONDS
        if not (do_status or do_bw):
            return

        bw_diff = 0
        if do_bw:
            self.last_bw_flush_time = now
            bw_diff = sent_bytes - self.last_bandwidth_sent_bytes
            if bw_diff > 0:
                self.last_bandwidth_sent_bytes = sent_bytes
        if do_status:
            self.last_persist_time = now

        def update_redis(st, p, sb, smb, bw, write_status, write_bw):
            try:
                if write_bw and bw > 0:
                    import datetime
                    try:
                        ym = datetime.datetime.now(datetime.UTC).strftime("%Y-%m")
                    except AttributeError:
                        ym = datetime.datetime.utcnow().strftime("%Y-%m")
                    self.rs._execute("INCRBY", f"streamly:monthly_bandwidth:{ym}", str(bw))
                    self.rs._execute("EXPIRE", f"streamly:monthly_bandwidth:{ym}", "5184000")
                    # Refresh the active-marker heartbeat on the same slow cadence
                    # as the bandwidth flush (still well within the 90s TTL).
                    self.rs._execute("EXPIRE", "streamly:active_transfer_global", str(_ACTIVE_TTL_SECONDS))
                if write_status:
                    # Single SET with inline EX = 1 write command.
                    self.rs._execute(
                        "SET",
                        f"streamly:transfer_status:{self.task_id}",
                        _json.dumps({
                            "progress": p,
                            "status": st,
                            "filename": self.filename,
                            "sent_bytes": sb,
                            "total_bytes": tot,
                            "speed_mb": smb,
                            "error": None,
                            "sid": self.sid
                        }),
                        "EX",
                        "3600"
                    )
            except Exception as e:
                log.warning("Failed to write transfer status in background: %s", e)

        self.loop.run_in_executor(_REDIS_EXECUTOR, update_redis,
                                  status, pct, sent_bytes, speed_mb, bw_diff, do_status, do_bw)

class UploadSender:
    def __init__(self, uploader, client, sender, file_id, part_count, big, loop):
        self.uploader = uploader
        self.client = client
        self.sender = sender
        self.part_count = part_count
        self.big = big
        self.file_id = file_id
        self.previous = None
        self.loop = loop
        self.exception = None

    async def start_upload(self, part_index: int, data: bytes) -> None:
        if self.exception:
            raise self.exception
        if self.previous:
            await self.previous
        self.previous = self.loop.create_task(self._next(part_index, data))

    async def _next(self, part_index: int, data: bytes) -> None:
        try:
            if self.big:
                request = functions.upload.SaveBigFilePartRequest(
                    file_id=self.file_id,
                    file_part=part_index,
                    file_total_parts=self.part_count,
                    bytes=data
                )
            else:
                request = functions.upload.SaveFilePartRequest(
                    file_id=self.file_id,
                    file_part=part_index,
                    bytes=data
                )
            await self.client._call(self.sender, request)
            self.uploader.update_progress(len(data))
        except Exception as e:
            self.exception = e
            raise e

    async def disconnect(self) -> None:
        if self.exception:
            raise self.exception
        if self.previous:
            await self.previous
        await self.sender.disconnect()

class ParallelUploader:
    def __init__(self, client, dc_id=None, progress_callback=None, file_size=0):
        self.client = client
        self.loop = client.loop
        self.dc_id = dc_id or client.session.dc_id
        self.auth_key = (None if dc_id and client.session.dc_id != dc_id
                         else client.session.auth_key)
        self.senders = []
        self.progress_callback = progress_callback
        self.file_size = file_size
        self.uploaded_bytes = 0

    def update_progress(self, sent):
        self.uploaded_bytes += sent
        if self.progress_callback:
            self.progress_callback(self.uploaded_bytes, self.file_size)

    async def _create_sender(self) -> MTProtoSender:
        dc = await self.client._get_dc(self.dc_id)
        sender = MTProtoSender(self.auth_key, loggers=self.client._log)
        await sender.connect(self.client._connection(
            dc.ip_address,
            dc.port,
            dc.id,
            loggers=self.client._log,
            proxy=self.client._proxy
        ))
        if not self.auth_key:
            log.info("Exporting auth key to DC %d", self.dc_id)
            auth = await self.client(ExportAuthorizationRequest(self.dc_id))
            self.client._init_request.query = ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes)
            req = InvokeWithLayerRequest(LAYER, self.client._init_request)
            await sender.send(req)
            self.auth_key = sender.auth_key
        return sender

    async def init_upload(self, file_id: int, file_size: int, part_size: int, connections: int) -> None:
        part_count = (file_size + part_size - 1) // part_size
        big = file_size > 10 * 1024 * 1024

        self.senders = [
            await self._create_upload_sender(file_id, part_count, big, connections),
            *await asyncio.gather(*[
                self._create_upload_sender(file_id, part_count, big, connections)
                for _ in range(1, connections)
            ])
        ]

    async def _create_upload_sender(self, file_id: int, part_count: int, big: bool, connections: int) -> UploadSender:
        sender_conn = await self._create_sender()
        return UploadSender(self, self.client, sender_conn, file_id, part_count, big, loop=self.loop)

    async def upload(self, part_index: int, part: bytes) -> None:
        # Check if there is an idle sender
        idle_sender = None
        for sender in self.senders:
            if sender.previous is None or sender.previous.done():
                idle_sender = sender
                break

        if idle_sender is None:
            # All senders are busy, wait for at least one to finish
            busy_tasks = {
                sender.previous: sender 
                for sender in self.senders 
                if sender.previous and not sender.previous.done()
            }
            if busy_tasks:
                done, pending = await asyncio.wait(list(busy_tasks.keys()), return_when=asyncio.FIRST_COMPLETED)
                finished_task = done.pop()
                idle_sender = busy_tasks[finished_task]

        # Start uploading on the idle sender
        await idle_sender.start_upload(part_index, part)

    async def finish_upload(self) -> None:
        if self.senders:
            await asyncio.gather(*[sender.disconnect() for sender in self.senders])
            self.senders = []

async def parallel_upload_file(client, output_queue, file_size, filename, progress_callback):
    part_size = 512 * 1024
    parts_count = (file_size + part_size - 1) // part_size
    file_id = secrets.randbits(63)
    is_big = file_size > 10 * 1024 * 1024

    # Fewer parallel upload connections => fewer SaveBigFilePart flood waits, with
    # negligible throughput loss (uploads are network-bound, not connection-bound).
    connections = 6 if file_size > 50 * 1024 * 1024 else (4 if file_size > 10 * 1024 * 1024 else 2)
    connections = min(connections, parts_count)

    uploader = ParallelUploader(client, progress_callback=progress_callback, file_size=file_size)
    await uploader.init_upload(file_id, file_size, part_size, connections)

    try:
        uploaded_parts = 0
        while uploaded_parts < parts_count:
            for sender in uploader.senders:
                if sender.exception:
                    raise sender.exception

            try:
                item = await asyncio.wait_for(output_queue.get(), timeout=90.0)
            except asyncio.TimeoutError:
                raise TimeoutError("Timeout waiting for downloaded data from queue")
            output_queue.task_done()

            if isinstance(item, Exception):
                raise item
            if item is None:
                raise ValueError("Download connection closed prematurely (received EOF before all parts were downloaded)")

            part_index, chunk = item
            await uploader.upload(part_index, chunk)
            uploaded_parts += 1

        await uploader.finish_upload()

    except Exception as e:
        await uploader.finish_upload()
        raise e

    if is_big:
        return types.InputFileBig(id=file_id, parts=parts_count, name=filename)
    else:
        return types.InputFile(id=file_id, parts=parts_count, name=filename, md5_checksum="")

def get_telegram_client(session_str):
    api_id = current_app.config.get("TELEGRAM_API_ID")
    api_hash = current_app.config.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise ValueError("Telegram credentials missing in configuration")
    return TelegramClient(
        StringSession(session_str),
        api_id,
        api_hash,
        connection=ConnectionTcpIntermediate,
        flood_sleep_threshold=_FLOOD_SLEEP_THRESHOLD
    )

async def validate_telegram_target(client, target_chat):
    try:
        if target_chat != "me":
            try:
                await client.get_dialogs()
            except Exception as de:
                log.warning("Failed to fetch dialogs for cache: %s", de)

        if str(target_chat).lstrip("-").isdigit():
            val = int(target_chat)
            # If a positive 10+ digit ID (commonly copied channel ID without prefix), try converting to channel ID format (-100...)
            if val > 0 and (len(str(val)) >= 10 or str(val).startswith("100")):
                try:
                    resolved_chat = int(f"-100{val}")
                    entity = await client.get_entity(resolved_chat)
                except Exception:
                    resolved_chat = val
                    entity = await client.get_entity(resolved_chat)
            else:
                resolved_chat = val
                entity = await client.get_entity(resolved_chat)
        else:
            resolved_chat = target_chat
            entity = await client.get_entity(resolved_chat)
    except Exception as e:
        raise ValueError(f"Could not find or access target chat/channel: {e}")

    if isinstance(entity, Channel):
        permissions = None
        try:
            permissions = await client.get_permissions(entity)
        except Exception as pe:
            log.warning("Failed to check channel permissions: %s", pe)
            
        if permissions is not None:
            if entity.broadcast:
                is_admin = getattr(permissions, "is_admin", False)
                is_creator = getattr(permissions, "is_creator", False)
                if not (is_admin or is_creator):
                    raise ValueError("You do not have permission to post in this broadcast channel (admin rights required).")
            else:
                if not getattr(permissions, "send_messages", True):
                    raise ValueError("You do not have permission to send messages here.")
    elif isinstance(entity, Chat):
        permissions = None
        try:
            permissions = await client.get_permissions(entity)
        except Exception as pe:
            log.warning("Failed to check chat permissions: %s", pe)
            
        if permissions is not None:
            if not getattr(permissions, "send_messages", True):
                raise ValueError("You do not have permission to send messages in this group.")

    return resolved_chat

def trigger_next_transfer(rs):
    """Atomic, multi-worker-safe dispatch wrapper (lock auto-expires; degrades to old behaviour)."""
    try:
        acquired = rs._execute("SET", _DISPATCH_LOCK_KEY, "1", "EX", str(_DISPATCH_LOCK_TTL), "NX")
    except Exception:
        acquired = "OK"  # Redis hiccup: don't drop the dispatch, fall back to unlocked path
    if acquired != "OK":
        log.info("Queue check: another worker is dispatching; skipping (will be handled there).")
        return
    try:
        _trigger_next_transfer_locked(rs)
    finally:
        try:
            rs._execute("DEL", _DISPATCH_LOCK_KEY)
        except Exception:
            pass


def _trigger_next_transfer_locked(rs):
    active = rs.get("streamly:active_transfer_global")
    if active:
        log.info("Queue check: transfer %s is active. Skipping.", active)
        return
        
    while True:
        task_id = rs._execute("RPOP", "streamly:transfer_queue")
        if not task_id:
            log.info("Queue check: no tasks in queue.")
            return
            
        args_raw = rs.get(f"streamly:task_args:{task_id}")
        if not args_raw:
            log.warning("Queue check: task %s has no arguments in Redis. Skipping.", task_id)
            continue
            
        try:
            args = _json.loads(args_raw)
            session_str = args["session_str"]
            api_id = args["api_id"]
            api_hash = args["api_hash"]
            file_url = args["file_url"]
            chat_id = args["chat_id"]
            filename = args["filename"]
            size = args["size"]
            sid = args["sid"]
            
            # Double check monthly bandwidth before starting the transfer
            import datetime
            import os
            try:
                ym = datetime.datetime.now(datetime.UTC).strftime("%Y-%m")
            except AttributeError:
                ym = datetime.datetime.utcnow().strftime("%Y-%m")
                
            raw_bw = rs.get(f"streamly:monthly_bandwidth:{ym}")
            bw_bytes = int(raw_bw) if raw_bw and raw_bw.isdigit() else 0
            
            is_hf = "SPACE_ID" in os.environ
            limit_bytes = int(99 * 1024 * 1024 * 1024) if is_hf else int(4.5 * 1024 * 1024 * 1024)
            limit_label = "99 GB" if is_hf else "4.5 GB"
            
            if bw_bytes >= limit_bytes or bw_bytes + size > limit_bytes:
                log.warning("Queue processing: task %s blocked because monthly bandwidth limit (%s) is exceeded", task_id, limit_label)
                rs._execute(
                    "SET",
                    f"streamly:transfer_status:{task_id}",
                    _json.dumps({
                        "progress": 0.0,
                        "status": "FAILED",
                        "error": f"Monthly bandwidth limit ({limit_label}) exceeded. Transfer blocked.",
                        "filename": filename,
                        "sent_bytes": 0,
                        "total_bytes": size
                    }),
                    "EX",
                    "3600"
                )
                rs._execute("DEL", f"streamly:task_args:{task_id}")
                continue
                
            break  # Successfully parsed a valid task
        except Exception as e:
            log.error("Queue check: failed to parse task args for %s: %s", task_id, e)
            rs._execute("DEL", f"streamly:task_args:{task_id}")
            continue
        
    rs.set("streamly:active_transfer_global", task_id, ex=_ACTIVE_TTL_SECONDS)
    rs.set(f"streamly:active_transfer:{sid}", task_id, ex=3600)
    
    t = threading.Thread(
        target=run_telethon_upload,
        args=(rs, session_str, api_id, api_hash, file_url, chat_id, filename, size, task_id, sid)
    )
    t.daemon = True
    t.start()
    log.info("Queue check: started transfer thread for task %s", task_id)

def run_telethon_upload(rs, session_str, api_id, api_hash, file_url, chat_id, filename, size, task_id, sid):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def upload():
        cancel_flag = [False]
        
        async def poll_cancel_request():
            while not cancel_flag[0]:
                try:
                    res = await loop.run_in_executor(_REDIS_EXECUTOR, rs.get, f"streamly:cancel_request:{task_id}")
                    if res:
                        cancel_flag[0] = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(5.0)
                
        cancel_poller = asyncio.create_task(poll_cancel_request())
        
        try:
            client = TelegramClient(
                StringSession(session_str),
                api_id,
                api_hash,
                connection=ConnectionTcpIntermediate,
                flood_sleep_threshold=_FLOOD_SLEEP_THRESHOLD
            )
            await client.connect()
            
            try:
                resolved_chat = await validate_telegram_target(client, chat_id)
            except Exception as pe:
                raise ValueError(str(pe))
            
            rs._execute(
                "SET",
                f"streamly:transfer_status:{task_id}",
                _json.dumps({
                    "progress": 0.0,
                    "status": "UPLOADING",
                    "filename": filename,
                    "sent_bytes": 0,
                    "total_bytes": size,
                    "error": None
                }),
                "EX",
                "3600"
            )
            
            exact_size = size
            import requests
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Connection": "keep-alive"
            }
            
            # Resolve proxy settings
            proxy_url = rs.get("streamly:cloudflare_worker_proxy")
            if proxy_url:
                if isinstance(proxy_url, bytes):
                    proxy_url = proxy_url.decode("utf-8")
                proxy_url = proxy_url.strip()
            if not proxy_url:
                proxy_url = current_app.config.get("CLOUDFLARE_WORKER_PROXY", "").strip()
            if not proxy_url:
                proxy_url = "https://streamly-proxy.lucidesh.workers.dev"

            if proxy_url:
                import urllib.parse
                download_url = f"{proxy_url.rstrip('/')}/?url={urllib.parse.quote(file_url)}"
                log.info("Routing download through Cloudflare Worker Proxy: %s", proxy_url)
            else:
                download_url = file_url
                log.info("Downloading directly from Seedr (no proxy configured)")

            # Query Content-Length using a streaming request (with fallback to direct Seedr URL)
            used_fallback = False
            try:
                if not proxy_url:
                    raise ValueError("No proxy configured")
                log.info("Querying Content-Length via proxy: %s", download_url)
                r = requests.get(download_url, stream=True, timeout=15.0, headers=headers)
                r.raise_for_status()
                content_len_header = r.headers.get("content-length")
                if content_len_header:
                    exact_size = int(content_len_header)
                else:
                    raise ValueError("Missing Content-Length header")
                if exact_size <= 0:
                    raise ValueError(f"Implausible Content-Length: {exact_size}")
                r.close()
            except Exception as e:
                log.warning("Proxy Content-Length check failed: %s. Falling back to direct Seedr URL.", e)
                download_url = file_url
                used_fallback = True
                exact_size = size  # Fall back to the Seedr-reported size
                try:
                    r = requests.get(download_url, stream=True, timeout=15.0, headers=headers)
                    r.raise_for_status()
                    content_len_header = r.headers.get("content-length")
                    if content_len_header:
                        exact_size = int(content_len_header)
                    r.close()
                except Exception as de:
                    log.warning("Direct Seedr Content-Length check failed: %s. Using reported size.", de)
            
            part_size = _TG_PART_SIZE
            parts_count = (exact_size + part_size - 1) // part_size
            if parts_count > _TG_MAX_PARTS:
                raise ValueError(f"File parts ({parts_count}) exceed Telegram upload limit of {_TG_MAX_PARTS} parts (file too large).")

            output_queue = asyncio.Queue(maxsize=16)

            def safe_put(item):
                if loop.is_closed():
                    return
                try:
                    fut = asyncio.run_coroutine_threadsafe(output_queue.put(item), loop)
                    fut.result()
                except Exception as ex:
                    log.warning("safe_put failed: %s", ex)

            def download_worker():
                try:
                    start_time = time.time()
                    downloaded_bytes = 0
                    
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "*/*",
                        "Accept-Encoding": "identity",
                        "Connection": "keep-alive"
                    }
                    
                    session = requests.Session()
                    try:
                        log.info("Downloading file from: %s", download_url)
                        r = session.get(download_url, stream=True, timeout=60.0, headers=headers)
                        r.raise_for_status()
                    except Exception as e:
                        if not used_fallback:
                            log.warning("Proxy download failed: %s. Retrying directly against Seedr URL.", e)
                            download_url = file_url
                            r = session.get(download_url, stream=True, timeout=60.0, headers=headers)
                            r.raise_for_status()
                        else:
                            raise e
                    
                    part_index = 0
                    buffer = bytearray()
                    remaining_bytes = exact_size
                    
                    for raw_chunk in r.iter_content(chunk_size=64 * 1024):
                        if cancel_flag[0]:
                            break
                        
                        if len(raw_chunk) > remaining_bytes:
                            raw_chunk = raw_chunk[:remaining_bytes]
                            
                        buffer.extend(raw_chunk)
                        remaining_bytes -= len(raw_chunk)
                        
                        while len(buffer) >= part_size:
                            chunk = bytes(buffer[:part_size])
                            del buffer[:part_size]
                            
                            downloaded_bytes += len(chunk)
                            
                            if part_index % 10 == 0:
                                elapsed = time.time() - start_time
                                if elapsed > 0:
                                    speed = downloaded_bytes / (elapsed * 1024 * 1024)
                                    log.info("Downloader speed: %.2f MB/s", speed)
                                    
                            safe_put((part_index, chunk))
                            part_index += 1
                            
                        if remaining_bytes <= 0:
                            break
                            
                    if not cancel_flag[0] and len(buffer) > 0:
                        chunk = bytes(buffer)
                        downloaded_bytes += len(chunk)
                        safe_put((part_index, chunk))
                        part_index += 1
                        
                    r.close()
                    
                    if not cancel_flag[0] and downloaded_bytes != exact_size:
                        raise ValueError(f"Download size mismatch: expected {exact_size} bytes, got {downloaded_bytes} bytes")
                        
                except Exception as de:
                    log.warning("Background download worker error: %s", de)
                    safe_put(de)
                finally:
                    safe_put(None)

            t = threading.Thread(target=download_worker, name="seedr-downloader")
            t.daemon = True
            t.start()
            
            tracker = ProgressTracker(rs, task_id, filename, exact_size, loop, cancel_flag, sid)
            
            uploaded = await parallel_upload_file(
                client,
                output_queue,
                file_size=exact_size,
                filename=filename,
                progress_callback=tracker
            )
            
            await loop.run_in_executor(None, t.join)
            
            await client.send_file(resolved_chat, uploaded, caption=f"File transferred: {filename}")
                    
            _completed_state = {
                "progress": 100.0,
                "status": "COMPLETED",
                "filename": filename,
                "sent_bytes": exact_size,
                "total_bytes": exact_size,
                "error": None,
                "sid": sid
            }
            _live_set(task_id, _completed_state)  # reflect final state in memory
            rs._execute(
                "SET",
                f"streamly:transfer_status:{task_id}",
                _json.dumps(_completed_state),
                "EX",
                "3600"
            )
        except Exception as e:
            log.exception("Telegram background upload failed")
            _failed_state = {
                "progress": 0.0,
                "status": "FAILED",
                "error": str(e),
                "filename": filename,
                "sent_bytes": 0,
                "total_bytes": exact_size,
                "sid": sid
            }
            _live_set(task_id, _failed_state)  # reflect final state in memory
            rs._execute(
                "SET",
                f"streamly:transfer_status:{task_id}",
                _json.dumps(_failed_state),
                "EX",
                "3600"
            )
        finally:
            cancel_flag[0] = True
            if not cancel_poller.done():
                cancel_poller.cancel()
            await client.disconnect()
            # Final state already persisted to Redis above; drop the in-memory
            # entry so the dict can't grow unbounded. Reads fall back to Redis.
            _live_clear(task_id)
            rs._execute("DEL", "streamly:active_transfer_global")
            rs._execute("DEL", f"streamly:task_args:{task_id}")
            trigger_next_transfer(rs)
            
    loop.run_until_complete(upload())
    loop.close()



@telegram_bp.get("/api/telegram/status")
@rate_limited(cost=1.0)
def telegram_status():
    rs = getattr(current_app, "rs", None)
    if not rs:
        return jsonify({"authenticated": False, "error": "Redis unavailable"})
        
    cryptg_active = False
    try:
        import cryptg
        cryptg_active = True
    except ImportError:
        pass

    from flask import session
    sid = session.get("sid") or ensure_sid()
    cache_key = f"streamly:tg_auth_cache:{sid}"
    
    # Try reading from cache
    cached = rs.get(cache_key)
    if cached:
        try:
            cached_data = _json.loads(cached)
            return jsonify({
                "authenticated": cached_data.get("authenticated", False),
                "cryptg_active": cryptg_active,
                "cached": True
            })
        except Exception:
            pass

    session_str = rs.get("streamly:telegram_session")
    if not session_str:
        return jsonify({"authenticated": False})
    
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def test_auth():
            client = get_telegram_client(session_str)
            await client.connect()
            authorized = await client.is_user_authorized()
            await client.disconnect()
            return authorized
            
        authorized = loop.run_until_complete(test_auth())
        loop.close()
        
        # Cache the result for 60 seconds
        rs.set(cache_key, _json.dumps({"authenticated": bool(authorized)}), ex=60)
        
        return jsonify({
            "authenticated": bool(authorized),
            "cryptg_active": cryptg_active
        })
    except Exception as e:
        log.exception("Error checking Telegram auth status")
        return jsonify({
            "authenticated": False,
            "cryptg_active": cryptg_active,
            "error": str(e)
        })


_DEFAULT_SPEEDTEST_URL = "https://speed.cloudflare.com/__down?bytes=10485760"


@telegram_bp.get("/api/telegram/test-download")
@rate_limited(cost=3.0)
def test_download_speed():
    # SECURITY: server-side fetch -> user `url` is an SSRF primitive. Now (1) requires
    # site auth (removed from exempt_routes), (2) rate limited, (3) rejects non-http(s)
    # schemes and any host resolving to a private/loopback/link-local/reserved address.
    raw_url = request.args.get("url")
    try:
        if raw_url is None or not raw_url.strip():
            test_url = _DEFAULT_SPEEDTEST_URL
        else:
            test_url, _pinned_ip = validate_public_url(raw_url)
    except ValidationError as ve:
        from ..security import json_error
        return json_error(400, "invalid_url", str(ve))

    max_bytes = 10 * 1024 * 1024  # Limit speed test to 10MB to avoid server overload
    try:
        import time
        import requests
        start_time = time.time()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive"
        }
        # allow_redirects=False so a public URL cannot 30x-redirect into a private address.
        r = requests.get(test_url, stream=True, timeout=30.0, headers=headers, allow_redirects=False)
        r.raise_for_status()
        total_downloaded = 0
        for chunk in r.iter_content(chunk_size=64 * 1024):
            total_downloaded += len(chunk)
            if total_downloaded >= max_bytes:
                break
        elapsed = time.time() - start_time
        speed_mb = total_downloaded / (elapsed * 1024 * 1024) if elapsed > 0 else 0.0
        return jsonify({
            "success": True,
            "bytes_downloaded": total_downloaded,
            "elapsed_seconds": round(elapsed, 2),
            "speed_mb_s": round(speed_mb, 2),
            "url": test_url
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@telegram_bp.post("/api/telegram/setup/send-code")
@rate_limited(cost=3.0)
@csrf_required
def send_code():
    rs = getattr(current_app, "rs", None)
    if not rs:
        from ..security import json_error
        return json_error(503, "redis_unavailable", "Redis is required for authentication")
    
    data = require_json_body(current_app.config)
    phone = data.get("phone")
    if not phone:
        from ..security import json_error
        return json_error(400, "bad_request", "Phone number is required")
    phone = str(phone).strip()
    
    try:
        api_id = current_app.config.get("TELEGRAM_API_ID")
        api_hash = current_app.config.get("TELEGRAM_API_HASH")
        if not api_id or not api_hash:
            from ..security import json_error
            return json_error(503, "config_missing", "TELEGRAM_API_ID or TELEGRAM_API_HASH is missing on server")
            
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def req_code():
            client = TelegramClient(
                StringSession(),
                api_id,
                api_hash,
                connection=ConnectionTcpIntermediate,
                flood_sleep_threshold=_FLOOD_SLEEP_THRESHOLD
            )
            await client.connect()
            res = await client.send_code_request(phone)
            temp_session = client.session.save()
            await client.disconnect()
            return temp_session, res.phone_code_hash
            
        temp_session, phone_code_hash = loop.run_until_complete(req_code())
        loop.close()
        
        setup_data = {
            "phone": phone,
            "temp_session": temp_session,
            "phone_code_hash": phone_code_hash
        }
        rs.set("streamly:telegram_temp_setup", _json.dumps(setup_data))
        return jsonify({"success": True})
    except Exception as e:
        log.exception("Failed to request Telegram verification code")
        from ..security import json_error
        return json_error(500, "telegram_error", str(e))

@telegram_bp.post("/api/telegram/setup/verify-code")
@rate_limited(cost=3.0)
@csrf_required
def verify_code():
    rs = getattr(current_app, "rs", None)
    if not rs:
        from ..security import json_error
        return json_error(503, "redis_unavailable", "Redis is required for authentication")
        
    data = require_json_body(current_app.config)
    code = data.get("code")
    if not code:
        from ..security import json_error
        return json_error(400, "bad_request", "Verification code is required")
    code = str(code).strip()
    
    setup_raw = rs.get("streamly:telegram_temp_setup")
    if not setup_raw:
        from ..security import json_error
        return json_error(400, "session_expired", "Setup session expired. Please enter phone number again.")
        
    try:
        setup_data = _json.loads(setup_raw)
        phone = setup_data["phone"]
        temp_session = setup_data["temp_session"]
        phone_code_hash = setup_data["phone_code_hash"]
        
        api_id = current_app.config.get("TELEGRAM_API_ID")
        api_hash = current_app.config.get("TELEGRAM_API_HASH")
        
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def sign_in_user():
            client = TelegramClient(
                StringSession(temp_session),
                api_id,
                api_hash,
                connection=ConnectionTcpIntermediate,
                flood_sleep_threshold=_FLOOD_SLEEP_THRESHOLD
            )
            await client.connect()
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
            final_session = client.session.save()
            await client.disconnect()
            return final_session
            
        final_session = loop.run_until_complete(sign_in_user())
        loop.close()
        
        from flask import session
        sid = session.get("sid") or ensure_sid()
        rs.set("streamly:telegram_session", final_session)
        rs._execute("DEL", "streamly:telegram_temp_setup")
        rs._execute("DEL", f"streamly:tg_auth_cache:{sid}")
        return jsonify({"success": True})
    except Exception as e:
        log.exception("Failed to verify Telegram code")
        from ..security import json_error
        return json_error(400, "auth_failed", f"Verification failed: {str(e)}")

def acquire_redis_lock(rs, lock_key, ttl_seconds, max_retries=10, retry_delay=0.1):
    import time
    for _ in range(max_retries):
        try:
            acquired = rs._execute("SET", lock_key, "1", "EX", str(ttl_seconds), "NX")
            if acquired == "OK":
                return True
        except Exception:
            return True
        time.sleep(retry_delay)
    return False


@telegram_bp.post("/api/telegram/send")
@rate_limited(cost=3.0)
@csrf_required
def telegram_send_file():
    rs = getattr(current_app, "rs", None)
    if not rs:
        from ..security import json_error
        return json_error(503, "redis_unavailable", "Redis is required for transfer updates")
        
    session_str = rs.get("streamly:telegram_session")
    if not session_str:
        from ..security import json_error
        return json_error(401, "telegram_not_authenticated", "Telegram is not authenticated")
        
    config = current_app.config
    data = require_json_body(config)
    file_id = validate_positive_int(data.get("file_id"), name="file_id", maximum=config.get("max_file_id", 1_000_000_000))
    
    from flask import session
    sid = session.get("sid") or ensure_sid()
    
    chat_id = config.get("TELEGRAM_CHAT_ID") or "-1004247146382"
    
    cloud = getattr(current_app, "cloud", None)
    try:
        client = current_client()
        file_details = client.fetch_file(file_id)
        filename = getattr(file_details, "name", "file")
        size = max(0, int(getattr(file_details, "size", 0)))
        file_url = getattr(file_details, "url", "")
        
        if not file_url:
            from ..security import json_error
            return json_error(404, "not_found", "Direct download URL is unavailable")
            
        # File size cap check (default 2.0 GB, adjustable for Telegram Premium up to 4.0 GB)
        # Note: Telegram uses decimal GB (1 GB = 1,000,000,000 bytes) for its API upload limit.
        import os
        max_file_size_gb = float(os.getenv("TELEGRAM_MAX_FILE_SIZE_GB", "2.0"))
        max_bytes = int(max_file_size_gb * 1000 * 1000 * 1000)
        
        # Absolute safety net: Telegram's MTProto upload has a strict limit of 4000 parts of 512 KB
        # for standard accounts, which is exactly 2,097,152,000 bytes (1.95 GiB).
        if max_file_size_gb <= 2.0:
            max_bytes = min(max_bytes, _TG_HARD_MAX)
            
        if size > max_bytes:
            from ..security import json_error
            return json_error(400, "file_too_large", f"File size ({size / (1000*1000*1000):.2f} GB) exceeds the Telegram upload limit of {max_file_size_gb} GB.")
            
    except Exception as e:
        log.warning("Failed to fetch file details from Seedr: %s", e)
        from ..security import json_error
        return json_error(502, "provider_error", "Failed to retrieve file from Seedr")
        
    # Bandwidth limit check under atomic lock (warning at 4.0 GB / 90 GB, block at 4.5 GB / 99 GB, tracking projected bandwidth)
    import datetime
    import os
    try:
        ym = datetime.datetime.now(datetime.UTC).strftime("%Y-%m")
    except AttributeError:
        ym = datetime.datetime.utcnow().strftime("%Y-%m")
        
    lock_key = "streamly:enqueue_lock"
    lock_acquired = acquire_redis_lock(rs, lock_key, ttl_seconds=5, max_retries=30, retry_delay=0.1)
    
    try:
        projected_bytes = get_projected_bandwidth(rs, ym, size)
        
        is_hf = "SPACE_ID" in os.environ
        if is_hf:
            limit_bytes = int(99 * 1024 * 1024 * 1024)
            warning_bytes = int(90 * 1024 * 1024 * 1024)
            limit_label = "99 GB"
            warning_label = "90 GB"
        else:
            limit_bytes = int(4.5 * 1024 * 1024 * 1024)
            warning_bytes = int(4.0 * 1024 * 1024 * 1024)
            limit_label = "4.5 GB"
            warning_label = "4.0 GB"
        
        if projected_bytes > limit_bytes:
            from ..security import json_error
            return json_error(400, "bandwidth_limit_exceeded", 
                              f"Queuing this file would exceed the monthly bandwidth limit of {limit_label}. Transfer blocked.")
                              
        warning_message = None
        if projected_bytes >= warning_bytes:
            warning_message = f"Monthly bandwidth consumption is projected to reach {warning_label} or more."
            
        api_id = config.get("TELEGRAM_API_ID")
        api_hash = config.get("TELEGRAM_API_HASH")
        
        task_id = uuid.uuid4().hex
        from flask import session
        sid = session.get("sid") or ensure_sid()
        
        rs._execute(
            "SET",
            f"streamly:transfer_status:{task_id}",
            _json.dumps({
                "progress": 0.0,
                "status": "QUEUED",
                "filename": filename,
                "sent_bytes": 0,
                "total_bytes": size,
                "error": None,
                "sid": sid
            }),
            "EX",
            "3600"
        )
        
        args = {
            "session_str": session_str,
            "api_id": api_id,
            "api_hash": api_hash,
            "file_url": file_url,
            "chat_id": chat_id,
            "filename": filename,
            "size": size,
            "sid": sid
        }
        rs.set(f"streamly:task_args:{task_id}", _json.dumps(args), ex=3600)
        
        rs.set(f"streamly:active_transfer:{sid}", task_id, ex=3600)
        rs._execute("LPUSH", "streamly:transfer_queue", task_id)
    finally:
        if lock_acquired:
            try:
                rs._execute("DEL", lock_key)
            except Exception:
                pass
    
    trigger_next_transfer(rs)
    
    res_data = {"success": True, "task_id": task_id}
    if warning_message:
        res_data["warning"] = warning_message
    return jsonify(res_data)

@telegram_bp.get("/api/telegram/task/<task_id>")
@rate_limited(cost=0.5)
def telegram_task_status(task_id):
    rs = getattr(current_app, "rs", None)
    if not rs:
        from ..security import json_error
        return json_error(503, "redis_unavailable", "Redis is required")
    # Layer 2: prefer fresh in-memory progress (no Redis read during live transfer).
    live = _live_get(task_id)
    raw = _json.dumps(live) if live is not None else rs.get(f"streamly:transfer_status:{task_id}")
    if not raw:
        raw = rs.get(f"streamly:telegram_task:{task_id}")
    if not raw:
        from ..security import json_error
        return json_error(404, "not_found", "Task not found")
        
    # Verify ownership
    from flask import session
    req_sid = session.get("sid") or ensure_sid()
    try:
        data = _json.loads(raw)
        owner_sid = data.get("sid")
        if owner_sid and owner_sid != req_sid:
            from ..security import json_error
            return json_error(403, "forbidden", "You do not own this task")
    except Exception:
        pass
        
    return Response(raw, mimetype="application/json")

@telegram_bp.get("/api/transfer/status")
@rate_limited(cost=0.5)
def transfer_status_route():
    rs = getattr(current_app, "rs", None)
    if not rs:
        from ..security import json_error
        return json_error(503, "redis_unavailable", "Redis is required")
    from flask import session
    sid = session.get("sid") or ensure_sid()
    task_id = rs.get(f"streamly:active_transfer:{sid}")
    if not task_id:
        return jsonify({"status": "IDLE"})
    if isinstance(task_id, bytes):
        task_id = task_id.decode("utf-8")
    # Layer 2: prefer fresh in-memory progress, fall back to Redis (recovery).
    live = _live_get(task_id)
    raw = _json.dumps(live) if live is not None else rs.get(f"streamly:transfer_status:{task_id}")
    if not raw:
        return jsonify({"status": "IDLE"})
        
    try:
        data = _json.loads(raw)
        if data.get("status") in ("COMPLETED", "FAILED"):
            rs.delete(f"streamly:active_transfer:{sid}")
    except Exception:
        pass
        
    return Response(raw, mimetype="application/json")


@telegram_bp.get("/api/telegram/queue")
@rate_limited(cost=0.5)
def get_telegram_queue():
    rs = getattr(current_app, "rs", None)
    if not rs:
        from ..security import json_error
        return json_error(503, "redis_unavailable", "Redis is required")
        
    active_task_id = rs.get("streamly:active_transfer_global")
    active_item = None
    if active_task_id:
        if isinstance(active_task_id, bytes):
            active_task_id = active_task_id.decode("utf-8")
        # Layer 2: prefer fresh in-memory status for the active task.
        live = _live_get(active_task_id)
        if live is not None:
            active_item = live
            active_item["task_id"] = active_task_id
        else:
            raw_status = rs.get(f"streamly:transfer_status:{active_task_id}")
            if raw_status:
                if isinstance(raw_status, bytes):
                    raw_status = raw_status.decode("utf-8")
                active_item = _json.loads(raw_status)
                active_item["task_id"] = active_task_id

    queue_task_ids = rs._execute("LRANGE", "streamly:transfer_queue", "0", "-1")
    queue_items = []
    if queue_task_ids:
        for tid in queue_task_ids:
            if isinstance(tid, bytes):
                tid = tid.decode("utf-8")
            raw_args = rs.get(f"streamly:task_args:{tid}")
            if raw_args:
                if isinstance(raw_args, bytes):
                    raw_args = raw_args.decode("utf-8")
                args = _json.loads(raw_args)
                queue_items.append({
                    "task_id": tid,
                    "filename": args.get("filename", "file"),
                    "total_bytes": args.get("size", 0),
                    "status": "QUEUED"
                })

    import datetime
    import os
    try:
        ym = datetime.datetime.now(datetime.UTC).strftime("%Y-%m")
    except AttributeError:
        ym = datetime.datetime.utcnow().strftime("%Y-%m")
    raw_bw = rs.get(f"streamly:monthly_bandwidth:{ym}")
    bw_bytes = int(raw_bw) if raw_bw and raw_bw.isdigit() else 0
    bw_gb = round(bw_bytes / (1024 * 1024 * 1024), 2)
    
    projected_bytes = get_projected_bandwidth(rs, ym, 0, active_item=active_item, queue_items=queue_items, bw_bytes=bw_bytes)
    projected_gb = round(projected_bytes / (1024 * 1024 * 1024), 2)
    
    is_hf = "SPACE_ID" in os.environ
    limit_gb = 99.0 if is_hf else 4.5
    
    destination = current_app.config.get("TELEGRAM_CHAT_ID") or "-1004247146382"
    
    return jsonify({
        "active": active_item,
        "queue": queue_items,
        "bandwidth_usage_gb": bw_gb,
        "bandwidth_projected_gb": projected_gb,
        "bandwidth_limit_gb": limit_gb,
        "destination": destination
    })


@telegram_bp.post("/api/telegram/cancel")
@rate_limited(cost=1.0)
@csrf_required
def telegram_cancel_transfer():
    rs = getattr(current_app, "rs", None)
    if not rs:
        from ..security import json_error
        return json_error(503, "redis_unavailable", "Redis is required")
        
    from flask import session
    sid = session.get("sid") or ensure_sid()
    
    # Try reading task_id from JSON request body
    task_id = None
    try:
        data = request.get_json(silent=True) or {}
        task_id = data.get("task_id")
    except Exception:
        pass
        
    # If not provided, fallback to the user's active transfer
    if not task_id:
        task_id = rs.get(f"streamly:active_transfer:{sid}")
        if isinstance(task_id, bytes):
            task_id = task_id.decode("utf-8")
            
    if not task_id:
        from ..security import json_error
        return json_error(400, "not_found", "No active transfer to cancel")

    # Fetch task args to verify ownership
    args_raw = rs.get(f"streamly:task_args:{task_id}")
    owner_sid = None
    if not args_raw:
        # Fallback to checking the status key if the task finished/failed
        raw_status = rs.get(f"streamly:transfer_status:{task_id}")
        if raw_status:
            try:
                status_data = _json.loads(raw_status)
                owner_sid = status_data.get("sid")
            except Exception:
                pass
        if not owner_sid:
            from ..security import json_error
            return json_error(404, "not_found", "Task not found or already completed")
    else:
        try:
            args = _json.loads(args_raw)
            owner_sid = args.get("sid")
        except Exception:
            owner_sid = None

    if owner_sid and owner_sid != sid:
        from ..security import json_error
        return json_error(403, "forbidden", "You do not own this task")
        
    active_global = rs.get("streamly:active_transfer_global")
    if isinstance(active_global, bytes):
        active_global = active_global.decode("utf-8")
        
    # Case 1: The task is currently running in the active uploader thread
    if active_global == task_id:
        rs.set(f"streamly:cancel_request:{task_id}", "1", ex=300)
        return jsonify({"success": True, "message": "Cancellation request sent to active transfer"})
        
    # Case 2: The task is queued but not yet active
    removed = rs._execute("LREM", "streamly:transfer_queue", "0", task_id)
    
    # Set status to FAILED/CANCELLED in Redis (preserving metadata)
    filename = "file"
    total_bytes = 0
    if args_raw:
        try:
            args = _json.loads(args_raw)
            filename = args.get("filename", "file")
            total_bytes = args.get("size", 0)
        except Exception:
            pass

    rs._execute(
        "SET",
        f"streamly:transfer_status:{task_id}",
        _json.dumps({
            "progress": 0.0,
            "status": "FAILED",
            "error": "Cancelled by user",
            "filename": filename,
            "sent_bytes": 0,
            "total_bytes": total_bytes,
            "sid": owner_sid
        }),
        "EX",
        "3600"
    )
    
    # Clean up keys for this task (using owner_sid)
    rs._execute("DEL", f"streamly:active_transfer:{owner_sid}")
    rs._execute("DEL", f"streamly:task_args:{task_id}")
    
    return jsonify({"success": True, "message": "Queued transfer cancelled successfully"})


@telegram_bp.post("/api/telegram/logout")
@rate_limited(cost=1.0)
@csrf_required
def telegram_logout():
    rs = getattr(current_app, "rs", None)
    if not rs:
        from ..security import json_error
        return json_error(503, "redis_unavailable", "Redis is required")
        
    from flask import session
    sid = session.get("sid") or ensure_sid()
    rs._execute("DEL", "streamly:telegram_session")
    rs._execute("DEL", f"streamly:tg_auth_cache:{sid}")
    return jsonify({"success": True})


@telegram_bp.get("/api/telegram/settings")
def get_telegram_settings():
    rs = getattr(current_app, "rs", None)
    if not rs:
        from ..security import json_error
        return json_error(503, "redis_unavailable", "Redis is required")
        
    proxy_url = rs.get("streamly:cloudflare_worker_proxy")
    if proxy_url:
        if isinstance(proxy_url, bytes):
            proxy_url = proxy_url.decode("utf-8")
        proxy_url = proxy_url.strip()
    else:
        proxy_url = ""
        
    return jsonify({
        "success": True,
        "cloudflare_worker_proxy": proxy_url
    })


@telegram_bp.post("/api/telegram/settings")
@rate_limited(cost=1.0)
@csrf_required
def save_telegram_settings():
    rs = getattr(current_app, "rs", None)
    if not rs:
        from ..security import json_error
        return json_error(503, "redis_unavailable", "Redis is required")
        
    data = require_json_body(current_app.config)
    proxy_url = data.get("cloudflare_worker_proxy", "").strip()
    
    if proxy_url:
        # Perform minor validation (should start with http:// or https://)
        if not (proxy_url.startswith("http://") or proxy_url.startswith("https://")):
            from ..security import json_error
            return json_error(400, "invalid_url", "Proxy URL must start with http:// or https://")
        rs.set("streamly:cloudflare_worker_proxy", proxy_url)
    else:
        rs.delete("streamly:cloudflare_worker_proxy")
        
    return jsonify({"success": True})



