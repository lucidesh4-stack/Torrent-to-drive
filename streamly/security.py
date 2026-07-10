from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import re
import asyncio
from typing import Any
import ipaddress
import socket
from urllib.parse import urlsplit
import httpx
import inspect
from functools import wraps
from fastapi import Request, HTTPException

class ValidationError(ValueError):
    """Raised when client-controlled input is malformed or unsafe."""
    pass


EMAIL_RE = re.compile(r"^[^@\s]{1,254}@[^@\s]{1,253}\.[^@\s]{2,63}$")
BTIH_RE = re.compile(r"(?:^|[?&])xt=urn:btih:([A-Fa-f0-9]{40}|[A-Za-z2-7]{32})", re.IGNORECASE)



_CGNAT_RANGE = ipaddress.ip_network("100.64.0.0/10")  # RFC 6598 Carrier-Grade NAT


def _ip_is_public(ip_str: str) -> bool:
    """True only for globally-routable unicast addresses."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.version == 4 and ip in _CGNAT_RANGE:
        # Python's ipaddress module doesn't classify 100.64.0.0/10 as private/reserved,
        # but it's carrier-grade-NAT space, not meant to be treated as public internet --
        # explicitly excluded here since none of the checks below catch it.
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def validate_public_url(value: Any, *, allowed_schemes: tuple[str, ...] = ("http", "https")) -> tuple[str, str]:
    """Validate a user-supplied URL for safe server-side fetching (anti-SSRF)."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("url is required")
    url = value.strip()
    if len(url) > 2048:
        raise ValidationError("url too long")

    parts = urlsplit(url)
    if parts.scheme.lower() not in allowed_schemes:
        raise ValidationError("url scheme not allowed")
    host = parts.hostname
    if not host:
        raise ValidationError("url host is missing")

    try:
        loop = asyncio.get_event_loop()
        infos = await loop.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                        proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValidationError("url host could not be resolved") from None

    resolved = {ai[4][0] for ai in infos}
    if not resolved:
        raise ValidationError("url host could not be resolved")
    for ip_str in resolved:
        if not _ip_is_public(ip_str):
            raise ValidationError("url resolves to a non-public address")

    return url, sorted(resolved)[0]


async def async_pinned_get(url: str, pinned_ip: str, client: httpx.AsyncClient, **kwargs):
    """DNS-pinned GET without monkey-patching or global locks."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    kwargs.setdefault("follow_redirects", False)
    headers = dict(kwargs.pop("headers", {}))
    headers["Host"] = host
    pinned_url = url.replace(f"//{host}", f"//{pinned_ip}", 1)
    return await client.get(pinned_url, headers=headers, **kwargs)


def validate_email(value: str) -> str:
    value = value.strip().lower()
    if not EMAIL_RE.match(value):
        raise ValidationError("Invalid email address")
    return value


def validate_password(value: str) -> str:
    if not isinstance(value, str) or not (1 <= len(value) <= 512):
        raise ValidationError("Invalid password")
    return value


def validate_positive_int(value: Any, *, name: str, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be an integer") from None
    if parsed < 0 or parsed > maximum:
        raise ValidationError(f"{name} out of range")
    return parsed


def validate_query(q: Any, config: Any) -> str:
    if not isinstance(q, str):
        raise ValidationError("q must be a string")
    q = " ".join(q.strip().split())
    if not q:
        raise ValidationError("q is required")
    
    max_len = getattr(config, "max_query_length", 128)
    if len(q) > max_len:
        raise ValidationError("q too long")
    return q


def validate_item_type(value: Any) -> str:
    if value not in {"file", "folder"}:
        raise ValidationError("type must be 'file' or 'folder'")
    return str(value)


def validate_magnet(value: Any, config: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("magnet must be a string")
    value = value.strip()
    
    max_len = getattr(config, "max_magnet_length", 8192)
    if len(value) > max_len:
        raise ValidationError("magnet too long")
    if not value.startswith("magnet:") or not BTIH_RE.search(value):
        raise ValidationError("Invalid magnet link")
    return value


@dataclass
class Bucket:
    tokens: float
    updated_at: float


class TokenBucketRateLimiter:
    """Coroutine-safe (not thread-safe) in-memory token bucket rate limiter.

    Guarded by asyncio.Lock, which serializes concurrent coroutines on ONE event loop --
    it provides no protection across OS threads or separate processes. This is safe
    under the current deployment (uvicorn --workers 1, single process/event loop) but
    would silently stop being safe if that were ever changed to --workers > 1 (each
    worker is a separate process with its own memory, so rate-limit state wouldn't even
    be shared -- a bigger problem than just lock safety) or if any code path started
    calling this from a thread pool executor.
    """
    def __init__(self, capacity: int, refill_per_second: float, max_keys: int = 50_000):
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self._lock = asyncio.Lock()
        self._buckets: OrderedDict[str, Bucket] = OrderedDict()
        self.max_keys = max_keys

    async def allow(self, key: str, cost: float = 1.0) -> bool:
        now = asyncio.get_event_loop().time()
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self.max_keys:
                    self._evict_locked()
                bucket = Bucket(tokens=self.capacity, updated_at=now)
                self._buckets[key] = bucket
            else:
                self._buckets.move_to_end(key)
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_second)
            bucket.updated_at = now
            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True
            return False

    def _evict_locked(self) -> None:
        """O(1) eviction: pop least-recently-used entries until under max_keys."""
        while len(self._buckets) > self.max_keys:
            self._buckets.popitem(last=False)


def rate_limited(cost: float = 1.0):
    """Decorator to limit request rate based on request's app state limiter."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            request = kwargs.get("request")
            if request is None:
                for arg in args:
                    if isinstance(arg, Request):
                        request = arg
                        break
            
            if request is None:
                return await func(*args, **kwargs)

            limiter = getattr(request.app.state, "limiter", None)
            if limiter is not None:
                sid = request.session.get("sid") or (request.client.host if request.client else "unknown")
                key = f"{sid}:{request.url.path}"
                if not await limiter.allow(key, cost=cost):
                    raise HTTPException(status_code=429, detail="Too many requests")

            return await func(*args, **kwargs)

        wrapper.__signature__ = inspect.signature(func)
        return wrapper
    return decorator
