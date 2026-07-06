# removed future annotations

import logging
import uuid
import time
import json as _json
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel
from typing import Any, List

from .auth import verify_csrf
from ..auth_utils import current_client
from ..security import (
    validate_item_type,
    validate_positive_int,
    validate_magnet,
    rate_limited,
    ValidationError,
)
from ..cloud_service import format_size, _safe_int

log = logging.getLogger(__name__)
cloud_router = APIRouter()


class CancelTransferPayload(BaseModel):
    id: Any


class DeleteItemPayload(BaseModel):
    type: str
    id: Any


class ZipItemPayload(BaseModel):
    type: str
    id: Any


class BulkItem(BaseModel):
    type: str
    id: Any


class BulkDeletePayload(BaseModel):
    items: List[BulkItem]


class BulkZipPayload(BaseModel):
    items: List[BulkItem]


class AddMagnetPayload(BaseModel):
    magnet: str
    size: Any = None
    name: str | None = None
    provider: str = "auto"


@cloud_router.get("/api/devices")
@rate_limited(cost=1.0)
async def list_devices(request: Request, client = Depends(current_client)):
    cloud = request.app.state.cloud
    try:
        devices = await cloud.get_devices(client)
    except Exception as e:
        log.warning("Provider error on devices: %s", e)
        raise HTTPException(status_code=502, detail="Provider unavailable or failed to list devices")
    return {"success": True, "devices": devices}


@cloud_router.get("/fs/folder/{folder_id}/items")
@rate_limited(cost=1.0)
async def list_items(request: Request, folder_id: str, client = Depends(current_client)):
    config = request.app.state.config
    try:
        folder = validate_positive_int(folder_id, name="folder_id", maximum=config.max_folder_id)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    cloud = request.app.state.cloud
    try:
        data = await cloud.list_items(client, folder)
    except Exception as e:
        log.warning("Provider error on list: %s", e)
        raise HTTPException(status_code=502, detail="Provider unavailable or failed to list items")
        
    for item in data["folders"] + data["files"]:
        item["size_str"] = format_size(item["size"])
    for transfer in data.get("transfers", []):
        transfer["size_str"] = format_size(transfer.get("size", 0))
        transfer["download_rate_str"] = format_size(transfer.get("download_rate", 0)) + "/s"
        
    if folder == 0:
        rs = getattr(request.app.state, "rs", None)
        queue_items = []
        if rs:
            try:
                raw_items = await rs._execute("LRANGE", "streamly:seedr_queue", "0", "-1") or []
                for raw in raw_items:
                    try:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")
                        queue_items.append(_json.loads(raw))
                    except Exception as item_err:
                        log.warning("Skipping corrupted queue entry in list_items: %s", item_err)
            except Exception as q_err:
                log.warning("Failed to fetch local queue for list_items: %s", q_err)
        data["queue"] = queue_items
        
    return data


@cloud_router.post("/api/transfer/cancel")
@rate_limited(cost=2.0)
async def cancel_transfer(request: Request, payload: CancelTransferPayload, client = Depends(current_client), _csrf = Depends(verify_csrf)):
    config = request.app.state.config
    try:
        transfer_id = validate_positive_int(payload.id, name="id", maximum=config.max_file_id)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    cloud = request.app.state.cloud
    try:
        await cloud.delete_transfer(client, transfer_id)
    except Exception as e:
        log.warning("Provider error on transfer cancel: %s", e)
        raise HTTPException(status_code=502, detail="Provider rejected the cancel request or is unavailable")
    return {"success": True}


@cloud_router.post("/api/delete")
@rate_limited(cost=2.0)
async def delete_item(request: Request, payload: DeleteItemPayload, client = Depends(current_client), _csrf = Depends(verify_csrf)):
    config = request.app.state.config
    try:
        item_type = validate_item_type(payload.type)
        item_id = validate_positive_int(payload.id, name="id", maximum=config.max_file_id)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    cloud = request.app.state.cloud
    try:
        await cloud.delete_item(client, item_type, item_id)
    except Exception as e:
        log.warning("Provider error on delete: %s", e)
        raise HTTPException(status_code=502, detail="Provider rejected the request or is unavailable")
    return {"success": True}


@cloud_router.post("/api/zip")
@rate_limited(cost=2.0)
async def zip_item(request: Request, payload: ZipItemPayload, client = Depends(current_client), _csrf = Depends(verify_csrf)):
    config = request.app.state.config
    try:
        item_type = validate_item_type(payload.type)
        item_id = validate_positive_int(payload.id, name="id", maximum=config.max_file_id)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    cloud = request.app.state.cloud
    try:
        url = await cloud.get_zip_url(client, item_type, item_id)
    except Exception as e:
        log.warning("Provider error on zip: %s", e)
        raise HTTPException(status_code=502, detail="Failed to create zip — provider unavailable")
    return {"success": bool(url), "url": url}


@cloud_router.post("/api/delete/bulk")
@rate_limited(cost=3.0)
async def delete_bulk(request: Request, payload: BulkDeletePayload, client = Depends(current_client), _csrf = Depends(verify_csrf)):
    config = request.app.state.config
    items = payload.items
    if not items:
        raise HTTPException(status_code=400, detail="items must be a non-empty list")
    if len(items) > 100:
        raise HTTPException(status_code=400, detail="Too many items (max 100)")
    
    cloud = request.app.state.cloud
    results = []
    for item in items:
        try:
            item_type = validate_item_type(item.type)
            item_id = validate_positive_int(item.id, name="id", maximum=config.max_file_id)
            await cloud.delete_item(client, item_type, item_id)
            results.append({"id": item_id, "type": item_type, "ok": True})
        except ValidationError as exc:
            results.append({"id": item.id, "type": item.type, "ok": False, "error": "Invalid item data"})
        except Exception as exc:
            log.warning("Bulk delete item failed: %s", exc)
            results.append({"id": item.id, "type": item.type, "ok": False, "error": "Provider error"})
    return {"success": True, "results": results}


@cloud_router.post("/api/zip/bulk")
@rate_limited(cost=3.0)
async def zip_bulk(request: Request, payload: BulkZipPayload, client = Depends(current_client), _csrf = Depends(verify_csrf)):
    config = request.app.state.config
    items = payload.items
    if not items:
        raise HTTPException(status_code=400, detail="items must be a non-empty list")
    if len(items) > 100:
        raise HTTPException(status_code=400, detail="Too many items (max 100)")
    
    validated = []
    for item in items:
        try:
            item_type = validate_item_type(item.type)
            item_id = validate_positive_int(item.id, name="id", maximum=config.max_file_id)
            validated.append({"type": item_type, "id": item_id})
        except ValidationError as ve:
            raise HTTPException(status_code=400, detail=str(ve))
    
    if not validated:
        raise HTTPException(status_code=400, detail="No valid items")
    
    cloud = request.app.state.cloud
    try:
        url = await cloud.get_zip_url_bulk(client, validated)
    except Exception as e:
        log.warning("Bulk zip failed: %s", e)
        raise HTTPException(status_code=502, detail="Failed to create zip — provider unavailable")
    return {"success": bool(url), "url": url}


@cloud_router.post("/api/add")
@rate_limited(cost=2.0)
async def add_magnet(request: Request, payload: AddMagnetPayload, client = Depends(current_client), _csrf = Depends(verify_csrf)):
    rs = getattr(request.app.state, "rs", None)
    config = request.app.state.config
    
    try:
        magnet = validate_magnet(payload.magnet, config)
    except ValidationError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    size_bytes = _safe_int(payload.size) if payload.size is not None else 0
    name = payload.name
    
    if not name:
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(magnet)
            qs = parse_qs(parsed.query)
            dn = qs.get("dn")
            if dn:
                name = dn[0]
        except Exception as e:
            log.debug("Could not extract display name (dn=) from magnet URI: %s", e)
    if not name:
        name = "Unknown Magnet"
        
    # Write to history
    from .queue import add_to_history_backend
    await add_to_history_backend(rs, magnet, name, size_bytes)
    
    provider = (payload.provider or "auto").strip().lower()
    if provider not in ("auto", "seedr", "offcloud"):
        provider = "auto"

    use_offcloud = (
        provider == "offcloud"
        or (provider == "auto" and size_bytes > 4.5 * 1024 * 1024 * 1024)
    )

    if use_offcloud:
        from .offcloud import _get_offcloud
        try:
            offcloud = await _get_offcloud(request)
        except Exception:
            offcloud = None

        if offcloud is None or not offcloud.configured:
            raise HTTPException(
                status_code=503,
                detail="Offcloud is not configured (required for files over 4.5GB). Please enter your API key to enable this.",
            )
        from ..offcloud_service import OffcloudError
        try:
            result = await offcloud.add_magnet(magnet)
        except OffcloudError as e:
            log.warning("Offcloud add_magnet failed for '%s': %s", name, e)
            raise HTTPException(status_code=502, detail=str(e))

        if rs:
            try:
                submissions = await rs.get_offcloud_submissions()
                submissions.insert(0, {
                    "request_id": result.get("requestId"),
                    "file_name": result.get("fileName") or name,
                    "status": result.get("status") or "created",
                    "size_bytes": size_bytes,
                    "created_at": int(time.time()),
                })
                await rs.save_offcloud_submissions(submissions[:200])
            except Exception as e:
                log.warning("Failed to record Offcloud submission for tracking: %s", e)

        return {
            "success": True,
            "provider": "offcloud",
            "request_id": result.get("requestId"),
            "file_name": result.get("fileName") or name,
            "status": result.get("status"),
        }

    if size_bytes > 4.5 * 1024 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File exceeds 4.5 GB limit and cannot be downloaded.")
        
    cloud = request.app.state.cloud
    should_queue = False
    
    try:
        if rs:
            q_len = await rs._execute("LLEN", "streamly:seedr_queue")
            if q_len and int(q_len) > 0:
                should_queue = True
    except Exception as e:
        log.error("Queue check failure before adding: %s", e)

    try:
        if not should_queue and rs:
            adding_locked = await rs._execute("GET", "streamly:seedr_adding_lock")
            if adding_locked:
                should_queue = True
    except Exception as e:
        log.error("Adding lock check failure: %s", e)

    if not should_queue:
        try:
            storage = await cloud.list_items(client, 0)
            used = max(0, _safe_int(storage.get("used")))
            maximum = max(1, _safe_int(storage.get("max")))
            transfers = storage.get("transfers", [])
            
            if len(transfers) > 0:
                should_queue = True
            elif size_bytes > 0 and (used + size_bytes > maximum):
                should_queue = True
        except Exception as e:
            log.error("Storage check failure before adding: %s", e)

    if not should_queue and rs:
        try:
            acquired = await rs._execute("SET", "streamly:seedr_adding_lock", "1", "EX", "10", "NX")
            if acquired != "OK":
                should_queue = True
        except Exception as e:
            log.error("Failed to acquire seedr adding lock: %s", e)

    if should_queue:
        if rs:
            queued_item = {
                "task_id": str(uuid.uuid4())[:8],
                "magnet": magnet,
                "name": name,
                "size": size_bytes,
                "time": int(time.time())
            }
            await rs._execute("RPUSH", "streamly:seedr_queue", _json.dumps(queued_item))
            return {"success": True, "queued": True}
        else:
            raise HTTPException(status_code=503, detail="Redis is required for queue storage")
            
    try:
        await cloud.add_magnet(client, magnet)
    except Exception as e:
        log.warning("Direct Seedr addition failed for '%s': %s. Falling back to local queue.", name, e)
        if rs:
            queued_item = {
                "task_id": str(uuid.uuid4())[:8],
                "magnet": magnet,
                "name": name,
                "size": size_bytes,
                "time": int(time.time())
            }
            await rs._execute("RPUSH", "streamly:seedr_queue", _json.dumps(queued_item))
            try:
                await rs._execute("DEL", "streamly:seedr_adding_lock")
            except Exception as e:
                # Not fatal (the lock has a 10s TTL and will expire on its own), but
                # worth knowing about since it delays the next magnet addition.
                log.warning("Failed to release seedr_adding_lock after fallback re-queue: %s", e)
            return {"success": True, "queued": True, "fallback": True}
        else:
            raise HTTPException(status_code=502, detail=str(e) or "Provider rejected the request and Redis fallback is unavailable")
            
    return {"success": True}


@cloud_router.get("/api/url")
@rate_limited(cost=1.0)
async def get_url(request: Request, file_id: str, client = Depends(current_client)):
    config = request.app.state.config
    try:
        f_id = validate_positive_int(file_id, name="file_id", maximum=config.max_file_id)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    cloud = request.app.state.cloud
    try:
        url = await cloud.get_stream_url(client, f_id)
    except Exception as e:
        log.warning("Provider error on get_url: %s", e)
        raise HTTPException(status_code=502, detail="Failed to get stream URL — provider unavailable")
        
    if not url:
        raise HTTPException(status_code=404, detail="Stream URL not available for this file")
    return {"success": True, "url": url}
