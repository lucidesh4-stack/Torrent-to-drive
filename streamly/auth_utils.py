from __future__ import annotations

import asyncio
import time
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


_MASTER_TOKEN_LOCK = asyncio.Lock()


async def current_client(request: Request):
    config = request.app.state.config
    cloud = request.app.state.cloud
    store = request.app.state.store
    rs = request.app.state.rs
    
    sid = request.session.get("sid")
    if not sid:
        sid = ensure_sid(request)
        
    # 1. Fast path: check in-memory store for this session id
    try:
        c = store.get(sid)
        if c:
            request.session.setdefault("site_auth", True)
            return c
    except NotAuthenticated:
        pass

    # 2. Fast path: check if ANY active client exists in store (single-tenant app)
    # If another request just restored the session, immediately bind it to this sid!
    if store and hasattr(store, "_items") and store._items:
        try:
            now = store.clock() if hasattr(store, "clock") else time.time()
            for _, entry in list(store._items.items()):
                if entry and getattr(entry, "expires_at", 0) > now and getattr(entry, "value", None):
                    active_client = entry.value
                    store.put(sid, active_client)
                    username = getattr(active_client, "username", "") or request.session.get("username", "")
                    if username:
                        request.session["username"] = username
                    request.session["site_auth"] = True
                    return active_client
        except Exception:
            pass

    # 3. Serialized token restoration via _MASTER_TOKEN_LOCK to prevent Seedr OAuth collision
    async with _MASTER_TOKEN_LOCK:
        # Re-check store after acquiring lock
        try:
            c = store.get(sid)
            if c:
                request.session.setdefault("site_auth", True)
                return c
        except NotAuthenticated:
            pass

        if not rs:
            # Check headless mode
            seedr_email = config.seedr_email
            seedr_password = config.seedr_password
            if seedr_email and seedr_password:
                try:
                    client, username = await cloud.login(seedr_email, seedr_password)
                    store.put(sid, client)
                    request.session["username"] = username
                    request.session["site_auth"] = True
                    return client
                except Exception as e:
                    log.error("Headless auto-login failed: %s", e)
            raise NotAuthenticated("Not authenticated")

        rt = await rs.get_refresh_token()
        if not rt:
            seedr_email = config.seedr_email
            seedr_password = config.seedr_password
            if seedr_email and seedr_password:
                try:
                    client, username = await cloud.login(seedr_email, seedr_password)
                    store.put(sid, client)
                    request.session["username"] = username
                    request.session["site_auth"] = True
                    new_rt = cloud.serialize_token(client)
                    if new_rt:
                        await rs.set_refresh_token(new_rt)
                    log.info("Auto-logged in headless mode for sid=%s", sid[:8])
                    return client
                except Exception as e:
                    log.error("Headless auto-login failed: %s", e)
            raise NotAuthenticated("Not authenticated")

        try:
            client, username = await cloud.login_with_saved_token(rt)
            store.put(sid, client)
            request.session["username"] = username
            request.session["site_auth"] = True
            new_rt = cloud.serialize_token(client)
            if new_rt:
                await rs.set_refresh_token(new_rt)
            log.info("Session restored via global master token for sid=%s...", sid[:8])
            return client
        except PermissionError:
            log.warning("Saved refresh token was rejected by Seedr OAuth")
            # Try headless login fallback before throwing NotAuthenticated
            seedr_email = config.seedr_email
            seedr_password = config.seedr_password
            if seedr_email and seedr_password:
                try:
                    client, username = await cloud.login(seedr_email, seedr_password)
                    store.put(sid, client)
                    request.session["username"] = username
                    request.session["site_auth"] = True
                    new_rt = cloud.serialize_token(client)
                    if new_rt:
                        await rs.set_refresh_token(new_rt)
                    log.info("Recovered session via headless login fallback for sid=%s", sid[:8])
                    return client
                except Exception:
                    pass
            raise NotAuthenticated("Refresh token invalid")
