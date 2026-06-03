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
from telethon.network import ConnectionTcpIntermediate
from telethon.tl.types import Channel, Chat
from ..auth_utils import current_client
from ..security import csrf_required, rate_limited, require_json_body, validate_positive_int, ensure_sid

log = logging.getLogger(__name__)

telegram_bp = Blueprint("telegram", __name__)

def get_projected_bandwidth(rs, ym, current_file_size=0):
    raw_bw = rs.get(f"streamly:monthly_bandwidth:{ym}")
    bw_bytes = int(raw_bw) if raw_bw and raw_bw.isdigit() else 0
    
    projected = bw_bytes + current_file_size
    
    # 1. Add remaining bytes of the active transfer (if any)
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

class AsyncQueueStreamWrapper:
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue
        self.buffer = b""
        self.name = "file"
        self.producer_done = False
        self.exception = None

    async def read(self, n):
        if self.exception:
            raise self.exception

        while len(self.buffer) < n and not (self.producer_done and self.queue.empty()):
            try:
                item = await self.queue.get()
                self.queue.task_done()
                if item is None:
                    self.producer_done = True
                elif isinstance(item, Exception):
                    self.exception = item
                    raise item
                else:
                    self.buffer += item
            except Exception as e:
                if isinstance(e, Exception) and not isinstance(e, ValueError):
                    self.exception = e
                    raise e
                break

        data = self.buffer[:n]
        self.buffer = self.buffer[n:]
        return data

async def producer(response_stream, queue):
    try:
        # Use 512 KB chunks to exactly match upload part size — eliminates
        # internal buffering/splitting inside AsyncQueueStreamWrapper.
        async for chunk in response_stream.aiter_bytes(chunk_size=512 * 1024):
            await queue.put(chunk)
        await queue.put(None)
    except Exception as pe:
        log.warning("Producer stream read error: %s", pe)
        await queue.put(pe)

class ProgressTracker:
    def __init__(self, rs, task_id, filename, total_bytes, loop, cancel_flag):
        self.rs = rs
        self.task_id = task_id
        self.filename = filename
        self.total_bytes = total_bytes
        self.loop = loop
        self.cancel_flag = cancel_flag
        self.last_pct = 0.0
        self.last_bandwidth_sent_bytes = 0
        self.last_write_time = time.time()
        self.last_write_bytes = 0

    def __call__(self, sent_bytes, total_bytes):
        # Check for cancel request via the non-blocking shared list flag
        if self.cancel_flag and self.cancel_flag[0]:
            raise ValueError("Cancelled by user")

        now = time.time()
        tot = total_bytes or self.total_bytes or 1
        pct = round((sent_bytes / tot) * 100, 1)
        
        elapsed = now - self.last_write_time
        if elapsed >= 2.0 or pct >= 100.0 or pct - self.last_pct >= 5.0:
            bytes_sent_since_last_write = sent_bytes - self.last_write_bytes
            if elapsed > 0:
                speed_bytes_sec = bytes_sent_since_last_write / elapsed
            else:
                speed_bytes_sec = 0.0
                
            speed_mb = round(speed_bytes_sec / (1024 * 1024), 2)
            
            self.last_write_time = now
            self.last_write_bytes = sent_bytes
            self.last_pct = pct
            
            # Accumulate bandwidth diff since last throttled write
            bw_diff = sent_bytes - self.last_bandwidth_sent_bytes
            if bw_diff > 0:
                self.last_bandwidth_sent_bytes = sent_bytes

            status = "COMPLETED" if pct >= 100.0 else "UPLOADING"
            
            # Perform both bandwidth tracking and status update in a single
            # thread executor call to keep Redis commands minimal (throttled to
            # every 2 seconds instead of every 512 KB chunk).
            def update_redis(st, p, sb, smb, bw):
                try:
                    import datetime
                    try:
                        ym = datetime.datetime.now(datetime.UTC).strftime("%Y-%m")
                    except AttributeError:
                        ym = datetime.datetime.utcnow().strftime("%Y-%m")
                    if bw > 0:
                        self.rs._execute("INCRBY", f"streamly:monthly_bandwidth:{ym}", str(bw))
                        self.rs._execute("EXPIRE", f"streamly:monthly_bandwidth:{ym}", "5184000")
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
                            "error": None
                        }),
                        "EX",
                        "3600"
                    )
                except Exception as e:
                    log.warning("Failed to write transfer status in background: %s", e)
            
            self.loop.run_in_executor(_REDIS_EXECUTOR, update_redis, status, pct, sent_bytes, speed_mb, bw_diff)

async def parallel_upload_file(client, file_wrapper, file_size, filename, progress_callback, concurrency=8):
    part_size = 512 * 1024
    parts_count = (file_size + part_size - 1) // part_size
    file_id = secrets.randbits(63)
    is_big = file_size > 10 * 1024 * 1024

    uploaded_bytes = 0
    failed = False

    async def upload_part(part_index, data):
        nonlocal uploaded_bytes
        try:
            if is_big:
                req = functions.upload.SaveBigFilePartRequest(
                    file_id=file_id,
                    file_part=part_index,
                    file_total_parts=parts_count,
                    bytes=data
                )
            else:
                req = functions.upload.SaveFilePartRequest(
                    file_id=file_id,
                    file_part=part_index,
                    bytes=data
                )
            await client(req)
            uploaded_bytes += len(data)
            if progress_callback:
                progress_callback(uploaded_bytes, file_size)
        except Exception as e:
            log.warning("Failed to upload part %d: %s", part_index, e)
            raise e

    # Sliding window: keep exactly `concurrency` tasks in-flight at a time.
    # This prevents pre-creating thousands of tasks for large files, which
    # congests the asyncio scheduler and causes event-loop stalls.
    sem = asyncio.Semaphore(concurrency)
    active_tasks: list[asyncio.Task] = []

    async def bounded_upload(part_index, data):
        async with sem:
            await upload_part(part_index, data)

    part_index = 0
    while True:
        chunk = await file_wrapper.read(part_size)
        if not chunk:
            break
        task = asyncio.create_task(bounded_upload(part_index, chunk))
        active_tasks.append(task)
        part_index += 1

        # Prune completed tasks from the list to keep memory usage flat
        active_tasks = [t for t in active_tasks if not t.done()]

    # Wait for all remaining in-flight tasks
    if active_tasks:
        results = await asyncio.gather(*active_tasks, return_exceptions=True)
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            raise errors[0]

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
        connection=ConnectionTcpIntermediate
    )

async def validate_telegram_target(client, target_chat):
    try:
        resolved_chat = int(target_chat) if str(target_chat).lstrip("-").isdigit() else target_chat
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
            limit_bytes = int(1000 * 1024 * 1024 * 1024) if is_hf else int(4.5 * 1024 * 1024 * 1024)
            limit_label = "1000 GB" if is_hf else "4.5 GB"
            
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
        
    rs.set("streamly:active_transfer_global", task_id, ex=3600)
    rs.set(f"streamly:active_transfer:{sid}", task_id, ex=3600)
    
    t = threading.Thread(
        target=run_telethon_upload,
        args=(rs, session_str, api_id, api_hash, file_url, chat_id, filename, size, task_id)
    )
    t.daemon = True
    t.start()
    log.info("Queue check: started transfer thread for task %s", task_id)

def run_telethon_upload(rs, session_str, api_id, api_hash, file_url, chat_id, filename, size, task_id):
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
                connection=ConnectionTcpIntermediate
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
            
            limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
            async with httpx.AsyncClient(limits=limits, timeout=120.0, follow_redirects=True) as httpx_client:
                async with httpx_client.stream("GET", file_url) as r:
                    r.raise_for_status()
                    
                    exact_size = size
                    content_len_header = r.headers.get("content-length")
                    if content_len_header:
                        try:
                            exact_size = int(content_len_header)
                        except ValueError:
                            pass
                    
                    # Larger buffer (16 × 512 KB = 8 MB) prevents upload
                    # workers from stalling while waiting for the next
                    # Seedr chunk to arrive.
                    queue = asyncio.Queue(maxsize=16)
                    producer_task = asyncio.create_task(producer(r, queue))
                    
                    wrapper = AsyncQueueStreamWrapper(queue)
                    wrapper.name = filename
                    
                    tracker = ProgressTracker(rs, task_id, filename, exact_size, loop, cancel_flag)
                    
                    uploaded = await parallel_upload_file(
                        client,
                        wrapper,
                        file_size=exact_size,
                        filename=filename,
                        progress_callback=tracker,
                        concurrency=8
                    )
                    
                    await producer_task
                    
                    await client.send_file(resolved_chat, uploaded, caption=f"File transferred: {filename}")
                    
            rs._execute(
                "SET",
                f"streamly:transfer_status:{task_id}",
                _json.dumps({
                    "progress": 100.0,
                    "status": "COMPLETED",
                    "filename": filename,
                    "sent_bytes": exact_size,
                    "total_bytes": exact_size,
                    "error": None
                }),
                "EX",
                "3600"
            )
        except Exception as e:
            log.exception("Telegram background upload failed")
            rs._execute(
                "SET",
                f"streamly:transfer_status:{task_id}",
                _json.dumps({
                    "progress": 0.0,
                    "status": "FAILED",
                    "error": str(e),
                    "filename": filename,
                    "sent_bytes": 0,
                    "total_bytes": size
                }),
                "EX",
                "3600"
            )
        finally:
            cancel_flag[0] = True
            if not cancel_poller.done():
                cancel_poller.cancel()
            await client.disconnect()
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
        return jsonify({"authenticated": bool(authorized)})
    except Exception as e:
        log.exception("Error checking Telegram auth status")
        return jsonify({"authenticated": False, "error": str(e)})

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
                connection=ConnectionTcpIntermediate
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
                connection=ConnectionTcpIntermediate
            )
            await client.connect()
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
            final_session = client.session.save()
            await client.disconnect()
            return final_session
            
        final_session = loop.run_until_complete(sign_in_user())
        loop.close()
        
        rs.set("streamly:telegram_session", final_session)
        rs._execute("DEL", "streamly:telegram_temp_setup")
        return jsonify({"success": True})
    except Exception as e:
        log.exception("Failed to verify Telegram code")
        from ..security import json_error
        return json_error(400, "auth_failed", f"Verification failed: {str(e)}")

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
    
    chat_id = config.get("TELEGRAM_CHAT_ID") or "me"
    
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
            
        # File size cap check (default 2 GB, adjustable for Telegram Premium up to 4 GB)
        import os
        max_file_size_gb = float(os.getenv("TELEGRAM_MAX_FILE_SIZE_GB", "2.0"))
        if size >= max_file_size_gb * 1024 * 1024 * 1024:
            from ..security import json_error
            return json_error(400, "file_too_large", f"File size exceeds the {max_file_size_gb} GB limit for Telegram uploads.")
            
    except Exception as e:
        log.warning("Failed to fetch file details from Seedr: %s", e)
        from ..security import json_error
        return json_error(502, "provider_error", "Failed to retrieve file from Seedr")
        
    # Bandwidth limit check (warning at 4.0 GB / 900 GB, block at 4.5 GB / 1000 GB, tracking projected bandwidth)
    import datetime
    import os
    try:
        ym = datetime.datetime.now(datetime.UTC).strftime("%Y-%m")
    except AttributeError:
        ym = datetime.datetime.utcnow().strftime("%Y-%m")
        
    projected_bytes = get_projected_bandwidth(rs, ym, size)
    
    is_hf = "SPACE_ID" in os.environ
    if is_hf:
        limit_bytes = int(1000 * 1024 * 1024 * 1024)
        warning_bytes = int(900 * 1024 * 1024 * 1024)
        limit_label = "1000 GB"
        warning_label = "900 GB"
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
    
    task_id = str(uuid.uuid4())[:8]
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
            "error": None
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
    raw = rs.get(f"streamly:transfer_status:{task_id}")
    if not raw:
        raw = rs.get(f"streamly:telegram_task:{task_id}")
    if not raw:
        from ..security import json_error
        return json_error(404, "not_found", "Task not found")
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
    raw = rs.get(f"streamly:transfer_status:{task_id}")
    if not raw:
        return jsonify({"status": "IDLE"})
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
    
    projected_bytes = get_projected_bandwidth(rs, ym, 0)
    projected_gb = round(projected_bytes / (1024 * 1024 * 1024), 2)
    
    is_hf = "SPACE_ID" in os.environ
    limit_gb = 1000.0 if is_hf else 4.5
    
    destination = current_app.config.get("TELEGRAM_CHAT_ID") or "me"
    
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
        
    active_global = rs.get("streamly:active_transfer_global")
    if isinstance(active_global, bytes):
        active_global = active_global.decode("utf-8")
        
    # Case 1: The task is currently running in the active uploader thread
    if active_global == task_id:
        rs.set(f"streamly:cancel_request:{task_id}", "1", ex=300)
        return jsonify({"success": True, "message": "Cancellation request sent to active transfer"})
        
    # Case 2: The task is queued but not yet active
    removed = rs._execute("LREM", "streamly:transfer_queue", "0", task_id)
    
    # Set status to FAILED/CANCELLED in Redis
    rs._execute(
        "SET",
        f"streamly:transfer_status:{task_id}",
        _json.dumps({
            "progress": 0.0,
            "status": "FAILED",
            "error": "Cancelled by user",
            "filename": "file",
            "sent_bytes": 0,
            "total_bytes": 0
        }),
        "EX",
        "3600"
    )
    
    # Clean up keys for this task
    rs._execute("DEL", f"streamly:active_transfer:{sid}")
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
        
    rs._execute("DEL", "streamly:telegram_session")
    return jsonify({"success": True})



