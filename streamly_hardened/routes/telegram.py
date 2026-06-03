from __future__ import annotations

import logging
import time
import uuid
import json as _json
import threading
import requests
import asyncio
from flask import Blueprint, jsonify, current_app, request, Response
from telethon import TelegramClient
from telethon.sessions import StringSession
from ..auth_utils import current_client
from ..security import csrf_required, rate_limited, require_json_body, validate_positive_int

log = logging.getLogger(__name__)

telegram_bp = Blueprint("telegram", __name__)

class RequestsStreamWrapper:
    def __init__(self, response):
        self.response = response
        self.iterator = response.iter_content(chunk_size=512 * 1024)
        self.buffer = b""
        self.name = "file"

    def read(self, n):
        while len(self.buffer) < n:
            try:
                chunk = next(self.iterator)
                self.buffer += chunk
            except StopIteration:
                break
        data = self.buffer[:n]
        self.buffer = self.buffer[n:]
        return data

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
            status = "completed" if pct >= 100.0 else "uploading"
            self.rs._execute(
                "SET",
                f"streamly:telegram_task:{self.task_id}",
                _json.dumps({
                    "progress": pct,
                    "status": status,
                    "filename": self.filename,
                    "sent": sent_bytes,
                    "total": tot
                }),
                "EX",
                "3600"
            )

def get_telegram_client(session_str):
    api_id = current_app.config.get("TELEGRAM_API_ID")
    api_hash = current_app.config.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise ValueError("Telegram credentials missing in configuration")
    return TelegramClient(StringSession(session_str), api_id, api_hash)

def run_telethon_upload(rs, session_str, api_id, api_hash, file_url, chat_id, filename, size, task_id):
    # Establish distinct event loop for background thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def upload():
        try:
            client = TelegramClient(StringSession(session_str), api_id, api_hash)
            await client.connect()
            
            with requests.get(file_url, stream=True) as r:
                r.raise_for_status()
                wrapper = RequestsStreamWrapper(r)
                wrapper.name = filename
                
                tracker = ProgressTracker(rs, task_id, filename, size)
                
                # Pre-set status to uploading
                rs._execute(
                    "SET",
                    f"streamly:telegram_task:{task_id}",
                    _json.dumps({
                        "progress": 0.0,
                        "status": "uploading",
                        "filename": filename,
                        "sent": 0,
                        "total": size
                    }),
                    "EX",
                    "3600"
                )
                
                uploaded = await client.upload_file(
                    wrapper,
                    file_name=filename,
                    file_size=size,
                    progress_callback=tracker
                )
                
                # Explicitly cast chat_id to int if numeric
                target_chat = int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id
                await client.send_file(target_chat, uploaded, caption=f"File transferred: {filename}")
                
            rs._execute(
                "SET",
                f"streamly:telegram_task:{task_id}",
                _json.dumps({
                    "progress": 100.0,
                    "status": "completed",
                    "filename": filename
                }),
                "EX",
                "3600"
            )
            await client.disconnect()
        except Exception as e:
            log.exception("Telegram background upload failed")
            rs._execute(
                "SET",
                f"streamly:telegram_task:{task_id}",
                _json.dumps({
                    "progress": 0.0,
                    "status": "failed",
                    "error": str(e),
                    "filename": filename
                }),
                "EX",
                "3600"
            )
            
    loop.run_until_complete(upload())
    loop.close()

def start_telegram_upload(rs, session_str, api_id, api_hash, file_url, chat_id, filename, size):
    task_id = str(uuid.uuid4())[:8]
    rs._execute(
        "SET",
        f"streamly:telegram_task:{task_id}",
        _json.dumps({
            "progress": 0.0,
            "status": "starting",
            "filename": filename
        }),
        "EX",
        "3600"
    )
    t = threading.Thread(
        target=run_telethon_upload,
        args=(rs, session_str, api_id, api_hash, file_url, chat_id, filename, size, task_id)
    )
    t.daemon = True
    t.start()
    return task_id

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
    
    task_id = start_telegram_upload(rs, session_str, api_id, api_hash, file_url, chat_id, filename, size)
    return jsonify({"success": True, "task_id": task_id})

@telegram_bp.get("/api/telegram/task/<task_id>")
@rate_limited(cost=0.5)
def telegram_task_status(task_id):
    rs = getattr(current_app, "rs", None)
    if not rs:
        from ..security import json_error
        return json_error(503, "redis_unavailable", "Redis is required")
    raw = rs.get(f"streamly:telegram_task:{task_id}")
    if not raw:
        from ..security import json_error
        return json_error(404, "not_found", "Task not found")
    return Response(raw, mimetype="application/json")
