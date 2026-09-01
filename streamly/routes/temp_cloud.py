"""
Streamly Temp Cloud (24h Ephemeral Drive) Routes
Provides high-speed local disk cloud storage with 24-hour auto-pruning,
1DM direct URL multi-threaded downloads, and archive extraction.
"""

from __future__ import annotations

import os
import time
import shutil
import logging
import asyncio
from typing import Optional
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, field_validator

from .auth import verify_csrf
from ..auth_utils import ensure_sid
from ..security import rate_limited, validate_public_url
from ..core.direct_downloader import Direct1DMDownloader
from ..core.archive_extractor import is_archive, safe_extract_archive
from ..core.bunkr_engine import is_bunkr_url, BunkrSequentialDownloader

log = logging.getLogger(__name__)
temp_cloud_router = APIRouter()

TEMP_CLOUD_ROOT = os.environ.get("TEMP_CLOUD_DIR", "/tmp/streamly_temp_cloud/storage")
DEFAULT_EXPIRY_SECONDS = 86400 # 24 hours
TEMP_STORAGE_QUOTA_GB = 50.0

def get_user_temp_dir(sid: str = "") -> str:
    """Unified instance temp cloud storage root so files persist across browser sessions for 24h."""
    os.makedirs(TEMP_CLOUD_ROOT, exist_ok=True)
    return TEMP_CLOUD_ROOT

def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

def _format_expiry(expiry_sec: int) -> str:
    if expiry_sec <= 0:
        return "Expired"
    hours = expiry_sec // 3600
    minutes = (expiry_sec % 3600) // 60
    if hours > 0:
        return f"Expires in {hours}h {minutes:02d}m"
    return f"Expires in {minutes}m"

async def auto_prune_expired_files(user_dir: str):
    """Removes files and directories that have exceeded the 24-hour expiration threshold."""
    now = time.time()
    try:
        for root, dirs, files in os.walk(user_dir, topdown=False):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    stat = os.stat(fpath)
                    if now - stat.st_mtime > DEFAULT_EXPIRY_SECONDS:
                        os.remove(fpath)
                        log.info("Auto-pruned expired temp file: %s", fpath)
                except Exception:
                    pass
            for dname in dirs:
                dpath = os.path.join(root, dname)
                try:
                    if not os.listdir(dpath): # Clean up empty extracted directories
                        os.rmdir(dpath)
                except Exception:
                    pass
    except Exception as e:
        log.debug("Auto-prune check error: %s", e)


def _compute_used_size(user_dir: str) -> int:
    used = 0
    for root, _, files in os.walk(user_dir):
        for f in files:
            try:
                used += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    return used


@temp_cloud_router.get("/api/temp_cloud/storage")
@rate_limited(cost=0.5)
async def temp_cloud_storage(request: Request):
    """Returns NVMe / local disk storage metrics for the top storage bar."""
    user_dir = get_user_temp_dir()
    
    # Calculate actual used storage in worker threadpool (non-blocking)
    user_used = await asyncio.to_thread(_compute_used_size, user_dir)

    quota_bytes = int(TEMP_STORAGE_QUOTA_GB * (1024 ** 3))
    user_used_gb = round(user_used / (1024 ** 3), 2)
    pct = round((user_used / quota_bytes) * 100, 1) if quota_bytes > 0 else 0.0
    pct = min(100.0, pct)

    return {
        "storage_label": "Temp NVMe Storage",
        "storage_metrics": f"{user_used_gb:.2f} / {TEMP_STORAGE_QUOTA_GB:.1f} GB • {pct:.0f}%",
        "storage_subtext": "Auto-expires in 24h",
        "percent": pct,
        "user_used_bytes": user_used,
        "total_bytes": quota_bytes,
        "free_bytes": max(0, quota_bytes - user_used)
    }


@temp_cloud_router.get("/api/temp_cloud/list")
@rate_limited(cost=1.0)
async def temp_cloud_list(request: Request, folder_id: Optional[str] = None):
    """Lists files and folders inside Temp Cloud matching Seedr schema."""
    sid = request.session.get("sid") or ensure_sid(request)
    user_dir = get_user_temp_dir(sid)
    
    await auto_prune_expired_files(user_dir)

    target_dir = user_dir
    rel_folder = ""
    if folder_id and folder_id.strip() and folder_id != "0" and folder_id != "root":
        # Ensure path security (cannot escape user directory)
        candidate = os.path.realpath(os.path.join(user_dir, folder_id.lstrip("/\\")))
        if candidate.startswith(os.path.realpath(user_dir)):
            target_dir = candidate
            rel_folder = os.path.relpath(target_dir, user_dir)

    if not os.path.exists(target_dir):
        return {"folders": [], "files": [], "total_size": 0}

    folders_list = []
    files_list = []
    now = time.time()

    try:
        with os.scandir(target_dir) as entries:
            for entry in entries:
                rel_path = os.path.relpath(entry.path, user_dir).replace("\\", "/")
                try:
                    stat = entry.stat()
                    created_at = stat.st_mtime
                    age = now - created_at
                    expiry_sec = max(0, int(DEFAULT_EXPIRY_SECONDS - age))
                    expiry_str = _format_expiry(expiry_sec)
                except Exception:
                    created_at = now
                    expiry_sec = DEFAULT_EXPIRY_SECONDS
                    expiry_str = "Expires in 24h 00m"

                if entry.is_dir():
                    # Calculate folder size
                    folder_size = 0
                    file_count = 0
                    for r, _, f_names in os.walk(entry.path):
                        for fn in f_names:
                            file_count += 1
                            try:
                                folder_size += os.path.getsize(os.path.join(r, fn))
                            except Exception:
                                pass
                    
                    folders_list.append({
                        "id": rel_path,
                        "folder_id": rel_path,
                        "name": entry.name,
                        "size": folder_size,
                        "size_str": _format_size(folder_size),
                        "type": "folder",
                        "items_count": file_count,
                        "created_at": created_at,
                        "last_update": created_at,
                        "expiry_seconds": expiry_sec,
                        "expiry_str": expiry_str
                    })
                elif entry.is_file():
                    fsize = stat.st_size
                    is_arch = is_archive(entry.name)
                    files_list.append({
                        "id": rel_path,
                        "file_id": rel_path,
                        "name": entry.name,
                        "size": fsize,
                        "size_str": _format_size(fsize),
                        "type": "archive" if is_arch else "file",
                        "is_archive": is_arch,
                        "is_video": entry.name.lower().endswith((".mkv", ".mp4", ".avi", ".mov", ".webm")),
                        "created_at": created_at,
                        "last_update": created_at,
                        "expiry_seconds": expiry_sec,
                        "expiry_str": expiry_str
                    })
    except Exception as e:
        log.error("Failed to read directory %s: %s", target_dir, e)
        raise HTTPException(status_code=500, detail=str(e))

    # Sort folders then files alphabetically
    folders_list.sort(key=lambda x: x["name"].lower())
    files_list.sort(key=lambda x: x["name"].lower())

    return {
        "folder_id": rel_folder or "0",
        "folders": folders_list,
        "files": files_list,
        "total_items": len(folders_list) + len(files_list)
    }


class DownloadPayload(BaseModel):
    url: str
    auto_unzip: bool = True

    @field_validator("url")
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v


from ..core.download_manager import DownloadManager

@temp_cloud_router.post("/api/temp_cloud/download")
@rate_limited(cost=1.0)
async def temp_cloud_download(request: Request, payload: DownloadPayload, _csrf = Depends(verify_csrf)):
    """Enqueues download into the Central DownloadManager with live progress tracking & controls."""
    await validate_public_url(payload.url)
    user_dir = get_user_temp_dir()

    # Pre-check Temp Cloud quota before dispatching
    user_used = await asyncio.to_thread(_compute_used_size, user_dir)
    quota_bytes = int(TEMP_STORAGE_QUOTA_GB * (1024 ** 3))
    if user_used >= quota_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"Temp Cloud quota full ({user_used / (1024**3):.1f} / {TEMP_STORAGE_QUOTA_GB:.1f} GB). Please delete files first."
        )

    manager = DownloadManager.get_instance()
    task = await manager.enqueue(payload.url, target_dir=user_dir, auto_unzip=payload.auto_unzip)
    msg = "Bunkr album queued! Downloading sequentially..." if task.task_type == "bunkr" else "1DM Turbo download started!"

    return {
        "success": True,
        "task_id": task.task_id,
        "task": task.to_dict(),
        "message": msg
    }


@temp_cloud_router.get("/api/temp_cloud/downloads")
async def temp_cloud_get_downloads(request: Request):
    """Returns all active, queued, and completed downloads."""
    manager = DownloadManager.get_instance()
    return manager.get_state()


class TaskControlPayload(BaseModel):
    task_id: str


@temp_cloud_router.post("/api/temp_cloud/downloads/cancel")
async def temp_cloud_cancel_download(request: Request, payload: TaskControlPayload, _csrf = Depends(verify_csrf)):
    """Cancels an active or queued download."""
    manager = DownloadManager.get_instance()
    success = await manager.cancel_task(payload.task_id)
    if not success:
        raise HTTPException(status_code=404, detail="Task not found or already finished")
    return {"success": True, "message": "Download cancelled successfully"}


@temp_cloud_router.post("/api/temp_cloud/downloads/pause")
async def temp_cloud_pause_download(request: Request, payload: TaskControlPayload, _csrf = Depends(verify_csrf)):
    """Pauses an active download."""
    manager = DownloadManager.get_instance()
    success = await manager.pause_task(payload.task_id)
    return {"success": success, "message": "Download paused" if success else "Cannot pause task"}


@temp_cloud_router.post("/api/temp_cloud/downloads/resume")
async def temp_cloud_resume_download(request: Request, payload: TaskControlPayload, _csrf = Depends(verify_csrf)):
    """Resumes a paused download."""
    manager = DownloadManager.get_instance()
    success = await manager.resume_task(payload.task_id)
    return {"success": success, "message": "Download resumed" if success else "Cannot resume task"}


@temp_cloud_router.get("/api/temp_cloud/downloads/sse")
async def temp_cloud_downloads_sse(request: Request):
    """Real-time Server-Sent Events (SSE) stream for download progress, speeds, and queue status."""
    manager = DownloadManager.get_instance()
    subscriber_queue = asyncio.Queue(maxsize=20)
    manager._subscribers.append(subscriber_queue)

    async def event_generator():
        try:
            import json as _json
            # Send initial state immediately
            init_state = manager.get_state()
            yield f"data: {_json.dumps(init_state)}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    state = await asyncio.wait_for(subscriber_queue.get(), timeout=2.0)
                    yield f"data: {_json.dumps(state)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if subscriber_queue in manager._subscribers:
                manager._subscribers.remove(subscriber_queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


import mimetypes
from fastapi.responses import StreamingResponse

@temp_cloud_router.get("/api/temp_cloud/stream")
@temp_cloud_router.get("/api/temp_cloud/file")
async def temp_cloud_stream(request: Request, file_id: str):
    """Streams or serves a file stored in Temp Cloud with HTTP 206 Range support for video seeking & playback."""
    user_dir = get_user_temp_dir()

    target_path = os.path.realpath(os.path.join(user_dir, file_id.lstrip("/\\")))
    if not target_path.startswith(os.path.realpath(user_dir)) or not os.path.exists(target_path) or os.path.isdir(target_path):
        raise HTTPException(status_code=404, detail="File not found in Temp Cloud")

    filename = os.path.basename(target_path)
    file_size = os.path.getsize(target_path)

    # Determine MIME type
    content_type, _ = mimetypes.guess_type(target_path)
    if not content_type:
        low_name = filename.lower()
        if low_name.endswith(".mkv"):
            content_type = "video/x-matroska"
        elif low_name.endswith(".mp4"):
            content_type = "video/mp4"
        elif low_name.endswith(".webm"):
            content_type = "video/webm"
        elif low_name.endswith(".mov"):
            content_type = "video/quicktime"
        elif low_name.endswith(".avi"):
            content_type = "video/x-msvideo"
        elif low_name.endswith(".mp3"):
            content_type = "audio/mpeg"
        else:
            content_type = "application/octet-stream"

    STREAM_CHUNK_SIZE = 2 * 1024 * 1024  # 2 MB Turbo Buffer Chunk Size

    range_header = request.headers.get("range")
    if not range_header:
        # Full file streaming with 2MB buffer chunks
        def _iter_file():
            with open(target_path, "rb") as f:
                while chunk := f.read(STREAM_CHUNK_SIZE):
                    yield chunk

        headers = {
            "Content-Length": str(file_size),
            "Accept-Ranges": "bytes",
            "Content-Type": content_type,
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "public, max-age=3600",
            "Connection": "keep-alive",
            "X-Content-Type-Options": "nosniff"
        }
        return StreamingResponse(_iter_file(), headers=headers, status_code=200)

    # Standard RFC 7233 Range parser: bytes=start-end, bytes=start-, or bytes=-suffix_len
    try:
        range_val = range_header.strip().lower().replace("bytes=", "")
        if range_val.startswith("-"):
            # Suffix range (e.g. bytes=-500 -> last 500 bytes)
            suffix_len = int(range_val[1:])
            start = max(0, file_size - suffix_len)
            end = file_size - 1
        elif "-" in range_val:
            parts = range_val.split("-", 1)
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if parts[1] else file_size - 1
        else:
            start = int(range_val)
            end = file_size - 1

        if end >= file_size:
            end = file_size - 1
        if start < 0 or start > end:
            start = 0
            end = file_size - 1
        length = end - start + 1
    except Exception:
        start = 0
        end = file_size - 1
        length = file_size

    def _iter_range(start_byte: int, byte_length: int):
        with open(target_path, "rb") as f:
            f.seek(start_byte)
            bytes_left = byte_length
            while bytes_left > 0:
                chunk_size = min(STREAM_CHUNK_SIZE, bytes_left)
                data = f.read(chunk_size)
                if not data:
                    break
                bytes_left -= len(data)
                yield data

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": content_type,
        "Content-Disposition": f'inline; filename="{filename}"',
        "Cache-Control": "public, max-age=3600",
        "Connection": "keep-alive",
        "X-Content-Type-Options": "nosniff"
    }
    return StreamingResponse(_iter_range(start, length), headers=headers, status_code=206)


class DeletePayload(BaseModel):
    item_id: str


@temp_cloud_router.post("/api/temp_cloud/delete")
@rate_limited(cost=1.0)
async def temp_cloud_delete(request: Request, payload: DeletePayload, _csrf = Depends(verify_csrf)):
    """Deletes a file or directory from user's Temp Cloud."""
    sid = request.session.get("sid") or ensure_sid(request)
    user_dir = get_user_temp_dir(sid)
    
    target_path = os.path.realpath(os.path.join(user_dir, payload.item_id.lstrip("/\\")))
    if not target_path.startswith(os.path.realpath(user_dir)):
        raise HTTPException(status_code=403, detail="Access denied")

    if not os.path.exists(target_path):
        return {"success": True, "message": "Item already removed"}

    try:
        if os.path.isdir(target_path):
            await asyncio.to_thread(shutil.rmtree, target_path, ignore_errors=True)
        else:
            await asyncio.to_thread(os.remove, target_path)
        return {"success": True, "message": "Item deleted from Temp Cloud"}
    except Exception as e:
        log.error("Delete failed for %s: %s", target_path, e)
        raise HTTPException(status_code=500, detail=str(e))
