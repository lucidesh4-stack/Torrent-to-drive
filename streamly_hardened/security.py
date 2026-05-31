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

from flask import abort, g, jsonify, request, session

from .config import AppConfig


class ValidationError(ValueError):
    """Raised when client-controlled input is malformed or unsafe."""


EMAIL_RE = re.compile(r"^[^@\s]{1,254}@[^@\s]{1,253}\.[^@\s]{2,63}$")
BTIH_RE = re.compile(r"(?:^|[?&])xt=urn:btih:([A-Fa-f0-9]{40}|[A-Za-z2-7]{32})(?:&|$)")


def require_json_body(config: AppConfig) -> dict[str, Any]:
    if request.content_length is not None and request.content_length > config.max_json_bytes:
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


def validate_query(q: Any, config: AppConfig) -> str:
    if not isinstance(q, str):
        raise ValidationError("q must be a string")
    q = " ".join(q.strip().split())
    if not q:
        raise ValidationError("q is required")
    if len(q) > config.max_query_length:
        raise ValidationError("q too long")
    return q


def validate_category(category: Any, config: AppConfig) -> str:
    category = "" if category is None else str(category)
    if category not in config.allowed_categories:
        raise ValidationError("Invalid category")
    return category


def validate_sort(sort: Any, config: AppConfig) -> str:
    sort = "relevance" if sort in (None, "") else str(sort)
    if sort not in config.allowed_sorts:
        raise ValidationError("Invalid sort")
    return sort


def validate_order(order: Any, config: AppConfig) -> str:
    order = "desc" if order in (None, "") else str(order)
    if order not in config.allowed_orders:
        raise ValidationError("Invalid order")
    return order


def validate_item_type(value: Any) -> str:
    if value not in {"file", "folder"}:
        raise ValidationError("type must be 'file' or 'folder'")
    return str(value)


def validate_magnet(value: Any, config: AppConfig) -> str:
    if not isinstance(value, str):
        raise ValidationError("magnet must be a string")
    value = value.strip()
    if len(value) > config.max_magnet_length:
        raise ValidationError("magnet too long")
    if not value.startswith("magnet:?") or not BTIH_RE.search(value):
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

    def __init__(self, capacity: int, refill_per_second: float, clock: Callable[[], float] = time.monotonic):
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self.clock = clock
        self._lock = threading.RLock()
        self._buckets: dict[str, Bucket] = {}

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = self.clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = Bucket(tokens=self.capacity, updated_at=now)
                self._buckets[key] = bucket
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_second)
            bucket.updated_at = now
            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True
            return False

    def prune(self, older_than_seconds: float = 3600.0) -> None:
        cutoff = self.clock() - older_than_seconds
        with self._lock:
            for key, bucket in list(self._buckets.items()):
                if bucket.updated_at < cutoff:
                    del self._buckets[key]


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
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "img-src 'self' data: https://m.media-amazon.com https://*.media-imdb.com; "
            "media-src 'self' blob: https:; "
            "connect-src 'self' https://bitsearch.eu https://v3.sg.media-imdb.com https://www.seedr.cc https:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "base-uri 'none'; object-src 'none'; frame-ancestors 'none'",
        )
        if request.path.startswith("/api/") or request.path.startswith("/fs/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


def stable_json_dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
