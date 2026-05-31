from __future__ import annotations

from flask import current_app, session
from .security import ensure_sid
from .store import NotAuthenticated

def _try_restore_from_refresh(sid: str):
    rs = getattr(current_app, "rs", None)
    cloud = getattr(current_app, "cloud", None)
    store = getattr(current_app, "store", None)
    
    if not rs:
        raise NotAuthenticated("Not authenticated")
    rt = rs.get_refresh_token()
    if not rt:
        raise NotAuthenticated("Not authenticated")
    try:
        client, username = cloud.login_with_saved_token(rt)
    except PermissionError:
        rs.delete_refresh_token()
        raise NotAuthenticated("Refresh token invalid")
    store.put(sid, client)
    session["username"] = username
    from .cloud_service import CloudService
    new_rt = CloudService.serialize_token(client)
    if new_rt:
        rs.set_refresh_token(new_rt)
    current_app.logger.info("Session restored via global master token for sid=%s...", sid[:8])
    return client

def current_client():
    config = current_app.config
    cloud = getattr(current_app, "cloud", None)
    store = getattr(current_app, "store", None)
    rs = getattr(current_app, "rs", None)
    
    sid = session.get("sid")
    if not sid:
        sid = ensure_sid()
    try:
        return store.get(sid)
    except NotAuthenticated:
        try:
            return _try_restore_from_refresh(sid)
        except NotAuthenticated:
            pass
        
        # Headless mode check
        seedr_email = config.get("SEEDR_EMAIL")
        seedr_password = config.get("SEEDR_PASSWORD")
        if seedr_email and seedr_password:
            try:
                client, username = cloud.login(seedr_email, seedr_password)
                store.put(sid, client)
                session["username"] = username
                if rs:
                    from .cloud_service import CloudService
                    rt = CloudService.serialize_token(client)
                    if rt:
                        rs.set_refresh_token(rt)
                current_app.logger.info("Auto-logged in headless mode for sid=%s", sid[:8])
                return client
            except PermissionError:
                current_app.logger.error("Headless auto-login failed: invalid SEEDR_EMAIL/SEEDR_PASSWORD")
            except ConnectionError:
                raise
            except Exception:
                current_app.logger.exception("Unexpected error during headless auto-login")
        raise NotAuthenticated("Not authenticated")
