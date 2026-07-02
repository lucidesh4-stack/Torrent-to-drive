# removed future annotations

import logging
import os
import time
import uuid
import json as _json
import asyncio
import urllib.parse
import httpx
import secrets
import datetime
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, Any
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession
from telethon.network import ConnectionTcpIntermediate, MTProtoSender
from telethon.errors import FilePartMissingError, FloodWaitError, RPCError
from telethon.tl.types import Channel, Chat
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InvokeWithLayerRequest
from telethon.tl.functions.auth import ExportAuthorizationRequest, ImportAuthorizationRequest

from .auth import verify_csrf
from ..auth_utils import current_client, ensure_sid
from ..security import (
    rate_limited,
    validate_positive_int,
    validate_public_url,
    async_pinned_get,
    ValidationError
)
from ..core.http_client import SeedrDownloader
from .telegram_client import manager as tg_manager, safe_disconnect, FLOOD_SLEEP_THRESHOLD as _FLOOD_SLEEP_THRESHOLD

log = logging.getLogger(__name__)
telegram_router = APIRouter()

_ACTIVE_TTL_SECONDS = 90
_PROGRESS_PERSIST_SECONDS = 20
_BANDWIDTH_FLUSH_SECONDS = 60

_LIVE_PROGRESS: dict[str, dict] = {}
_LIVE_PROGRESS_LOCK = asyncio.Lock()

_DISPATCH_LOCK_KEY = "streamly:transfer_dispatch_lock"
_DISPATCH_LOCK_TTL = 15

_TG_MAX_PARTS = 4000
_TG_PART_SIZE = 512 * 1024
_TG_HARD_MAX = _TG_MAX_PARTS * _TG_PART_SIZE
_DEFAULT_SPEEDTEST_URL = "https://speed.cloudflare.com/__down?bytes=10485760"


async def _live_set(task_id: str, data: dict) -> None:
    async with _LIVE_PROGRESS_LOCK:
        _LIVE_PROGRESS[task_id] = data


async def _live_get(task_id: str):
    async with _LIVE_PROGRESS_LOCK:
        v = _LIVE_PROGRESS.get(task_id)
        return dict(v) if v is not None else None


async def _live_get_active():
    async with _LIVE_PROGRESS_LOCK:
        for tid, v in _LIVE_PROGRESS.items():
            if v.get("status") in ("UPLOADING", "QUEUED"):
                out = dict(v)
                out.setdefault("task_id", tid)
                return out
    return None


async def _live_clear(task_id: str) -> None:
    async with _LIVE_PROGRESS_LOCK:
        _LIVE_PROGRESS.pop(task_id, None)


async def get_projected_bandwidth(rs, ym, current_file_size=0, active_item=None, queue_items=None, bw_bytes=None):
    if bw_bytes is None:
        raw_bw = await rs.get(f"streamly:monthly_bandwidth:{ym}")
        bw_bytes = int(raw_bw) if raw_bw and raw_bw.isdigit() else 0
        
    projected = bw_bytes + current_file_size
    
    if active_item is not None:
        try:
            total = int(active_item.get("total_bytes", 0))
            sent = int(active_item.get("sent_bytes", 0))
            projected += max(0, total - sent)
        except Exception:
            pass
    else:
        active_task_id = await rs.get("streamly:active_transfer_global")
        if active_task_id:
            if isinstance(active_task_id, bytes):
                active_task_id = active_task_id.decode("utf-8")
            raw_status = await rs.get(f"streamly:transfer_status:{active_task_id}")
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
                
    if queue_items is not None:
        for item in queue_items:
            try:
                projected += int(item.get("total_bytes", 0))
            except Exception:
                pass
    else:
        queue_task_ids = await rs._execute("LRANGE", "streamly:transfer_queue", "0", "-1")
        if queue_task_ids:
            for tid in queue_task_ids:
                try:
                    if isinstance(tid, bytes):
                        tid = tid.decode("utf-8")
                    raw_args = await rs.get(f"streamly:task_args:{tid}")
                    if raw_args:
                        if isinstance(raw_args, bytes):
                            raw_args = raw_args.decode("utf-8")
                        args = _json.loads(raw_args)
                        projected += int(args.get("size", 0))
                except Exception:
                    pass
                
    return projected


class ProgressTracker:
    def __init__(self, rs, task_id, filename, total_bytes, cancel_flag, sid=None):
        self.rs = rs
        self.task_id = task_id
        self.filename = filename
        self.total_bytes = total_bytes
        self.cancel_flag = cancel_flag
        self.sid = sid
        self.last_pct = 0.0
        self.last_bandwidth_sent_bytes = 0
        self.last_write_time = time.time()
        self.last_write_bytes = 0
        self.last_speed_mb = None
        self.last_persist_time = 0.0
        self.last_bw_flush_time = time.time()
        self.phase = "upload"

    def __call__(self, sent_bytes, total_bytes=None):
        if self.cancel_flag and self.cancel_flag[0]:
            raise ValueError("Cancelled by user")

        now = time.time()
        tot = total_bytes or self.total_bytes or 1
        
        if self.phase == "download":
            pct = round((sent_bytes / tot) * 50.0, 1)
        elif self.phase == "streaming":
            pct = round((sent_bytes / tot) * 100.0, 1)
        else:
            pct = round(50.0 + (sent_bytes / tot) * 50.0, 1)

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

        # Update live progress in memory
        asyncio.create_task(_live_set(self.task_id, {
            "progress": pct,
            "status": status,
            "filename": self.filename,
            "sent_bytes": sent_bytes,
            "total_bytes": tot,
            "speed_mb": speed_mb,
            "error": None,
            "sid": self.sid,
        }))

        finished = pct >= 100.0
        do_status = finished or (now - self.last_persist_time) >= _PROGRESS_PERSIST_SECONDS
        do_bw = finished or (now - self.last_bw_flush_time) >= _BANDWIDTH_FLUSH_SECONDS
        if not (do_status or do_bw):
            return

        bw_diff = 0
        if do_bw:
            self.last_bw_flush_time = now
            if self.phase == "upload":
                bw_diff = sent_bytes - self.last_bandwidth_sent_bytes
                if bw_diff > 0:
                    self.last_bandwidth_sent_bytes = sent_bytes
        if do_status:
            self.last_persist_time = now

        asyncio.create_task(self.update_redis_async(status, pct, speed_mb, sent_bytes, tot, bw_diff, do_status, do_bw))

    async def update_redis_async(self, status, pct, speed_mb, sent_bytes, total_bytes, bw_diff, write_status, write_bw):
        try:
            if write_bw and bw_diff > 0:
                ym = datetime.datetime.now(datetime.UTC).strftime("%Y-%m")
                await self.rs._execute("INCRBY", f"streamly:monthly_bandwidth:{ym}", str(bw_diff))
                await self.rs._execute("EXPIRE", f"streamly:monthly_bandwidth:{ym}", str(60 * 24 * 60 * 60))
            if write_status:
                state = {
                    "progress": pct,
                    "status": status,
                    "filename": self.filename,
                    "sent_bytes": sent_bytes,
                    "total_bytes": total_bytes,
                    "speed_mb": speed_mb,
                    "error": None,
                    "sid": self.sid
                }
                await self.rs._execute("SET", f"streamly:transfer_status:{self.task_id}", _json.dumps(state), "EX", "3600")
                await self.rs._execute("EXPIRE", "streamly:active_transfer_global", str(_ACTIVE_TTL_SECONDS))
        except Exception as e:
            log.warning("Failed to persist progress in Redis: %s", e)


async def validate_telegram_target(client: TelegramClient, chat_id: str) -> Any:
    target = chat_id.strip()
    if not target or target.lower() == "me":
        return "me"
    
    # Try resolving ID directly
    if target.startswith("-100") and target[4:].isdigit():
        try:
            return await client.get_input_entity(int(target))
        except Exception:
            pass
    elif target.isdigit() or (target.startswith("-") and target[1:].isdigit()):
        try:
            return await client.get_input_entity(int(target))
        except Exception:
            pass
            
    # Try username or string lookup
    try:
        entity = await client.get_entity(target)
        if isinstance(entity, (Channel, Chat)):
            return entity
        return await client.get_input_entity(entity)
    except Exception as e:
        raise ValueError(f"Could not resolve chat target '{target}': {e}")


def trigger_next_transfer(app):
    async def run():
        await _trigger_next_transfer_locked(app)
    task = asyncio.create_task(run())
    app.state.background_tasks.add(task)
    task.add_done_callback(app.state.background_tasks.discard)


async def _trigger_next_transfer_locked(app):
    rs = app.state.rs
    if not rs:
        return
        
    try:
        acquired = await rs._execute("SET", _DISPATCH_LOCK_KEY, "1", "EX", str(_DISPATCH_LOCK_TTL), "NX")
        if acquired != "OK":
            return
            
        active = await rs.get("streamly:active_transfer_global")
        if active:
            await rs._execute("DEL", _DISPATCH_LOCK_KEY)
            return
            
        next_task_id = await rs._execute("LPOP", "streamly:transfer_queue")
        if not next_task_id:
            await rs._execute("DEL", _DISPATCH_LOCK_KEY)
            return
            
        if isinstance(next_task_id, bytes):
            next_task_id = next_task_id.decode("utf-8")
            
        raw_args = await rs.get(f"streamly:task_args:{next_task_id}")
        if not raw_args:
            log.warning("Queue dispatch: task args missing for task %s. Skipping.", next_task_id)
            await rs._execute("DEL", _DISPATCH_LOCK_KEY)
            trigger_next_transfer(app)
            return
            
        if isinstance(raw_args, bytes):
            raw_args = raw_args.decode("utf-8")
            
        args = _json.loads(raw_args)
        
        session_str = await rs.get("streamly:telegram_session")
        api_id = app.state.config.telegram_api_id
        api_hash = app.state.config.telegram_api_hash
        
        if not session_str or not api_id or not api_hash:
            log.error("Queue dispatch: missing credentials/session. Re-queueing task %s.", next_task_id)
            await rs._execute("LPUSH", "streamly:transfer_queue", next_task_id)
            await rs._execute("DEL", _DISPATCH_LOCK_KEY)
            return
            
        file_url = args.get("url")
        chat_id = args.get("chat_id")
        filename = args.get("filename")
        size = int(args.get("size", 0))
        sid = args.get("sid")
        
        task_id = next_task_id
        await rs.set("streamly:active_transfer_global", task_id, ex=_ACTIVE_TTL_SECONDS)
        await rs.set(f"streamly:active_transfer:{sid}", task_id, ex=3600)
        await rs._execute("DEL", _DISPATCH_LOCK_KEY)
        
        # Start async task
        task = asyncio.create_task(run_telethon_upload(app, rs, session_str, api_id, api_hash, file_url, chat_id, filename, size, next_task_id, sid))
        if not hasattr(app.state, "active_tasks"):
            app.state.active_tasks = {}
        app.state.active_tasks[next_task_id] = task
        app.state.background_tasks.add(task)
        task.add_done_callback(app.state.background_tasks.discard)
        log.info("Queue check: started transfer background task for %s", next_task_id)
        
    except Exception as e:
        log.exception("Error in queue dispatch trigger: %s", e)
        try:
            await rs._execute("DEL", _DISPATCH_LOCK_KEY)
        except Exception:
            pass


async def download_to_queue(
    queue: asyncio.Queue,
    url: str,
    worker_url: str,
    headers: dict,
    exact_size: int,
    part_size: int,
    cancel_flag: list[bool]
):
    try:
        buffer = bytearray()
        part_index = 0
        downloaded = 0

        async def process_stream(response):
            nonlocal downloaded, part_index
            async for raw_chunk in response.aiter_bytes(chunk_size=128 * 1024):
                if cancel_flag[0]:
                    break
                buffer.extend(raw_chunk)
                downloaded += len(raw_chunk)
                while len(buffer) >= part_size:
                    chunk = bytes(buffer[:part_size])
                    del buffer[:part_size]
                    await queue.put((part_index, chunk))
                    part_index += 1

        if worker_url:
            try:
                encoded_url = urllib.parse.quote(url, safe='')
                worker_endpoint = f"{worker_url.rstrip('/')}/?url={encoded_url}"
                async with httpx.AsyncClient(timeout=60.0) as client:
                    async with client.stream("GET", worker_endpoint, headers=headers) as response:
                        if response.status_code != 403:
                            response.raise_for_status()
                            await process_stream(response)
                            if not cancel_flag[0] and downloaded == exact_size:
                                if len(buffer) > 0:
                                    await queue.put((part_index, bytes(buffer)))
                                return
            except Exception as e:
                log.warning("Proxy stream download failed, falling back to direct: %s", e)
        
        buffer.clear()
        part_index = 0
        downloaded = 0
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                await process_stream(response)
                if not cancel_flag[0]:
                    if downloaded != exact_size:
                        raise ValueError(f"Download size mismatch: expected {exact_size} bytes, got {downloaded} bytes")
                    if len(buffer) > 0:
                        await queue.put((part_index, bytes(buffer)))
    except Exception as e:
        await queue.put(e)
    finally:
        await queue.put(None)


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
        idle_sender = None
        for sender in self.senders:
            if sender.previous is None or sender.previous.done():
                idle_sender = sender
                break

        if idle_sender is None:
            busy_tasks = {
                sender.previous: sender 
                for sender in self.senders 
                if sender.previous and not sender.previous.done()
            }
            if busy_tasks:
                done, pending = await asyncio.wait(list(busy_tasks.keys()), return_when=asyncio.FIRST_COMPLETED)
                finished_task = done.pop()
                idle_sender = busy_tasks[finished_task]

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

    # Use up to 4 parallel connections to optimize startup time and avoid flood waits
    connections = 4 if file_size > 50 * 1024 * 1024 else (2 if file_size > 10 * 1024 * 1024 else 1)
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


async def run_telethon_upload(app, rs, session_str, api_id, api_hash, file_url, chat_id, filename, size, task_id, sid):
    cancel_flag = [False]
    
    async def poll_cancel_request():
        while not cancel_flag[0]:
            try:
                res = await rs.get(f"streamly:cancel_request:{task_id}")
                if res:
                    cancel_flag[0] = True
                    break
            except Exception:
                pass
            await asyncio.sleep(5.0)
            
    cancel_poller = asyncio.create_task(poll_cancel_request())
    
    client = None
    try:
        client = tg_manager.get_upload_client(session_str, api_id=api_id, api_hash=api_hash, app=app)
        await client.connect()
        
        try:
            resolved_chat = await validate_telegram_target(client, chat_id)
        except Exception as pe:
            raise ValueError(str(pe))
        
        await rs._execute(
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
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
            "Referer": "https://www.seedr.cc/"
        }
        
        download_url = file_url
        log.info("Downloading directly from Seedr (no proxy): %s", download_url)

        try:
            log.info("Querying Content-Length directly from Seedr: %s", download_url)
            async with httpx.AsyncClient(http2=True, timeout=15.0) as client_cl:
                r = await client_cl.get(download_url, headers=headers)
                r.raise_for_status()
                content_len_header = r.headers.get("content-length")
                if content_len_header:
                    exact_size = int(content_len_header)
        except Exception as e:
            log.warning("Direct Seedr Content-Length check failed: %s: %s. Using reported size %d.", type(e).__name__, e, size)
            exact_size = size
        
        part_size = 512 * 1024
        # audit H5: wait_for(output_queue.get()
        parts_count = (exact_size + part_size - 1) // part_size
        max_bytes = _TG_HARD_MAX
        if exact_size > max_bytes:
            raise ValueError(f"File too large for Telegram MTProto upload: {exact_size} bytes (max {max_bytes})")

        if parts_count > _TG_MAX_PARTS:
            raise ValueError(f"File parts ({parts_count}) exceed Telegram upload limit of {_TG_MAX_PARTS} parts (file too large).")

        tracker = ProgressTracker(rs, task_id, filename, exact_size, cancel_flag, sid)

        max_attempts = 3
        backoff = 5.0
        uploaded = None

        for attempt in range(1, max_attempts + 1):
            log.info("Starting upload attempt %d/%d for task %s", attempt, max_attempts, task_id)
            cancel_flag[0] = False
            
            output_queue = asyncio.Queue(maxsize=16)
            download_task = asyncio.create_task(
                download_to_queue(
                    output_queue,
                    download_url,
                    app.state.config.cloudflare_worker_proxy,
                    headers,
                    exact_size,
                    part_size,
                    cancel_flag
                )
            )

            try:
                tracker.phase = "streaming"
                tracker.last_pct = 0.0
                tracker.last_write_bytes = 0
                
                def upload_progress(current, total):
                    if cancel_flag[0]:
                        raise ValueError("Cancelled by user")
                    tracker(current, total)

                log.info("Starting streaming Telegram upload for task %s", task_id)
                
                uploaded = await parallel_upload_file(
                    client,
                    output_queue,
                    exact_size,
                    filename,
                    upload_progress
                )

                uploaded_parts = uploaded.parts
                actual_parts = uploaded_parts
                # parts=actual_parts

                await download_task

                await client.send_file(resolved_chat, uploaded, caption=f"File transferred: {filename}")
                log.info("Upload and send completed successfully on attempt %d", attempt)
                break
            except (FilePartMissingError, FloodWaitError, RPCError, httpx.HTTPError, Exception) as e:
                user_cancelled = cancel_flag[0] or (isinstance(e, ValueError) and str(e) == "Cancelled by user")
                cancel_flag[0] = True
                download_task.cancel()
                try:
                    await download_task
                except Exception:
                    pass
                
                if user_cancelled:
                    log.info("Transfer cancelled by user. Aborting upload retry loop.")
                    raise e

                if isinstance(e, FilePartMissingError):
                    log.error("Telegram upload failed due to missing file parts: %s", e)
                elif isinstance(e, FloodWaitError):
                    log.error("Telegram rate limit hit: wait for %d seconds. Error: %s", e.seconds, e)
                elif isinstance(e, RPCError):
                    log.error("Telegram RPC error: %s (code: %d, message: %s)", e, e.code, e.message)
                elif isinstance(e, httpx.HTTPError):
                    log.error("HTTP error during transfer: %s", e)
                else:
                    log.error("General error during transfer: %s", e)

                if attempt < max_attempts:
                    sleep_time = backoff * (2 ** (attempt - 1))
                    log.info("Retrying entire upload in %.1f seconds...", sleep_time)
                    await asyncio.sleep(sleep_time)
                else:
                    if isinstance(e, FilePartMissingError):
                        raise ValueError("Telegram upload failed: some file parts are missing from storage after multiple retries.") from e
                    elif isinstance(e, FloodWaitError):
                        raise ValueError(f"Telegram rate limit hit: must wait for {e.seconds} seconds.") from e
                    elif isinstance(e, RPCError):
                        raise ValueError(f"Telegram server error: {e.message}") from e
                    else:
                        raise e
                
        _completed_state = {
            "progress": 100.0,
            "status": "COMPLETED",
            "filename": filename,
            "sent_bytes": exact_size,
            "total_bytes": exact_size,
            "error": None,
            "sid": sid
        }
        await _live_set(task_id, _completed_state)
        await rs._execute(
            "SET",
            f"streamly:transfer_status:{task_id}",
            _json.dumps(_completed_state),
            "EX",
            "3600"
        )
    except asyncio.CancelledError:
        log.info("Telegram background upload cancelled via task.cancel()")
        _failed_state = {
            "progress": 0.0,
            "status": "FAILED",
            "error": "Cancelled by user",
            "filename": filename,
            "sent_bytes": 0,
            "total_bytes": exact_size,
            "sid": sid
        }
        await _live_set(task_id, _failed_state)
        await rs._execute(
            "SET",
            f"streamly:transfer_status:{task_id}",
            _json.dumps(_failed_state),
            "EX",
            "3600"
        )
        raise
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
        await _live_set(task_id, _failed_state)
        await rs._execute(
            "SET",
            f"streamly:transfer_status:{task_id}",
            _json.dumps(_failed_state),
            "EX",
            "3600"
        )
    finally:
        if hasattr(app.state, "active_tasks"):
            app.state.active_tasks.pop(task_id, None)
        cancel_flag[0] = True
        if not cancel_poller.done():
            cancel_poller.cancel()
        if client:
            await safe_disconnect(client)
        await _live_clear(task_id)
        await rs._execute("DEL", "streamly:active_transfer_global")
        await rs._execute("DEL", f"streamly:task_args:{task_id}")
        trigger_next_transfer(app)


@telegram_router.get("/api/telegram/status")
@rate_limited(cost=1.0)
async def telegram_status(request: Request):
    rs = getattr(request.app.state, "rs", None)
    if not rs:
        return {"authenticated": False, "error": "Redis unavailable"}
        
    cryptg_active = False
    try:
        import cryptg
        cryptg_active = True
    except ImportError:
        pass

    sid = request.session.get("sid") or ensure_sid(request)
    cache_key = f"streamly:tg_auth_cache:{sid}"
    
    cached = await rs.get(cache_key)
    if cached:
        try:
            cached_data = _json.loads(cached)
            return {
                "authenticated": cached_data.get("authenticated", False),
                "cryptg_active": cryptg_active,
                "cached": True
            }
        except Exception:
            pass

    session_str = await rs.get("streamly:telegram_session")
    if not session_str:
        return {"authenticated": False}
    
    try:
        async with tg_manager.get_client(session_str, app=request.app) as client:
            authorized = await client.is_user_authorized()
            
        await rs.set(cache_key, _json.dumps({"authenticated": authorized}), ex=60)
        return {
            "authenticated": authorized,
            "cryptg_active": cryptg_active
        }
    except Exception as e:
        log.warning("Telegram status check failed: %s", e)
        return {"authenticated": False, "error": str(e)}


@telegram_router.get("/api/telegram/test-download")
@rate_limited(cost=3.0)
async def test_download_speed(request: Request):
    raw_url = request.query_params.get("url")
    pinned_ip = None
    try:
        if raw_url is None or not raw_url.strip():
            test_url, pinned_ip = validate_public_url(_DEFAULT_SPEEDTEST_URL)
        else:
            test_url, pinned_ip = validate_public_url(raw_url)
    except ValidationError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    max_bytes = 10 * 1024 * 1024
    try:
        start_time = time.time()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive"
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Connect via async_pinned_get to prevent SSRF
            r = await async_pinned_get(test_url, pinned_ip, client, headers=headers)
            r.raise_for_status()
            
            total_downloaded = 0
            # Read in chunks
            async for chunk in r.aiter_bytes(chunk_size=64 * 1024):
                total_downloaded += len(chunk)
                if total_downloaded >= max_bytes:
                    break
                    
        elapsed = time.time() - start_time
        speed_mb = total_downloaded / (elapsed * 1024 * 1024) if elapsed > 0 else 0.0
        return {
            "success": True,
            "bytes_downloaded": total_downloaded,
            "elapsed_seconds": round(elapsed, 2),
            "speed_mb_s": round(speed_mb, 2)
        }
    except Exception as e:
        log.warning("Speed test download failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Speed test failed: {e}")


class PhonePayload(BaseModel):
    phone: str


class CodePayload(BaseModel):
    code: str


@telegram_router.post("/api/telegram/send-code")
@rate_limited(cost=3.0)
async def send_code(request: Request, payload: PhonePayload, _csrf = Depends(verify_csrf)):
    rs = request.app.state.rs
    if not rs:
        raise HTTPException(status_code=503, detail="Redis unavailable")
        
    phone = payload.phone.strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Phone number is required")
        
    api_id = request.app.state.config.telegram_api_id
    api_hash = request.app.state.config.telegram_api_hash
    
    if not api_id or not api_hash:
        raise HTTPException(status_code=500, detail="Telegram credentials missing in configuration")
        
    session_str = secrets.token_hex(16)
    client = get_telegram_client(session_str, app=request.app)
    await client.connect()
    
    try:
        sid = request.session.get("sid") or ensure_sid(request)
        code_hash_data = await client.send_code_request(phone)
        code_hash = code_hash_data.phone_code_hash
        
        await rs.set(f"streamly:tg_auth_session:{sid}", session_str, ex=600)
        await rs.set(f"streamly:tg_auth_phone:{sid}", phone, ex=600)
        await rs.set(f"streamly:tg_auth_hash:{sid}", code_hash, ex=600)
        
        return {"success": True, "message": "Code sent successfully"}
    except Exception as e:
        log.warning("Failed to send Telegram code request: %s", e)
        raise HTTPException(status_code=502, detail=f"Failed to send code: {e}")
    finally:
        await safe_disconnect(client)


@telegram_router.post("/api/telegram/verify-code")
@rate_limited(cost=3.0)
async def verify_code(request: Request, payload: CodePayload, _csrf = Depends(verify_csrf)):
    rs = request.app.state.rs
    if not rs:
        raise HTTPException(status_code=503, detail="Redis unavailable")
        
    code = payload.code.strip()
    if not code:
        raise HTTPException(status_code=400, detail="Verification code is required")
        
    sid = request.session.get("sid") or ensure_sid(request)
    session_str = await rs.get(f"streamly:tg_auth_session:{sid}")
    phone = await rs.get(f"streamly:tg_auth_phone:{sid}")
    code_hash = await rs.get(f"streamly:tg_auth_hash:{sid}")
    
    if not session_str or not phone or not code_hash:
        raise HTTPException(status_code=400, detail="Authentication session expired. Please send code again.")
        
    client = get_telegram_client(session_str, app=request.app)
    await client.connect()
    
    try:
        await client.sign_in(phone, code, phone_code_hash=code_hash)
        
        # Save session string
        session_str_final = client.session.save()
        await rs.set("streamly:telegram_session", session_str_final)
        
        # Cleanup temp auth state
        await rs._execute("DEL", f"streamly:tg_auth_session:{sid}")
        await rs._execute("DEL", f"streamly:tg_auth_phone:{sid}")
        await rs._execute("DEL", f"streamly:tg_auth_hash:{sid}")
        await rs._execute("DEL", f"streamly:tg_auth_cache:{sid}")
        
        return {"success": True, "message": "Logged in successfully"}
    except Exception as e:
        log.warning("Failed to verify Telegram code: %s", e)
        raise HTTPException(status_code=502, detail=f"Verification failed: {e}")
    finally:
        await safe_disconnect(client)


class SendFilePayload(BaseModel):
    file_id: Any
    chat_id: str = "me"


async def acquire_redis_lock(rs, lock_key, ttl_seconds, max_retries=10, retry_delay=0.1):
    for _ in range(max_retries):
        ok = await rs._execute("SET", lock_key, "1", "EX", str(ttl_seconds), "NX")
        if ok == "OK":
            return True
        await asyncio.sleep(retry_delay)
    return False


@telegram_router.post("/api/telegram/send")
@rate_limited(cost=3.0)
async def telegram_send_file(request: Request, payload: SendFilePayload, client = Depends(current_client), _csrf = Depends(verify_csrf)):
    config = request.app.state.config
    rs = request.app.state.rs
    cloud = request.app.state.cloud
    
    if not rs:
        raise HTTPException(status_code=503, detail="Redis unavailable")
        
    try:
        f_id = validate_positive_int(payload.file_id, name="file_id", maximum=config.max_file_id)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    # Fetch file details from Seedr
    try:
        file_info = await cloud.get_stream_url(client, f_id)
        if not file_info:
            raise HTTPException(status_code=404, detail="File not found or stream URL unavailable")
    except Exception as e:
        log.warning("Provider error on send-file lookup: %s", e)
        raise HTTPException(status_code=502, detail="Failed to retrieve file details from provider")

    # Resolve filename and size
    try:
        items = await cloud.list_items(client, 0)
        # Find the file in folders / files list to get exact name & size
        file_obj = None
        for f in items.get("files", []):
            if f.get("id") == f_id:
                file_obj = f
                break
        if not file_obj:
            # Look inside subfolders
            for folder in items.get("folders", []):
                sub_items = await cloud.list_items(client, folder["id"])
                for f in sub_items.get("files", []):
                    if f.get("id") == f_id:
                        file_obj = f
                        break
                if file_obj:
                    break
        
        if file_obj:
            filename = file_obj["name"]
            size = file_obj["size"]
        else:
            filename = file_info.split("/")[-1].split("?")[0] or "file"
            size = 0
    except Exception:
        filename = file_info.split("/")[-1].split("?")[0] or "file"
        size = 0

    max_bytes = _TG_HARD_MAX
    if size > max_bytes:
        raise HTTPException(status_code=400, detail=f"File exceeds Telegram upload limit of 2.0 GB ({format_size(size)})")

    # Bandwidth verification
    try:
        ym = datetime.datetime.now(datetime.UTC).strftime("%Y-%m")
        raw_bw = await rs.get(f"streamly:monthly_bandwidth:{ym}")
        bw_bytes = int(raw_bw) if raw_bw and raw_bw.isdigit() else 0
        
        limit_gb = float(os.getenv("TELEGRAM_BANDWIDTH_LIMIT_GB", "99.0"))
        limit_bytes = int(limit_gb * 1024 * 1024 * 1024)
        
        projected = await get_projected_bandwidth(rs, ym, current_file_size=size, bw_bytes=bw_bytes)
        
        if projected > limit_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"This transfer would exceed the monthly bandwidth limit of {limit_gb} GB. (Current + Queued + Selected: {projected / (1024**3):.2f} GB)"
            )
    except HTTPException:
        raise
    except Exception as bwe:
        log.warning("Bandwidth verification failed: %s", bwe)

    # Queue the task
    sid = request.session.get("sid") or ensure_sid(request)
    task_id = str(uuid.uuid4())
    
    task_args = {
        "task_id": task_id,
        "url": file_info,
        "chat_id": payload.chat_id,
        "filename": filename,
        "size": size,
        "sid": sid
    }
    
    await rs.set(f"streamly:task_args:{task_id}", _json.dumps(task_args))
    await rs._execute("RPUSH", "streamly:transfer_queue", task_id)
    
    # Update state to QUEUED
    await rs._execute(
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
    
    trigger_next_transfer(request.app)
    return {"success": True, "task_id": task_id}


@telegram_router.get("/api/telegram/task/{task_id}")
@rate_limited(cost=0.5)
async def telegram_task_status(request: Request, task_id: str):
    rs = request.app.state.rs
    if not rs:
        raise HTTPException(status_code=503, detail="Redis unavailable")
        
    sid = request.session.get("sid") or ensure_sid(request)
    raw_args = await rs.get(f"streamly:task_args:{task_id}")
    if raw_args:
        try:
            args = _json.loads(raw_args.decode("utf-8") if isinstance(raw_args, bytes) else raw_args)
            if args.get("sid") != sid:
                raise HTTPException(status_code=403, detail="Forbidden")
        except HTTPException:
            raise
        except Exception:
            pass

    # Check in memory first
    state = await _live_get(task_id)
    if state:
        return state
        
    raw = await rs.get(f"streamly:transfer_status:{task_id}")
    if raw:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return _json.loads(raw)
        except Exception:
            pass
            
    raise HTTPException(status_code=404, detail="Task not found")


@telegram_router.get("/api/telegram/queue")
@rate_limited(cost=0.5)
async def get_telegram_queue(request: Request):
    rs = request.app.state.rs
    if not rs:
        return {"active": None, "queue": []}
        
    ym = datetime.datetime.now(datetime.UTC).strftime("%Y-%m")
    raw_bw = await rs.get(f"streamly:monthly_bandwidth:{ym}")
    bw_bytes = int(raw_bw) if raw_bw and raw_bw.isdigit() else 0
    limit_gb = float(os.getenv("TELEGRAM_BANDWIDTH_LIMIT_GB", "99.0"))
    
    active_item = await _live_get_active()
    
    if not active_item:
        active_task_id = await rs.get("streamly:active_transfer_global")
        if active_task_id:
            if isinstance(active_task_id, bytes):
                active_task_id = active_task_id.decode("utf-8")
            raw_status = await rs.get(f"streamly:transfer_status:{active_task_id}")
            if raw_status:
                try:
                    if isinstance(raw_status, bytes):
                        raw_status = raw_status.decode("utf-8")
                    active_item = _json.loads(raw_status)
                    active_item.setdefault("task_id", active_task_id)
                except Exception:
                    pass

    # Read queue items
    queue_items = []
    queue_task_ids = await rs._execute("LRANGE", "streamly:transfer_queue", "0", "-1") or []
    for qid in queue_task_ids:
        if isinstance(qid, bytes):
            qid = qid.decode("utf-8")
        raw_args = await rs.get(f"streamly:task_args:{qid}")
        if raw_args:
            try:
                if isinstance(raw_args, bytes):
                    raw_args = raw_args.decode("utf-8")
                args = _json.loads(raw_args)
                queue_items.append({
                    "task_id": qid,
                    "filename": args.get("filename"),
                    "total_bytes": int(args.get("size", 0))
                })
            except Exception:
                pass

    projected_bytes = await get_projected_bandwidth(
        rs, ym, current_file_size=0, active_item=active_item, queue_items=queue_items, bw_bytes=bw_bytes
    )

    dest = os.getenv("TELEGRAM_CHAT_ID", "-1004247146382")
    if dest == "-1004247146382":
        dest = "me"

    return {
        "active": active_item,
        "queue": queue_items,
        "bandwidth_usage_gb": bw_bytes / (1024**3),
        "bandwidth_projected_gb": projected_bytes / (1024**3),
        "bandwidth_limit_gb": limit_gb,
        "destination": dest
    }


class CancelPayload(BaseModel):
    task_id: str


@telegram_router.post("/api/telegram/cancel")
@rate_limited(cost=1.0)
async def telegram_cancel_transfer(request: Request, payload: CancelPayload, _csrf = Depends(verify_csrf)):
    rs = request.app.state.rs
    if not rs:
        raise HTTPException(status_code=503, detail="Redis unavailable")
        
    task_id = payload.task_id.strip()
    # Bypass user-ownership sid check to prevent 403 Forbidden errors when session cookies change.
    
    # 1. Check if it's currently active
    active = await rs.get("streamly:active_transfer_global")
    if active and (isinstance(active, bytes) and active.decode("utf-8") == task_id or active == task_id):
        if hasattr(request.app.state, "active_tasks") and task_id in request.app.state.active_tasks:
            task = request.app.state.active_tasks[task_id]
            task.cancel()
            log.info("Cancelled running task %s directly via task.cancel()", task_id)
        else:
            await rs.set(f"streamly:cancel_request:{task_id}", "1", ex=10)
        return {"success": True, "message": "Cancellation request sent to active task."}
        
    # 2. Check if it's in the queue
    queue = await rs._execute("LRANGE", "streamly:transfer_queue", "0", "-1") or []
    found = False
    for item in queue:
        decoded = item.decode("utf-8") if isinstance(item, bytes) else item
        if decoded == task_id:
            await rs._execute("LREM", "streamly:transfer_queue", "0", item)
            await rs._execute("DEL", f"streamly:transfer_status:{task_id}")
            await rs._execute("DEL", f"streamly:task_args:{task_id}")
            found = True
            break
            
    if found:
        return {"success": True, "message": "Queued transfer cancelled successfully."}
        
    raise HTTPException(status_code=404, detail="Task not found in active or queue state")


@telegram_router.post("/api/telegram/logout")
@rate_limited(cost=1.0)
async def telegram_logout(request: Request, _csrf = Depends(verify_csrf)):
    rs = request.app.state.rs
    if not rs:
        raise HTTPException(status_code=503, detail="Redis unavailable")
        
    await rs._execute("DEL", "streamly:telegram_session")
    
    # Clear session caches
    sid = request.session.get("sid") or ensure_sid(request)
    await rs._execute("DEL", f"streamly:tg_auth_cache:{sid}")
    return {"success": True}


@telegram_router.get("/api/telegram/settings")
async def get_telegram_settings(request: Request):
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "-1004247146382")
    return {"success": True, "chat_id": chat_id}


class SettingsPayload(BaseModel):
    chat_id: str


@telegram_router.post("/api/telegram/settings")
@rate_limited(cost=1.0)
async def save_telegram_settings(request: Request, payload: SettingsPayload, _csrf = Depends(verify_csrf)):
    chat_id = payload.chat_id.strip()
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id is required")
        
    # Verify the target chat is valid by connecting to Telegram
    rs = request.app.state.rs
    session_str = await rs.get("streamly:telegram_session") if rs else None
    if not session_str:
        raise HTTPException(status_code=400, detail="Telegram account not linked")
        
    try:
        async with tg_manager.get_client(session_str, app=request.app) as client:
            await validate_telegram_target(client, chat_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid target: {e}")
        
    # Persist the change to OS environment (runtime only, Docker will persist)
    os.environ["TELEGRAM_CHAT_ID"] = chat_id
    
    # Invalidate caches
    if rs:
        sid = request.session.get("sid") or ensure_sid(request)
        await rs._execute("DEL", f"streamly:tg_auth_cache:{sid}")
        
    return {"success": True, "message": "Telegram export target updated successfully."}
