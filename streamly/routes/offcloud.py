"""
Offcloud routes -- large-file overflow path.
"""
# removed future annotations

import logging
import urllib.parse
import os
import time

from fastapi import APIRouter, Request, HTTPException, Depends
from typing import Any
from pydantic import BaseModel

from .auth import verify_csrf
from ..security import rate_limited, validate_magnet
from ..offcloud_service import OffcloudService, OffcloudError

log = logging.getLogger(__name__)
offcloud_router = APIRouter()


class OffcloudConfigPayload(BaseModel):
    api_key: str


class OffcloudAddPayload(BaseModel):
    magnet: str


class OffcloudStatusPayload(BaseModel):
    request_id: str


async def _get_offcloud(request: Request) -> OffcloudService:
    # 1. Check if configured in state (env)
    svc = getattr(request.app.state, "offcloud", None)
    if svc and svc.configured:
        return svc

    # 2. Check if saved in Redis
    rs = getattr(request.app.state, "rs", None)
    if rs:
        key = await rs.get_offcloud_key()
        if key:
            svc = OffcloudService(key)
            request.app.state.offcloud = svc  # Cache for subsequent requests
            return svc

    raise HTTPException(
        status_code=503,
        detail="Offcloud is not configured. Please enter your API key to enable this feature.",
    )


@offcloud_router.post("/api/offcloud/config")
async def offcloud_config(request: Request, payload: OffcloudConfigPayload, _csrf = Depends(verify_csrf)):
    key = payload.api_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="API key cannot be empty")

    # Validate key before saving
    test_svc = OffcloudService(key)
    try:
        await test_svc.get_status("dummy_id_test")
    except OffcloudError as e:
        if "rejected the API key" in str(e):
            raise HTTPException(status_code=400, detail="Invalid Offcloud API key.")
    except Exception:
        pass

    rs = getattr(request.app.state, "rs", None)
    if not rs:
        raise HTTPException(status_code=500, detail="Redis storage not available")

    await rs.save_offcloud_key(key)
    return {"success": True}


@offcloud_router.post("/api/offcloud/add")
@rate_limited(cost=2.0)
async def offcloud_add(request: Request, payload: OffcloudAddPayload, _csrf = Depends(verify_csrf)):
    config = request.app.state.config
    try:
        magnet = validate_magnet(payload.magnet, config)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    svc = await _get_offcloud(request)
    try:
        result = await svc.add_magnet(magnet)
    except OffcloudError as e:
        log.warning("Offcloud add_magnet failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))

    return {
        "success": True,
        "request_id": result.get("requestId"),
        "file_name": result.get("fileName"),
        "status": result.get("status"),
    }


@offcloud_router.post("/api/offcloud/status")
@rate_limited(cost=1.0)
async def offcloud_status(request: Request, payload: OffcloudStatusPayload, _csrf = Depends(verify_csrf)):
    request_id = (payload.request_id or "").strip()
    if not request_id:
        raise HTTPException(status_code=400, detail="request_id is required")

    svc = await _get_offcloud(request)
    try:
        result = await svc.get_status(request_id)
    except OffcloudError as e:
        log.warning("Offcloud get_status failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))

    return {"success": True, "status": result}


@offcloud_router.get("/api/offcloud/enabled")
async def offcloud_enabled(request: Request):
    svc = getattr(request.app.state, "offcloud", None)
    if svc and svc.configured:
        return {"success": True, "enabled": True}

    rs = getattr(request.app.state, "rs", None)
    if rs:
        key = await rs.get_offcloud_key()
        if key:
            return {"success": True, "enabled": True}

    return {"success": True, "enabled": False}


def map_offcloud_item(item: dict[str, Any]) -> dict[str, Any]:
    req_id = item.get("requestId") or item.get("request_id") or ""
    file_name = item.get("fileName") or item.get("file_name") or "Unnamed"
    status = item.get("status")
    
    status_str = "created"
    download_url = item.get("url") or item.get("download_url")
    if isinstance(status, dict):
        status_str = status.get("status") or "created"
        if not download_url:
            download_url = status.get("url")
    elif isinstance(status, str):
        status_str = status

    # Robust size parsing from nested status dict, outer dict, and handle unit strings
    raw_size = None
    if isinstance(status, dict):
        raw_size = (
            status.get("size")
            or status.get("fileSize")
            or status.get("file_size")
            or status.get("fileSizeInBytes")
            or status.get("file_size_bytes")
        )
    if not raw_size:
        raw_size = (
            item.get("size")
            or item.get("fileSize")
            or item.get("file_size")
            or item.get("size_bytes")
            or item.get("fileSizeInBytes")
        )

    size_val = 0
    if raw_size is not None:
        try:
            if isinstance(raw_size, (int, float)):
                size_val = int(raw_size)
            else:
                import re
                s_str = str(raw_size).strip().lower()
                # Parse float values or values with units (e.g., "1.2 GB" or "950 MB")
                match = re.match(r"^([\d\.]+)\s*([a-z]*)$", s_str)
                if match:
                    val = float(match.group(1))
                    unit = match.group(2)
                    if "g" in unit:
                        size_val = int(val * 1024 * 1024 * 1024)
                    elif "m" in unit:
                        size_val = int(val * 1024 * 1024)
                    elif "k" in unit:
                        size_val = int(val * 1024)
                    else:
                        size_val = int(val)
                else:
                    size_val = int(float(s_str))
        except Exception:
            size_val = 0

    size_bytes = size_val
    created_at = item.get("created_at") or item.get("createdOn") or item.get("created")
    
    if isinstance(created_at, str):
        try:
            created_at = int(float(created_at))
        except ValueError:
            from datetime import datetime
            try:
                clean_date = created_at.replace("Z", "+00:00")
                dt = datetime.fromisoformat(clean_date)
                created_at = int(dt.timestamp())
            except Exception:
                try:
                    clean_date = created_at.replace("Z", "+00:00")
                    dt = datetime.strptime(clean_date[:19], "%Y-%m-%d %H:%M:%S")
                    created_at = int(dt.timestamp())
                except Exception:
                    created_at = int(time.time())
    elif not isinstance(created_at, (int, float)):
        created_at = int(time.time())
    else:
        created_at = int(created_at)

    return {
        "request_id": req_id,
        "file_name": file_name,
        "status": status_str,
        "size_bytes": size_bytes,
        "created_at": created_at,
        "download_url": download_url,
    }


@offcloud_router.get("/api/offcloud/list")
@rate_limited(cost=1.0)
async def offcloud_list(request: Request):
    rs = getattr(request.app.state, "rs", None)
    try:
        svc = await _get_offcloud(request)
    except HTTPException as he:
        return {"success": True, "items": [], "_warning": str(he.detail)}

    try:
        raw_items = await svc.get_history()
    except OffcloudError as e:
        log.warning("Offcloud get_history failed: %s", e)
        if rs:
            submissions = await rs.get_offcloud_submissions()
            return {"success": True, "items": submissions, "_warning": f"Offcloud API error: {e}. Showing cached list."}
        raise HTTPException(status_code=502, detail=str(e))

    items = []
    for item in raw_items:
        try:
            mapped = map_offcloud_item(item)
            items.append(mapped)
        except Exception as ex:
            log.warning("Failed to map Offcloud item %r: %s", item, ex)

    if rs:
        try:
            await rs.save_offcloud_submissions(items)
        except Exception as e:
            log.warning("Failed to save offcloud history cache to Redis: %s", e)

    return {"success": True, "items": items}


@offcloud_router.get("/api/offcloud/explore/{request_id}")
@rate_limited(cost=1.0)
async def offcloud_explore(request: Request, request_id: str):
    request_id = request_id.strip()
    if not request_id:
        raise HTTPException(status_code=400, detail="request_id is required")

    svc = await _get_offcloud(request)
    try:
        urls = await svc.explore_folder(request_id)
    except OffcloudError as e:
        log.warning("Offcloud explore_folder failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))

    files = []
    for url in urls:
        parsed_url = urllib.parse.urlparse(url)
        filename = os.path.basename(parsed_url.path)
        filename = urllib.parse.unquote(filename) or "Unnamed File"

        files.append({
            "name": filename,
            "download_url": url,
            "size": 0,
            "type": "file"
        })

    return {"success": True, "files": files}

@offcloud_router.get("/offcloud-debug")
async def offcloud_debug(request: Request):
    try:
        import httpx
        url = "https://1-cdn2-ovh-bea.energycdn.com/cdn3sto/frostyicebreath-sto/69ffd86ebbc112.07118806/595767889/1783660823/603c1a1cdfd73e987a8f77a68e682cec2cd28c15/Moving.S01.KOREAN.1080p.WEBRip.x265-KONTRAST.zip"
        async with httpx.AsyncClient(follow_redirects=False) as client:
            resp = await client.head(url)
            return {
                "status_code": resp.status_code,
                "headers": dict(resp.headers)
            }
    except Exception as e:
        return {"error": str(e)}


