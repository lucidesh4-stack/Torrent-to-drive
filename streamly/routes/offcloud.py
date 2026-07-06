"""
Offcloud routes -- large-file overflow path.
"""
from __future__ import annotations

import logging
import urllib.parse
import os
import time
from fastapi import APIRouter, Request, HTTPException, Depends
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


_TERMINAL_STATUSES = {"downloaded", "error"}


@offcloud_router.get("/api/offcloud/list")
@rate_limited(cost=1.0)
async def offcloud_list(request: Request):
    rs = getattr(request.app.state, "rs", None)
    if not rs:
        return {"success": True, "items": [], "_warning": "Tracking unavailable (Redis not configured)"}

    submissions = await rs.get_offcloud_submissions()
    try:
        svc = await _get_offcloud(request)
    except HTTPException:
        svc = None

    if svc:
        changed = False
        for item in submissions:
            # 1. Sanitize status field in case it was saved as a dict
            curr_status = item.get("status")
            if isinstance(curr_status, dict):
                curr_status = curr_status.get("status") or "created"
                item["status"] = curr_status
                changed = True

            if item.get("status") in _TERMINAL_STATUSES:
                continue
            request_id = item.get("request_id")
            if not request_id:
                continue
            try:
                status_data = await svc.get_status(request_id)
                new_status = None
                download_url = None
                if isinstance(status_data, dict):
                    status_val = status_data.get("status")
                    if isinstance(status_val, dict):
                        new_status = status_val.get("status")
                        download_url = status_val.get("url")
                    elif isinstance(status_val, str):
                        new_status = status_val
                    
                    if not download_url:
                        download_url = status_data.get("url")

                if new_status and new_status != item.get("status"):
                    item["status"] = new_status
                    changed = True
                if download_url and download_url != item.get("download_url"):
                    item["download_url"] = download_url
                    changed = True
            except OffcloudError as e:
                log.debug("Offcloud status refresh failed for %s: %s", request_id, e)

        if changed:
            try:
                await rs.save_offcloud_submissions(submissions)
            except Exception as e:
                log.warning("Failed to persist refreshed Offcloud statuses: %s", e)

    return {"success": True, "items": submissions}


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
