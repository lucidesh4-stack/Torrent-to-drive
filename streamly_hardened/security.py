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

from flask import abort, g, jsonify, request, session

from .config import AppConfig


class ValidationError(ValueError):
    """Raised when client-controlled input is malformed or unsafe."""


EMAIL_RE = re.compile(r"^[^@\s]{1,254}@[^@\s]{1,253}\.[^@\s]{2,63}$")
BTIH_RE = re.compile(r"(?:^|[?&])xt=urn:btih:([A-Fa-f0-9]{40}|[A-Za-z2-7]{32})", re.IGNORECASE)


def _get_cfg(config: Any, key: str, default: Any = None) -> Any:
    """Safely get config value from either a dataclass or a dictionary."""
    if hasattr(config, key):
        return getattr(config, key)
    if isinstance(config, Mapping):
        return config.get(key, default)
    return default


def _ip_is_public(ip_str: str) -> bool:
    """True only for globally-routable unicast addresses.

    Rejects loopback, private (RFC1918), link-local (incl. 169.254.0.0/16 cloud
    metadata), unique-local, multicast, reserved and unspecified ranges — for
    both IPv4 and IPv6 (including IPv4-mapped IPv6).
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_public_url(value: Any, *, allowed_schemes: tuple[str, ...] = ("http", "https")) -> tuple[str, str]:
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
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
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


def pinned_get(url: str, pinned_ip: str, **kwargs):
    """requests.get that connects to `pinned_ip` instead of re-resolving the host.

    Anti-DNS-rebinding: validate_public_url() resolves+vets the host's IP, but a
    plain requests.get(host) re-resolves DNS, letting an attacker flip the record
    to a private address (TOCTOU). We mount a tiny adapter that overrides the
    connection pool's resolved address to the already-vetted IP while keeping the
    original Host header and TLS SNI (server_hostname) intact.

    `allow_redirects` defaults to False here: a 30x could point at a fresh host
    that would NOT be pinned. Callers that must follow redirects should re-validate
    each hop themselves.
    """
    import requests
    from urllib3.util import connection as _urllib3_conn

    parts = urlsplit(url)
    host = parts.hostname or ""
    kwargs.setdefault("allow_redirects", False)

    _orig_create_conn = _urllib3_conn.create_connection

    def _pinned_create_connection(address, *a, **kw):
        _h, port = address
        # Force the connection to the vetted IP; SNI/Host stay = original host.
        return _orig_create_conn((pinned_ip, port), *a, **kw)

    _urllib3_conn.create_connection = _pinned_create_connection
    try:
        return requests.get(url, **kwargs)
    finally:
        _urllib3_conn.create_connection = _orig_create_conn


def require_json_body(config: Any) -> dict[str, Any]:
    max_bytes = _get_cfg(config, "max_json_bytes", 16 * 1024)
    if request.content_length is not None and request.content_length > max_bytes:
        raise ValidationError("JSON body too large")
    if not request.is_json:
        raise ValidationError("Expected application/json")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("Expected JSON object")
    return data


def require_str(data: Mapping[str, Any], key: str, *, min_len: int = 1, max_len: int = 256) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValidationError(f"{key} must be a string")
    value = value.strip()
    if len(value) < min_len or len(value) > max_len:
        raise ValidationError(f"{key} length must be between {min_len} and {max_len}")
    return value


def validate_email(value: str) -> str:
    value = value.strip().lower()
    if not EMAIL_RE.match(value):
        raise ValidationError("Invalid email address")
    return value


def validate_password(value: str) -> str:
    # Do not enforce composition rules here; Seedr owns auth policy.
    # Length bounds prevent accidental huge allocations/log spam.
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
    
    max_len = _get_cfg(config, "max_query_length", 128)
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
    
    max_len = _get_cfg(config, "max_magnet_length", 8192)
    if len(value) > max_len:
        raise ValidationError("magnet too long")
    if not value.startswith("magnet:") or not BTIH_RE.search(value):
        raise ValidationError("Invalid magnet link")
    return value


def json_error(status: int, code: str, message: str):
    response = jsonify({"success": False, "error": {"code": code, "message": message}})
    response.status_code = status
    return response


def ensure_sid() -> str:
    sid = session.get("sid")
    if not sid:
        sid = secrets.token_urlsafe(32)
        session["sid"] = sid
    return sid


def get_csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def csrf_required(fn: Callable):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = session.get("csrf")
        supplied = request.headers.get("X-CSRF-Token", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            return json_error(403, "csrf_failed", "CSRF validation failed")
        return fn(*args, **kwargs)

    return wrapper


@dataclass
class Bucket:
    tokens: float
    updated_at: float


class TokenBucketRateLimiter:
    """Thread-safe in-memory token bucket.

    This protects a single process. At global scale, replace with Redis/Dragonfly or an API gateway
    limiter keyed by user + route. The interface is deliberately tiny for easy replacement.
    """

    def __init__(self, capacity: int, refill_per_second: float, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = 50_000):
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self.clock = clock
        self._lock = threading.RLock()
        self._buckets: dict[str, Bucket] = {}
        # Cap the number of tracked keys so a spoofed-XFF / many-endpoint flood
        # cannot grow this dict without bound (memory-DoS) in the long-lived worker.
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
        """Drop buckets that have fully refilled (idle) — they carry no rate state.
        If none are idle (all actively limited), evict the least-recently-updated
        one so the map stays bounded. Caller holds the lock."""
        idle = [k for k, b in self._buckets.items()
                if min(self.capacity, b.tokens + max(0.0, now - b.updated_at) * self.refill_per_second) >= self.capacity]
        if idle:
            for k in idle:
                del self._buckets[k]
            return
        oldest = min(self._buckets, key=lambda k: self._buckets[k].updated_at)
        del self._buckets[oldest]



def rate_limited(cost: float = 1.0):
    def decorator(fn: Callable):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            from .extensions import limiter
            if limiter is None:
                return fn(*args, **kwargs)
            sid = session.get("sid") or request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
            key = f"{sid}:{request.endpoint}"
            if not limiter.allow(key, cost=cost):
                return json_error(429, "rate_limited", "Too many requests")
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def install_security_headers(app) -> None:
    @app.after_request
    def set_headers(response):
        import os
        is_hf = "SPACE_ID" in os.environ
        
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        
        if is_hf:
            # Hugging Face runs in an iframe on huggingface.co and hf.space
            # Remove X-Frame-Options to allow CSP frame-ancestors to govern framing behavior
            if "X-Frame-Options" in response.headers:
                del response.headers["X-Frame-Options"]
        else:
            response.headers.setdefault("X-Frame-Options", "DENY")
            
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        
        csp_ancestors = "frame-ancestors 'self' https://huggingface.co https://*.hf.space" if is_hf else "frame-ancestors 'none'"
        
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "img-src 'self' data: https://m.media-amazon.com https://*.media-imdb.com; "
            "media-src 'self' blob: https:; "
            "connect-src 'self' https://bitsearch.eu https://v3.sg.media-imdb.com https://www.seedr.cc https:; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "script-src 'self' 'unsafe-inline'; "
            f"base-uri 'none'; object-src 'none'; {csp_ancestors}",
        )
        if request.path.startswith("/api/") or request.path.startswith("/fs/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

