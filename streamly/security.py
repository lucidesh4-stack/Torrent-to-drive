from __future__ import annotations
from dataclasses import dataclass
from functools import wraps
import hmac
import json
import re
import secrets
import threading
import time
from typing import Any, Callable, Mapping
import ipaddress
import socket
from urllib.parse import urlsplit
from fastapi import Request, HTTPException

from .config import settings

class ValidationError(ValueError):
    """Raised when client-controlled input is malformed or unsafe."""

EMAIL_RE = re.compile(r"^[^@\s]{1,254}@[^@\s]{1,253}\.[^@\s]{2,63}$")
BTIH_RE = re.compile(r"(?:^|[?&])xt=urn:btih:([A-Fa-f0-9]{40}|[A-Za-z2-7]{32})", re.IGNORECASE)

def _get_cfg(config: Any, key: str, default: Any = None) -> Any:
    if hasattr(config, key): return getattr(config, key)
    if isinstance(config, Mapping): return config.get(key, default)
    return default

def _ip_is_public(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None: ip = mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified)

def validate_public_url(value: Any, *, allowed_schemes: tuple[str, ...] = ("http", "https")) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip(): raise ValidationError("url is required")
    url = value.strip()
    if len(url) > 2048: raise ValidationError("url too long")
    parts = urlsplit(url)
    if parts.scheme.lower() not in allowed_schemes: raise ValidationError("url scheme not allowed")
    host = parts.hostname
    if not host: raise ValidationError("url host is missing")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValidationError("url host could not be resolved") from None
    resolved = {ai[4][0] for ai in infos}
    if not resolved: raise ValidationError("url host could not be resolved")
    for ip_str in resolved:
        if not _ip_is_public(ip_str): raise ValidationError("url resolves to a non-public address")
    return url, sorted(resolved)[0]

def require_json_body(request: Request) -> dict[str, Any]:
    # FastAPI handles JSON parsing, but we can still check size
    if request.headers.get("content-length"):
        if int(request.headers.get("content-length")) > settings.max_json_bytes:
            raise ValidationError("JSON body too large")
    return {} # FastAPI's body parameters replace this logic

def require_str(data: Mapping[str, Any], key: str, *, min_len: int = 1, max_len: int = 256) -> str:
    value = data.get(key)
    if not isinstance(value, str): raise ValidationError(f"{key} must be a string")
    value = value.strip()
    if len(value) < min_len or len(value) > max_len: raise ValidationError(f"{key} length must be between {min_len} and {max_len}")
    return value

def validate_email(value: str) -> str:
    value = value.strip().lower()
    if not EMAIL_RE.match(value): raise ValidationError("Invalid email address")
    return value

def validate_password(value: str) -> str:
    if not isinstance(value, str) or not (1 <= len(value) <= 512): raise ValidationError("Invalid password")
    return value

def validate_positive_int(value: Any, *, name: str, maximum: int) -> int:
    try: parsed = int(value)
    except (TypeError, ValueError): raise ValidationError(f"{name} must be an integer") from None
    if parsed < 0 or parsed > maximum: raise ValidationError(f"{name} out of range")
    return parsed

def validate_query(q: Any) -> str:
    if not isinstance(q, str): raise ValidationError("q must be a string")
    q = " ".join(q.strip().split())
    if not q: raise ValidationError("q is required")
    if len(q) > settings.max_query_length: raise ValidationError("q too long")
    return q

def validate_item_type(value: Any) -> str:
    if value not in {"file", "folder"}: raise ValidationError("type must be 'file' or 'folder'")
    return str(value)

def validate_magnet(value: Any) -> str:
    if not isinstance(value, str): raise ValidationError("magnet must be a string")
    value = value.strip()
    if len(value) > settings.max_magnet_length: raise ValidationError("magnet too long")
    if not value.startswith("magnet:") or not BTIH_RE.search(value): raise ValidationError("Invalid magnet link")
    return value

def json_error(status: int, code: str, message: str):
    # In FastAPI, we raise HTTPException instead of returning a response object
    raise HTTPException(status_code=status, detail={"code": code, "message": message})

@dataclass
class Bucket:
    tokens: float
    updated_at: float

class TokenBucketRateLimiter:
    def __init__(self, capacity: int, refill_per_second: float, clock: Callable[[], float] = time.monotonic, max_keys: int = 50_000):
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self.clock = clock
        self._lock = threading.RLock()
        self._buckets: dict[str, Bucket] = {}
        self.max_keys = max_keys

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = self.clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self.max_keys:
                    self._evict_locked(now)
                bucket = Bucket(tokens=self.capacity, updated_at=now)
                self._buckets[key] = bucket
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_second)
            bucket.updated_at = now
            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True
            return False

    def _evict_locked(self, now: float) -> None:
        idle = [k for k, b in self._buckets.items() if min(self.capacity, b.tokens + max(0.0, now - b.updated_at) * self.refill_per_second) >= self.capacity]
        if idle:
            for k in idle: del self._buckets[k]
            return
        oldest = min(self._buckets, key=lambda k: self._buckets[k].updated_at)
        del self._buckets[oldest]

def rate_limited(cost: float = 1.0):
    def decorator(fn: Callable):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            # In FastAPI, 'request' is always passed as a keyword argument if defined in the route
            request: Request = kwargs.get("request")
            if not request:
                # Search for request in positional args
                for arg in args:
                    if isinstance(arg, Request):
                        request = arg
                        break
            
            if not request:
                return await fn(*args, **kwargs)

            # Access the limiter from app state
            limiter = request.app.state.limiter
            if not limiter:
                return await fn(*args, **kwargs)
            
            # Use session SID or IP as key
            sid = request.state.session.get("sid") or request.client.host
            key = f"{sid}:{request.url.path}"
            
            if not limiter.allow(key, cost=cost):
                raise HTTPException(status_code=429, detail={"code": "rate_limited", "message": "Too many requests"})
            
            return await fn(*args, **kwargs)
        return wrapper
    return decorator

def install_security_headers(app) -> None:
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        is_hf = "SPACE_ID" in os.environ
        response.headers["X-Content-Type-Options"] = "nosniff"
        if not is_hf:
            response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        csp_ancestors = "frame-ancestors 'self' https://huggingface.co https://*.hf.space" if is_hf else "frame-ancestors 'none'"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data: https://m.media-amazon.com https://*.media-imdb.com https://*.ytimg.com; "
            "media-src 'self' blob: https:; "
            "connect-src 'self' https://bitsearch.eu https://v3.sg.media-imdb.com https://www.seedr.cc https:; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "script-src 'self' 'unsafe-inline'; "
            "frame-src 'self' https://www.youtube.com https://www.youtube-nocookie.com; "
            f"base-uri 'none'; object-src 'none'; {csp_ancestors}"
        )
        return response
