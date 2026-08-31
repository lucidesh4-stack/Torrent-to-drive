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
from typing import Optional, List
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, field_validator

from .auth import verify_csrf
from ..auth_utils import ensure_sid
from ..security import rate_limited, validate_public_url
from ..core.direct_downloader import Direct1DMDownloader
from ..core.archive_extractor import is_archive, safe_extract_archive

log = logging.getLogger(__name__)
temp_cloud_router = APIRouter()

TEMP_CLOUD_ROOT = os.environ.get("TEMP_CLOUD_DIR", "/tmp/streamly_temp_cloud")
DEFAULT_EXPIRY_SECONDS = 86400 # 24 hours

def get_user_temp_dir(sid: str) -> str:
    safe_sid = "".join(c for c in sid if c.isalnum() or c in "_-")
    path = os.path.join(TEMP_CLOUD_ROOT, safe_sid)
    os.makedirs(path, exist_ok=True)
    return path

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


@temp_cloud_router.get("/api/temp_cloud/storage")
@rate_limited(cost=0.5)
async def temp_cloud_storage(request: Request):
    """Returns NVMe / local disk storage metrics for the top storage bar."""
    sid = request.session.get("sid") or ensure_sid(request)
    user_dir = get_user_temp_dir(sid)
    
    # Calculate disk usage of the host drive
    total, used, free = shutil.disk_usage(TEMP_CLOUD_ROOT if os.path.exists(TEMP_CLOUD_ROOT) else "/")
    
    # Calculate user's specific temp usage
    user_used = 0
    for root, _, files in os.walk(user_dir):
        for f in files:
            try:
                user_used += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass

    total_gb = round(total / (1024 ** 3), 1)
    used_gb = round(used / (1024 ** 3), 2)
    user_used_gb = round(user_used / (1024 ** 3), 2)
    pct = round((used / total) * 100, 1) if total > 0 else 0.0

    return {
        "storage_label": "Temp NVMe Storage",
        "storage_metrics": f"{user_used_gb:.2f} / {total_gb:.1f} GB • {pct:.0f}%",
        "storage_subtext": "Auto-expires in 24h",
        "percent": pct,
        "user_used_bytes": user_used,
        "total_bytes": total,
        "free_bytes": free
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
        validate_public_url(v)
        return v


@temp_cloud_router.post("/api/temp_cloud/download")
@rate_limited(cost=1.0)
async def temp_cloud_download(request: Request, payload: DownloadPayload, _csrf = Depends(verify_csrf)):
    """Spawns 1DM multi-part engine to download URL directly into user's Temp Cloud directory."""
    sid = request.session.get("sid") or ensure_sid(request)
    user_dir = get_user_temp_dir(sid)

    downloader = Direct1DMDownloader(target_dir=user_dir, num_connections=16)

    async def _run_download_and_extract():
        try:
            downloaded_file = await downloader.download(payload.url)
            if payload.auto_unzip and is_archive(downloaded_file):
                log.info("Auto-extracting downloaded archive: %s", downloaded_file)
                await asyncio.to_thread(safe_extract_archive, downloaded_file, user_dir, delete_archive=True)
        except Exception as e:
            log.error("Temp cloud download failed for URL %s: %s", payload.url, e)

    asyncio.create_task(_run_download_and_extract())

    return {
        "success": True,
        "message": "1DM Download started in background. File will appear in Temp Cloud upon completion."
    }


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
            shutil.rmtree(target_path, ignore_errors=True)
        else:
            os.remove(target_path)
        return {"success": True, "message": "Item deleted from Temp Cloud"}
    except Exception as e:
        log.error("Delete failed for %s: %s", target_path, e)
        raise HTTPException(status_code=500, detail=str(e))
