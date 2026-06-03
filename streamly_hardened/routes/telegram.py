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
from flask import Blueprint, jsonify, current_app, request, Response
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat
from ..auth_utils import current_client
from ..security import csrf_required, rate_limited, require_json_body, validate_positive_int, ensure_sid

log = logging.getLogger(__name__)

telegram_bp = Blueprint("telegram", __name__)

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
        async for chunk in response_stream.aiter_bytes(chunk_size=1 * 1024 * 1024):
            await queue.put(chunk)
        await queue.put(None)
    except Exception as pe:
        log.warning("Producer stream read error: %s", pe)
        await queue.put(pe)

class ProgressTracker:
    def __init__(self, rs, task_id, filename, total_bytes):
        self.rs = rs
        self.task_id = task_id
        self.filename = filename
        self.total_bytes = total_bytes
        self.last_update_time = 0.0
        self.last_pct = 0.0

    def __call__(self, sent_bytes, total_bytes):
        now = time.time()
        tot = total_bytes or self.total_bytes or 1
        pct = round((sent_bytes / tot) * 100, 1)
        if now - self.last_update_time >= 2.0 or pct >= 100.0 or pct - self.last_pct >= 5.0:
            self.last_update_time = now
            self.last_pct = pct
            status = "COMPLETED" if pct >= 100.0 else "UPLOADING"
            self.rs._execute(
                "SET",
                f"streamly:transfer_status:{self.task_id}",
                _json.dumps({
                    "progress": pct,
                    "status": status,
                    "filename": self.filename,
                    "sent_bytes": sent_bytes,
                    "total_bytes": tot,
                    "error": None
                }),
                "EX",
                "3600"
            )

async def parallel_upload_file(client, file_wrapper, file_size, filename, progress_callback, concurrency=3):
    part_size = 512 * 1024
    parts_count = (file_size + part_size - 1) // part_size
    file_id = secrets.randbits(63)
    
    sem = asyncio.Semaphore(concurrency)
    tasks = []
    uploaded_bytes = 0
    
    async def upload_part(part_index, data):
        async with sem:
            try:
                is_big = file_size > 10 * 1024 * 1024
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
                nonlocal uploaded_bytes
                uploaded_bytes += len(data)
                if progress_callback:
                    progress_callback(uploaded_bytes, file_size)
            except Exception as e:
                log.warning("Failed to upload part %d: %s", part_index, e)
                raise e

    for part_index in range(parts_count):
        chunk = await file_wrapper.read(part_size)
        if not chunk:
            break
        task = asyncio.create_task(upload_part(part_index, chunk))
        tasks.append(task)
        
    try:
        await asyncio.gather(*tasks)
    except Exception as gather_exc:
        # Cancel all pending tasks to prevent hangs on error
        for t in tasks:
            if not t.done():
                t.cancel()
        raise gather_exc
        
    if file_size > 10 * 1024 * 1024:
        return types.InputFileBig(id=file_id, parts=parts_count, name=filename)
    else:
        return types.InputFile(id=file_id, parts=parts_count, name=filename, md5_checksum="")

def get_telegram_client(session_str):
    api_id = current_app.config.get("TELEGRAM_API_ID")
    api_hash = current_app.config.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise ValueError("Telegram credentials missing in configuration")
    return TelegramClient(StringSession(session_str), api_id, api_hash)

async def validate_telegram_target(client, target_chat):
    try:
        resolved_chat = int(target_chat) if str(target_chat).lstrip("-").isdigit() else target_chat
        entity = await client.get_entity(resolved_chat)
    except Exception as e:
        raise ValueError(f"Could not find or access target chat/channel: {e}")

    if isinstance(entity, Channel):
        permissions = await client.get_permissions(entity)
        if entity.broadcast:
            if not (permissions.is_admin or permissions.is_creator):
                raise ValueError("You do not have permission to post in this broadcast channel (admin rights required).")
        else:
            if not permissions.send_messages:
                raise ValueError("You do not have permission to send messages here.")
    elif isinstance(entity, Chat):
        permissions = await client.get_permissions(entity)
        if not permissions.send_messages:
            raise ValueError("You do not have permission to send messages in this group.")

    return resolved_chat

def trigger_next_transfer(rs):
    active = rs.get("streamly:active_transfer_global")
    if active:
        log.info("Queue check: transfer %s is active. Skipping.", active)
        return
        
    task_id = rs._execute("RPOP", "streamly:transfer_queue")
    if not task_id:
        log.info("Queue check: no tasks in queue.")
        return
        
    args_raw = rs.get(f"streamly:task_args:{task_id}")
    if not args_raw:
        log.warning("Queue check: task %s has no arguments in Redis. Skipping.", task_id)
        trigger_next_transfer(rs)
        return
        
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
    except Exception as e:
        log.error("Queue check: failed to parse task args for %s: %s", task_id, e)
        rs._execute("DEL", f"streamly:task_args:{task_id}")
        trigger_next_transfer(rs)
        return
        
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
        try:
            client = TelegramClient(StringSession(session_str), api_id, api_hash)
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
            
            limits = httpx.Limits(max_keepalive_connections=2, max_connections=5)
            async with httpx.AsyncClient(limits=limits, timeout=60.0) as httpx_client:
                async with httpx_client.stream("GET", file_url) as r:
                    r.raise_for_status()
                    
                    exact_size = size
                    content_len_header = r.headers.get("content-length")
                    if content_len_header:
                        try:
                            exact_size = int(content_len_header)
                        except ValueError:
                            pass
                    
                    queue = asyncio.Queue(maxsize=5)
                    producer_task = asyncio.create_task(producer(r, queue))
                    
                    wrapper = AsyncQueueStreamWrapper(queue)
                    wrapper.name = filename
                    
                    tracker = ProgressTracker(rs, task_id, filename, exact_size)
                    
                    uploaded = await parallel_upload_file(
                        client,
                        wrapper,
                        file_size=exact_size,
                        filename=filename,
                        progress_callback=tracker,
                        concurrency=3
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
            client = TelegramClient(StringSession(), api_id, api_hash)
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
            client = TelegramClient(StringSession(temp_session), api_id, api_hash)
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
    chat_id = data.get("chat_id") or config.get("TELEGRAM_CHAT_ID") or "me"
    
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
            
    except Exception as e:
        log.warning("Failed to fetch file details from Seedr: %s", e)
        from ..security import json_error
        return json_error(502, "provider_error", "Failed to retrieve file from Seedr")
        
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
    
    return jsonify({"success": True, "task_id": task_id})

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

