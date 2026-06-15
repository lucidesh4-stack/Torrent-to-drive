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

    connections = 12 if file_size > 50 * 1024 * 1024 else (8 if file_size > 10 * 1024 * 1024 else 4)
    connections = min(connections, parts_count)

    uploader = ParallelUploader(client, progress_callback=progress_callback, file_size=file_size)
    await uploader.init_upload(file_id, file_size, part_size, connections)

    try:
        uploaded_parts = 0
        while uploaded_parts < parts_count:
            for sender in uploader.senders:
                if sender.exception:
                    raise sender.exception

            item = await output_queue.get()
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
        connection=ConnectionTcpIntermediate
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

            # Query Content-Length using a streaming request
            r = requests.get(download_url, stream=True, timeout=30.0, headers=headers)
            r.raise_for_status()
            content_len_header = r.headers.get("content-length")
            if content_len_header:
                try:
                    exact_size = int(content_len_header)
                except ValueError:
                    pass
            r.close()
            
            import queue as py_queue
            index_queue = py_queue.Queue()
            part_size = 512 * 1024
            parts_count = (exact_size + part_size - 1) // part_size
            for idx in range(parts_count):
                index_queue.put(idx)

            output_queue = asyncio.Queue(maxsize=16)
            active_threads = 2
            threads_lock = threading.Lock()

            def safe_put(item):
                if loop.is_closed():
                    return
                try:
                    fut = asyncio.run_coroutine_threadsafe(output_queue.put(item), loop)
                    fut.result()
                except (RuntimeError, AssertionError):
                    pass

            def download_worker(worker_id):
                nonlocal active_threads
                try:
                    start_time = time.time()
                    downloaded_bytes = 0
                    session = requests.Session()
                    while not cancel_flag[0]:
                        try:
                            part_index = index_queue.get_nowait()
                        except py_queue.Empty:
                            break

                        start = part_index * part_size
                        end = min(exact_size - 1, (part_index + 1) * part_size - 1)
                        if start > end:
                            index_queue.task_done()
                            continue

                        headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "Accept": "*/*",
                            "Range": f"bytes={start}-{end}",
                            "Connection": "keep-alive"
                        }
                        
                        r = session.get(download_url, headers=headers, timeout=60.0)
                        r.raise_for_status()
                        chunk = r.content
                        
                        downloaded_bytes += len(chunk)
                        
                        if part_index % 10 == 0:
                            elapsed = time.time() - start_time
                            if elapsed > 0:
                                speed = downloaded_bytes / (elapsed * 1024 * 1024)
                                log.info("Downloader %d speed: %.2f MB/s", worker_id, speed)

                        safe_put((part_index, chunk))
                        index_queue.task_done()
                        
                except Exception as de:
                    log.warning("Background download worker %d error: %s", worker_id, de)
                    safe_put(de)
                finally:
                    with threads_lock:
                        active_threads -= 1
                        if active_threads == 0:
                            safe_put(None)

            download_threads = []
            for w_id in range(2):
                t = threading.Thread(target=download_worker, args=(w_id,), name=f"seedr-downloader-{w_id}")
                t.daemon = True
                t.start()
                download_threads.append(t)
            
            tracker = ProgressTracker(rs, task_id, filename, exact_size, loop, cancel_flag)
            
            uploaded = await parallel_upload_file(
                client,
                output_queue,
                file_size=exact_size,
                filename=filename,
                progress_callback=tracker
            )
            
            for t in download_threads:
                await loop.run_in_executor(None, t.join)
            
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
    
    cryptg_active = False
    try:
        import cryptg
        cryptg_active = True
    except ImportError:
        pass

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


@telegram_bp.get("/api/telegram/test-download")
def test_download_speed():
    test_url = request.args.get("url", "https://speed.cloudflare.com/__down?bytes=10485760")
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
        r = requests.get(test_url, stream=True, timeout=30.0, headers=headers)
        r.raise_for_status()
        total_downloaded = 0
        for chunk in r.iter_content(chunk_size=64 * 1024):
            total_downloaded += len(chunk)
            if total_downloaded >= max_bytes:
                break
        elapsed = time.time() - start_time
        speed_mb = total_downloaded / (elapsed * 1024 * 1024)
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
        
    # Bandwidth limit check (warning at 4.0 GB / 90 GB, block at 4.5 GB / 99 GB, tracking projected bandwidth)
    import datetime
    import os
    try:
        ym = datetime.datetime.now(datetime.UTC).strftime("%Y-%m")
    except AttributeError:
        ym = datetime.datetime.utcnow().strftime("%Y-%m")
        
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



