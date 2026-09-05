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
from ..auth_utils import ensure_sid, current_client
from ..security import rate_limited, validate_public_url
from ..core.direct_downloader import Direct1DMDownloader
from ..core.archive_extractor import is_archive, safe_extract_archive
from ..core.bunkr_engine import is_bunkr_url, BunkrSequentialDownloader

log = logging.getLogger(__name__)
temp_cloud_router = APIRouter()

def _resolve_temp_cloud_root() -> str:
    # 1. Explicit env var override
    env_dir = os.environ.get("PERSISTENT_STORAGE_DIR") or os.environ.get("TEMP_CLOUD_DIR")
    if env_dir:
        try:
            p = os.path.abspath(env_dir)
            os.makedirs(p, exist_ok=True)
            if os.access(p, os.W_OK):
                return p
        except Exception:
            pass

    # 2. Hugging Face Spaces Persistent Storage / Bucket mount (/data)
    if os.path.exists("/data"):
        try:
            p = "/data/streamly_storage"
            os.makedirs(p, exist_ok=True)
            test_file = os.path.join(p, ".write_test")
            with open(test_file, "w") as f:
                f.write("ok")
            os.remove(test_file)
            log.info("Using persistent bucket mount at %s", p)
            return p
        except Exception:
            try:
                # If subfolder creation fails on certain FUSE bucket drivers, use /data directly
                test_file = "/data/.write_test"
                with open(test_file, "w") as f:
                    f.write("ok")
                os.remove(test_file)
                log.info("Using persistent bucket mount directly at /data")
                return "/data"
            except Exception as e:
                log.warning("/data bucket mount exists but is not writable (%s)", e)

    # 3. User home persistent space (~/streamly_storage)
    try:
        home = os.path.expanduser("~")
        if home and os.path.exists(home):
            p = os.path.join(home, "streamly_storage")
            os.makedirs(p, exist_ok=True)
            if os.access(p, os.W_OK):
                return p
    except Exception:
        pass

    # 4. Project workspace local storage
    try:
        local_dir = os.path.abspath(os.path.join(os.getcwd(), "storage", "temp_cloud"))
        os.makedirs(local_dir, exist_ok=True)
        if os.access(local_dir, os.W_OK):
            return local_dir
    except Exception:
        pass

    # 5. Linux /tmp fallback
    if os.name != "nt" and os.path.exists("/tmp"):
        return "/tmp/streamly_temp_cloud"

    return os.path.abspath(os.path.join(os.getcwd(), "temp_cloud_data"))

TEMP_CLOUD_ROOT = _resolve_temp_cloud_root()
DEFAULT_EXPIRY_DAYS = float(os.environ.get("TEMP_CLOUD_EXPIRY_DAYS", "30.0"))
DEFAULT_EXPIRY_SECONDS = int(DEFAULT_EXPIRY_DAYS * 86400)
TEMP_STORAGE_QUOTA_GB = float(os.environ.get("TEMP_STORAGE_QUOTA_GB", "50.0"))

def get_user_temp_dir(sid: str = "") -> str:
    """Unified instance cloud storage root persisting files across restarts and sessions."""
    global TEMP_CLOUD_ROOT
    if TEMP_CLOUD_ROOT and os.path.exists(TEMP_CLOUD_ROOT):
        return TEMP_CLOUD_ROOT
    target_root = _resolve_temp_cloud_root()
    TEMP_CLOUD_ROOT = target_root
    return target_root

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
        return "Expires soon"
    days = expiry_sec // 86400
    hours = (expiry_sec % 86400) // 3600
    minutes = (expiry_sec % 3600) // 60
    if days > 0:
        return f"Expires in {days}d {hours:02d}h"
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
                    # If file has an old preserved timestamp from archive/remote (e.g. before year 2025), touch it to now!
                    if stat.st_mtime < 1735689600:
                        os.utime(fpath, (now, now))
                        continue
                        
                    file_age = now - stat.st_mtime
                    if file_age > DEFAULT_EXPIRY_SECONDS:
                        os.remove(fpath)
                        log.info("Auto-pruned expired temp file: %s (age: %.1f hours)", fpath, file_age / 3600.0)
                except Exception:
                    pass
            for dname in dirs:
                dpath = os.path.join(root, dname)
                try:
                    d_stat = os.stat(dpath)
                    # Only clean empty folders if they are at least 1 hour old to avoid race conditions with active downloads
                    if not os.listdir(dpath) and (now - d_stat.st_ctime > 3600):
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


async def verify_user_session(request: Request):
    """
    Guarantees session ID existence for Temp Cloud local disk operations.
    Completely decoupled from Seedr OAuth state.
    """
    ensure_sid(request)
    request.session.setdefault("site_auth", True)
    return True


@temp_cloud_router.get("/api/temp_cloud/storage")
@rate_limited(cost=0.5)
async def temp_cloud_storage(request: Request, _auth = Depends(verify_user_session)):
    """Returns NVMe / local disk storage metrics for the top storage bar."""
    user_dir = get_user_temp_dir()
    
    # Calculate actual used storage in worker threadpool (non-blocking)
    user_used = await asyncio.to_thread(_compute_used_size, user_dir)

    try:
        total_phys, _, free_phys = shutil.disk_usage(user_dir)
        # S3 / FUSE / Ceph object bucket mounts report virtual geometry (> 100 TB / Petabytes)
        is_cloud_bucket = total_phys > (100 * 1024 * (1024 ** 3))
        
        if "TEMP_STORAGE_QUOTA_GB" in os.environ:
            effective_quota_gb = TEMP_STORAGE_QUOTA_GB
            quota_bytes = int(effective_quota_gb * (1024 ** 3))
            is_unlimited = False
        elif is_cloud_bucket:
            effective_quota_gb = 1000.0  # Soft 1 TB benchmark for visual progress
            quota_bytes = total_phys
            is_unlimited = True
        elif total_phys > 0:
            effective_quota_gb = round(total_phys / (1024 ** 3), 1)
            quota_bytes = total_phys
            is_unlimited = False
        else:
            effective_quota_gb = TEMP_STORAGE_QUOTA_GB
            quota_bytes = int(effective_quota_gb * (1024 ** 3))
            is_unlimited = False
    except Exception:
        effective_quota_gb = TEMP_STORAGE_QUOTA_GB
        quota_bytes = int(effective_quota_gb * (1024 ** 3))
        is_unlimited = False

    user_used_gb = round(user_used / (1024 ** 3), 2)
    if is_unlimited:
        metrics_str = f"{user_used_gb:.2f} GB • Unlimited Bucket"
        pct = round((user_used / (1024 ** 4)) * 100, 1)  # Percentage of 1 TB soft bar
        storage_label = "Cloud Bucket Storage"
    else:
        pct = round((user_used / quota_bytes) * 100, 1) if quota_bytes > 0 else 0.0
        metrics_str = f"{user_used_gb:.2f} / {effective_quota_gb:.1f} GB • {pct:.0f}%"
        storage_label = "Temp NVMe Storage"

    pct = min(100.0, pct)

    return {
        "storage_label": storage_label,
        "storage_metrics": metrics_str,
        "storage_subtext": "S3 Bucket Mount Active" if is_unlimited else "Auto-expires in 24h",
        "percent": pct,
        "user_used_bytes": user_used,
        "total_bytes": quota_bytes,
        "free_bytes": max(0, quota_bytes - user_used)
    }


@temp_cloud_router.get("/api/temp_cloud/list")
@rate_limited(cost=1.0)
async def temp_cloud_list(request: Request, folder_id: Optional[str] = None, _auth = Depends(verify_user_session)):
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
                    created_at = max(stat.st_ctime, stat.st_mtime)
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
                    if entry.name.endswith(".part"):
                        continue
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
                        "is_video": entry.name.lower().endswith((".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".ts", ".flv", ".wmv")),
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
async def temp_cloud_download(request: Request, payload: DownloadPayload, _auth = Depends(verify_user_session), _csrf = Depends(verify_csrf)):
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
    if task.task_type == "stream":
        msg = "📡 HLS/DASH Stream download started via yt-dlp!"
    elif task.task_type in ("bunkr", "media_grabber"):
        msg = "Media Grabber album queued! Downloading sequentially..."
    else:
        msg = "1DM Turbo download started!"

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
    log.info("Received download cancel request for task_id: %s", payload.task_id)
    manager = DownloadManager.get_instance()
    success = await manager.cancel_task(payload.task_id)
    return {"success": success, "message": "Download cancelled successfully" if success else "Task already finished or not found"}


@temp_cloud_router.post("/api/temp_cloud/downloads/pause")
async def temp_cloud_pause_download(request: Request, payload: TaskControlPayload, _csrf = Depends(verify_csrf)):
    """Pauses an active download."""
    log.info("Received download pause request for task_id: %s", payload.task_id)
    manager = DownloadManager.get_instance()
    success = await manager.pause_task(payload.task_id)
    return {"success": success, "message": "Download paused" if success else "Cannot pause task"}


@temp_cloud_router.post("/api/temp_cloud/downloads/resume")
async def temp_cloud_resume_download(request: Request, payload: TaskControlPayload, _csrf = Depends(verify_csrf)):
    """Resumes a paused download."""
    log.info("Received download resume request for task_id: %s", payload.task_id)
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
@temp_cloud_router.get("/api/temp_cloud/download_file")
async def temp_cloud_stream(request: Request, file_id: str, download: bool = False):
    """Streams or serves a file stored in Temp Cloud with HTTP 206 Range support for video seeking & playback."""
    user_dir = get_user_temp_dir()

    target_path = os.path.realpath(os.path.join(user_dir, file_id.lstrip("/\\")))
    if not target_path.startswith(os.path.realpath(user_dir)) or not os.path.exists(target_path) or os.path.isdir(target_path):
        raise HTTPException(status_code=404, detail="File not found in Temp Cloud")

    filename = os.path.basename(target_path)
    file_size = os.path.getsize(target_path)

    is_download = download or request.query_params.get("download") in ("1", "true", "yes") or request.url.path.endswith("/download_file")
    disposition = "attachment" if is_download else "inline"

    # Determine MIME type
    if is_download:
        content_type = "application/octet-stream"
    else:
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

    STREAM_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB High-Throughput FUSE Buffer Chunk Size

    range_header = request.headers.get("range")

    async def _iter_file_chunks(path: str, start_byte: int, byte_length: int, chunk_size: int = STREAM_CHUNK_SIZE):
        """Asynchronously streams file chunks via threadpool to prevent blocking the event loop on FUSE bucket I/O."""
        def _read_sync(file_obj, sz):
            return file_obj.read(sz)

        with open(path, "rb", buffering=0) as f:
            if start_byte > 0:
                f.seek(start_byte)
            remaining = byte_length
            while remaining > 0:
                to_read = min(chunk_size, remaining)
                chunk = await asyncio.to_thread(_read_sync, f, to_read)
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    if not range_header:
        # Full file streaming / direct single-connection download
        headers = {
            "Content-Length": str(file_size),
            "Accept-Ranges": "bytes",
            "Content-Type": content_type,
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "Cache-Control": "public, max-age=86400",
            "Connection": "keep-alive",
            "X-Content-Type-Options": "nosniff"
        }
        return StreamingResponse(_iter_file_chunks(target_path, 0, file_size), headers=headers, status_code=200)

    # Standard RFC 7233 Range parser: supports bytes=start-end, bytes=start-, and bytes=-suffix_len
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

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": content_type,
        "Content-Disposition": f'{disposition}; filename="{filename}"',
        "Cache-Control": "public, max-age=86400",
        "Connection": "keep-alive",
        "X-Content-Type-Options": "nosniff"
    }
    return StreamingResponse(_iter_file_chunks(target_path, start, length), headers=headers, status_code=206)


class DeletePayload(BaseModel):
    item_id: str


@temp_cloud_router.post("/api/temp_cloud/delete")
@rate_limited(cost=1.0)
async def temp_cloud_delete(request: Request, payload: DeletePayload, _auth = Depends(verify_user_session), _csrf = Depends(verify_csrf)):
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


from ..core.archive_extractor import safe_create_zip, safe_extract_archive, is_archive

class ArchiveOperationPayload(BaseModel):
    item_id: str
    delete_source: bool = False


@temp_cloud_router.post("/api/temp_cloud/zip")
@rate_limited(cost=1.0)
async def temp_cloud_zip_folder(request: Request, payload: ArchiveOperationPayload, _auth = Depends(verify_user_session), _csrf = Depends(verify_csrf)):
    """Compresses a folder into a standard .zip archive."""
    sid = request.session.get("sid") or ensure_sid(request)
    user_dir = get_user_temp_dir(sid)

    target_path = os.path.realpath(os.path.join(user_dir, payload.item_id.lstrip("/\\")))
    if not target_path.startswith(os.path.realpath(user_dir)):
        raise HTTPException(status_code=403, detail="Access denied")

    if not os.path.exists(target_path):
        raise HTTPException(status_code=404, detail="Folder not found")

    if not os.path.isdir(target_path):
        raise HTTPException(status_code=400, detail="Target is not a folder")

    try:
        zip_path = await asyncio.to_thread(safe_create_zip, target_path, delete_folder=payload.delete_source)
        zip_name = os.path.basename(zip_path)
        return {"success": True, "message": f"Folder zipped successfully to {zip_name}", "zip_file": zip_name}
    except Exception as e:
        log.error("Zip failed for %s: %s", target_path, e)
        raise HTTPException(status_code=500, detail=str(e))


@temp_cloud_router.post("/api/temp_cloud/unzip")
@rate_limited(cost=1.0)
async def temp_cloud_unzip_archive(request: Request, payload: ArchiveOperationPayload, _auth = Depends(verify_user_session), _csrf = Depends(verify_csrf)):
    """Extracts a .zip, .rar, .7z, or .tar archive file."""
    sid = request.session.get("sid") or ensure_sid(request)
    user_dir = get_user_temp_dir(sid)

    target_path = os.path.realpath(os.path.join(user_dir, payload.item_id.lstrip("/\\")))
    if not target_path.startswith(os.path.realpath(user_dir)):
        raise HTTPException(status_code=403, detail="Access denied")

    if not os.path.exists(target_path):
        raise HTTPException(status_code=404, detail="Archive not found")

    if not is_archive(target_path):
        raise HTTPException(status_code=400, detail="File is not a supported archive format (.zip, .rar, .7z, .tar.gz)")

    # Ensure file is not actively being downloaded by the download manager
    manager = DownloadManager.get_instance()
    fname = os.path.basename(target_path)
    active_dict = getattr(manager, "active_tasks", getattr(manager, "_tasks", {}))
    for task in active_dict.values():
        if (task.filename == fname or task.task_id == payload.item_id) and task.status in ("DOWNLOADING", "QUEUED"):
            raise HTTPException(
                status_code=400,
                detail=f"'{fname}' is still actively downloading ({task.progress:.1f}%). Please wait for the download to finish before unzipping."
            )

    try:
        parent_dir = os.path.dirname(target_path)
        dest_dir = await asyncio.to_thread(safe_extract_archive, target_path, extract_to=parent_dir, delete_archive=payload.delete_source)
        folder_name = os.path.basename(dest_dir)
        return {"success": True, "message": f"Archive extracted successfully to {folder_name}", "dest_folder": folder_name}
    except Exception as e:
        log.error("Unzip failed for %s: %s", target_path, e)
        raise HTTPException(status_code=500, detail=str(e))


class CreateFolderPayload(BaseModel):
    name: str
    folder_id: Optional[str] = ""


@temp_cloud_router.post("/api/temp_cloud/folder/create")
@rate_limited(cost=1.0)
async def temp_cloud_create_folder(request: Request, payload: CreateFolderPayload, _auth = Depends(verify_user_session), _csrf = Depends(verify_csrf)):
    """Creates a new folder in Temp Cloud."""
    import re
    sid = request.session.get("sid") or ensure_sid(request)
    user_dir = get_user_temp_dir(sid)

    clean_name = re.sub(r'[\\/*?:"<>|]', "", payload.name).strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Invalid folder name")

    parent_rel = (payload.folder_id or "").strip().lstrip("/\\")
    if parent_rel and parent_rel != "0":
        target_dir = os.path.realpath(os.path.join(user_dir, parent_rel))
    else:
        target_dir = os.path.realpath(user_dir)

    if not target_dir.startswith(os.path.realpath(user_dir)):
        raise HTTPException(status_code=403, detail="Access denied")

    new_folder_path = os.path.join(target_dir, clean_name)
    if os.path.exists(new_folder_path):
        raise HTTPException(status_code=400, detail=f"A folder or file named '{clean_name}' already exists")

    try:
        os.makedirs(new_folder_path, exist_ok=True)
        now = time.time()
        os.utime(new_folder_path, (now, now))
        return {"success": True, "message": f"Folder '{clean_name}' created successfully", "folder_name": clean_name}
    except Exception as e:
        log.error("Create folder failed for %s: %s", new_folder_path, e)
        raise HTTPException(status_code=500, detail=str(e))


class RenamePayload(BaseModel):
    item_id: str
    new_name: str


@temp_cloud_router.post("/api/temp_cloud/rename")
@rate_limited(cost=1.0)
async def temp_cloud_rename(request: Request, payload: RenamePayload, _auth = Depends(verify_user_session), _csrf = Depends(verify_csrf)):
    """Renames a file or folder in Temp Cloud."""
    import re
    sid = request.session.get("sid") or ensure_sid(request)
    user_dir = get_user_temp_dir(sid)

    clean_name = re.sub(r'[\\/*?:"<>|]', "", payload.new_name).strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Invalid new name")

    source_path = os.path.realpath(os.path.join(user_dir, payload.item_id.lstrip("/\\")))
    if not source_path.startswith(os.path.realpath(user_dir)):
        raise HTTPException(status_code=403, detail="Access denied")

    if not os.path.exists(source_path):
        raise HTTPException(status_code=404, detail="Item not found")

    parent_dir = os.path.dirname(source_path)
    dest_path = os.path.join(parent_dir, clean_name)

    if os.path.exists(dest_path) and dest_path != source_path:
        raise HTTPException(status_code=400, detail=f"An item named '{clean_name}' already exists")

    try:
        os.rename(source_path, dest_path)
        now = time.time()
        os.utime(dest_path, (now, now))
        return {"success": True, "message": f"Renamed to '{clean_name}' successfully", "new_name": clean_name}
    except Exception as e:
        log.error("Rename failed for %s -> %s: %s", source_path, dest_path, e)
        raise HTTPException(status_code=500, detail=str(e))
