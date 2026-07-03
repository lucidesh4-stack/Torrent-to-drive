from __future__ import annotations

import secrets
import logging
from fastapi import Request
from .store import NotAuthenticated

log = logging.getLogger(__name__)


def ensure_sid(request: Request) -> str:
    sid = request.session.get("sid")
    if not sid:
        sid = secrets.token_urlsafe(32)
        request.session["sid"] = sid
    return sid


def rotate_sid(request: Request) -> str:
    """Issue a brand-new session id, discarding whatever was there before.

    Must be called on successful authentication (never before), so that a session id
    that existed prior to login -- which could have been planted in a victim's browser
    by an attacker (classic session fixation: attacker sets a known sid, victim logs in,
    attacker's pre-known sid is now valid for the victim's authenticated session) --
    is never the one bound to the newly-authenticated client.
    """
    sid = secrets.token_urlsafe(32)
    request.session["sid"] = sid
    return sid


def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def current_client(request: Request):
    config = request.app.state.config
    cloud = request.app.state.cloud
    store = request.app.state.store
    rs = request.app.state.rs
    
    sid = request.session.get("sid")
    if not sid:
        sid = ensure_sid(request)
        
    try:
        return store.get(sid)
    except NotAuthenticated:
        try:
            if not rs:
                raise NotAuthenticated("Not authenticated")
            rt = await rs.get_refresh_token()
            if not rt:
                raise NotAuthenticated("Not authenticated")
            try:
                client, username = await cloud.login_with_saved_token(rt)
            except PermissionError:
                await rs.delete_refresh_token()
                raise NotAuthenticated("Refresh token invalid")
            store.put(sid, client)
            request.session["username"] = username
            new_rt = cloud.serialize_token(client)
            if new_rt:
                await rs.set_refresh_token(new_rt)
            log.info("Session restored via global master token for sid=%s...", sid[:8])
            return client
        except NotAuthenticated:
            pass
        
        # Headless mode check
        seedr_email = config.seedr_email
        seedr_password = config.seedr_password
        if seedr_email and seedr_password:
            try:
                client, username = await cloud.login(seedr_email, seedr_password)
                store.put(sid, client)
                request.session["username"] = username
                if rs:
                    rt = cloud.serialize_token(client)
                    if rt:
                        await rs.set_refresh_token(rt)
                log.info("Auto-logged in headless mode for sid=%s", sid[:8])
                return client
            except PermissionError:
                log.error("Headless auto-login failed: invalid SEEDR_EMAIL/SEEDR_PASSWORD")
            except ConnectionError:
                raise
            except Exception:
                log.exception("Unexpected error during headless auto-login")
        raise NotAuthenticated("Not authenticated")
