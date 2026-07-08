"""
Offcloud routes -- large-file overflow path.
"""
from __future__ import annotations

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
            return OffcloudService(key)

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

    size = item.get("size") or item.get("size_bytes") or 0
    created_at = item.get("created_at") or item.get("created")
    
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
        "size_bytes": int(size),
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

    deleted_ids = set()
    if rs:
        try:
            deleted_ids = await rs.get_offcloud_deleted_ids()
        except Exception as e:
            log.warning("Failed to fetch deleted IDs from Redis: %s", e)

    try:
        raw_items = await svc.get_history()
    except OffcloudError as e:
        log.warning("Offcloud get_history failed: %s", e)
        if rs:
            submissions = await rs.get_offcloud_submissions()
            filtered = [s for s in submissions if s.get("request_id") not in deleted_ids]
            return {"success": True, "items": filtered, "_warning": f"Offcloud API error: {e}. Showing cached list."}
        raise HTTPException(status_code=502, detail=str(e))

    items = []
    for item in raw_items:
        try:
            mapped = map_offcloud_item(item)
            if mapped["request_id"] not in deleted_ids:
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


class DeleteOffcloudPayload(BaseModel):
    request_id: str


@offcloud_router.post("/api/offcloud/delete")
@rate_limited(cost=2.0)
async def delete_offcloud_item(request: Request, payload: DeleteOffcloudPayload, _csrf = Depends(verify_csrf)):
    rs = getattr(request.app.state, "rs", None)
    if not rs:
        raise HTTPException(status_code=503, detail="Redis unavailable")
    
    try:
        await rs.add_offcloud_deleted_id(payload.request_id)
    except Exception as e:
        log.warning("Failed to record deleted ID in Redis: %s", e)
        raise HTTPException(status_code=500, detail="Failed to record deletion")

    try:
        submissions = await rs.get_offcloud_submissions()
        new_submissions = [s for s in submissions if s.get("request_id") != payload.request_id]
        if len(new_submissions) != len(submissions):
            await rs.save_offcloud_submissions(new_submissions)
    except Exception as e:
        log.warning("Failed to clean up cached submissions: %s", e)

    return {"success": True}


@offcloud_router.get("/api/offcloud/debug-history")
async def debug_history(request: Request):
    try:
        svc = await _get_offcloud(request)
    except HTTPException as he:
        return {"error": "Not configured", "detail": he.detail}
    
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            r = await client.get(f"https://offcloud.com/api/cloud/history?key={svc.api_key}")
            try:
                json_data = r.json()
            except Exception:
                json_data = None
            return {
                "status_code": r.status_code,
                "is_list": isinstance(json_data, list),
                "is_dict": isinstance(json_data, dict),
                "keys": list(json_data.keys()) if isinstance(json_data, dict) else None,
                "length": len(json_data) if isinstance(json_data, (list, dict)) else None,
                "raw_text": r.text[:2000]
            }
        except Exception as e:
            return {"error": "Request failed", "detail": str(e)}
