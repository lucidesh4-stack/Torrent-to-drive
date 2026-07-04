# removed future annotations

import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel

from ..auth_utils import current_client, ensure_sid, get_csrf_token, rotate_sid
from ..store import NotAuthenticated
from ..security import validate_email, validate_password, rate_limited

log = logging.getLogger(__name__)
auth_router = APIRouter()


class LoginPayload(BaseModel):
    email: str
    password: str


async def verify_csrf(request: Request):
    expected = request.session.get("csrf")
    supplied = request.headers.get("X-CSRF-Token", "")
    import hmac
    if not expected or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


@auth_router.get("/api/csrf")
@rate_limited(cost=0.2)
async def csrf(request: Request):
    ensure_sid(request)
    return {"success": True, "csrfToken": get_csrf_token(request)}


@auth_router.get("/api/status")
@rate_limited(cost=0.2)
async def status_route(request: Request, client = Depends(current_client)):
    return {
        "success": True,
        "authenticated": True,
        "username": request.session.get("username", "")
    }


@auth_router.post("/api/login")
@rate_limited(cost=5.0)
async def login(request: Request, payload: LoginPayload, _csrf = Depends(verify_csrf)):
    # Check body size and type (handled by FastAPI/Pydantic automatically)
    try:
        email = validate_email(payload.email)
        password = validate_password(payload.password)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    ensure_sid(request)  # guarantees a session exists before we touch it below
    cloud = request.app.state.cloud
    store = request.app.state.store
    rs = request.app.state.rs
    
    try:
        client, username = await cloud.login(email, password)
    except PermissionError as pe:
        raise HTTPException(status_code=401, detail=str(pe))
    except ConnectionError as ce:
        raise HTTPException(status_code=502, detail=str(ce))
    
    # Rotate the session id NOW, after credentials are verified but before binding the
    # authenticated client to it. Prevents session fixation: any sid that existed prior
    # to this successful login (which could have been set by an attacker before the
    # victim authenticated) is discarded and never gets bound to the real client.
    sid = rotate_sid(request)
    store.put(sid, client)
    request.session["username"] = username
    
    if rs:
        rt = cloud.serialize_token(client)
        if rt:
            await rs.set_refresh_token(rt)
            
    return {"success": True, "username": username}


@auth_router.post("/api/login/silent")
@rate_limited(cost=1.0)
async def login_silent(request: Request):
    try:
        await current_client(request)
        return {"success": True, "username": request.session.get("username", "")}
    except NotAuthenticated:
        raise HTTPException(status_code=401, detail="No valid refresh token stored")
    except Exception:
        log.exception("Unexpected error during silent login")
        raise HTTPException(status_code=500, detail="Internal server error")
