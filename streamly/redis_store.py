"""Upstash Redis REST client wrapper.

Uses Upstash HTTP REST API (no redis-py / no TCP socket needed).
Sends commands as JSON arrays: ["SET", "key", "value"]
See: https://upstash.com/docs/redis/features/restapi
"""
from __future__ import annotations

import json as _json
import logging
import secrets
import ssl
import asyncio
import datetime as _dt
from typing import Optional, Any
import httpx

log = logging.getLogger(__name__)

_SECRET_KEY = "streamly:secret_key"
_REFRESH_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days
_LOGS_KEY = "streamly:logs"
_LOGS_MAX_LINES = 50000

# Device/visitor registry: one Redis hash holding every distinct browser session ever
# seen (permanent, never auto-expired -- "keep forever" per design decision), plus a
# separate short-TTL key per session used only to compute "active right now".
_DEVICE_REGISTRY_KEY = "streamly:devices:registry"
_DEVICE_ACTIVE_KEY_PREFIX = "streamly:devices:active:"
_DEVICE_ACTIVE_TTL_SECONDS = 5 * 60  # a device is "active" if seen in the last 5 minutes


class RedisStore:
    def __init__(self, url: str, token: str, timeout: float = 3.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _execute(self, *command: str) -> Optional[Any]:
        from .core.http_client import HttpClientManager
        try:
            client = await HttpClientManager.get_instance().get_client()
        except Exception:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.set_ciphers('DEFAULT@SECLEVEL=1')
            client = httpx.AsyncClient(verify=ssl_ctx, timeout=self.timeout)

        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            try:
                r = await client.post(self.url, headers=self._headers, json=list(command), timeout=self.timeout)
                r.raise_for_status()
                return r.json().get("result")
            except (httpx.HTTPError, httpx.NetworkError) as e:
                cmd_name = command[0] if command else "UNKNOWN"
                if attempt < max_attempts:
                    log.warning("Upstash request failed (attempt %d/%d) for command %s: %s. Retrying...", attempt, max_attempts, cmd_name, e)
                    await asyncio.sleep(0.5)
                else:
                    log.warning("Upstash request failed final (attempt %d/%d) for command %s: %s", attempt, max_attempts, cmd_name, e)
                    return None

    async def get(self, key: str) -> Optional[str]:
        result = await self._execute("GET", key)
        return result if isinstance(result, str) else None

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        cleaned = self._strip_wrapping_quotes(value)
        if ex is not None:
            return await self._execute("SET", key, cleaned, "EX", str(ex)) == "OK"
        return await self._execute("SET", key, cleaned) == "OK"

    async def delete(self, key: str) -> bool:
        res = await self._execute("DEL", key)
        return bool(isinstance(res, int) and res > 0)

    @staticmethod
    def _strip_wrapping_quotes(value: str) -> str:
        s = (value or "").strip()
        while len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
            s = s[1:-1].strip()
        s = s.replace('\\"', '"').replace("\\\\", "\\")
        return s

    async def get_or_create_secret(self) -> str:
        existing = await self.get(_SECRET_KEY)
        if existing:
            return existing
        new_secret = secrets.token_hex(32)
        if await self.set(_SECRET_KEY, new_secret):
            log.info("Generated and persisted new SECRET_KEY to Upstash")
            return new_secret
        log.warning("Could not persist SECRET_KEY; using ephemeral")
        return new_secret

    async def get_refresh_token(self) -> Optional[str]:
        return await self.get("streamly:master_refresh_token")

    async def set_refresh_token(self, token: str | None) -> bool:
        """Persist token to Redis with 30-day TTL. Rejects falsy values."""
        if not token:
            log.warning("set_refresh_token called with empty/None value — skipping Redis write")
            return False
        cleaned = self._strip_wrapping_quotes(token)
        return await self._execute("SET", "streamly:master_refresh_token", cleaned, "EX", str(_REFRESH_TTL_SECONDS)) == "OK"

    async def delete_refresh_token(self) -> bool:
        return await self._execute("DEL", "streamly:master_refresh_token") is not None

    async def get_history(self, sid: str) -> list[dict[str, Any]]:
        raw = await self.get(f"streamly:history:{sid}")
        if not raw:
            return []
        try:
            return _json.loads(raw)
        except Exception:
            return []

    async def save_history(self, sid: str, items: list[dict[str, Any]]) -> bool:
        """Save history to Redis. Returns True on success, False on failure."""
        return await self.set(f"streamly:history:{sid}", _json.dumps(items))

    async def push_log(self, line: str, max_lines: int = _LOGS_MAX_LINES) -> None:
        """Append a single formatted log line to a capped Redis list."""
        await self._execute("LPUSH", _LOGS_KEY, line)
        await self._execute("LTRIM", _LOGS_KEY, "0", str(max_lines - 1))

    async def push_logs(self, lines: list[str], max_lines: int = _LOGS_MAX_LINES) -> bool:
        """Append MANY log lines in ONE batched LPUSH (+ one LTRIM)."""
        if not lines:
            return True
        try:
            await self._execute("LPUSH", _LOGS_KEY, *lines)
            await self._execute("LTRIM", _LOGS_KEY, "0", str(max_lines - 1))
            return True
        except Exception as e:
            log.warning("push_logs batch failed: %s", e)
            return False

    async def get_logs(self, limit: int = _LOGS_MAX_LINES) -> list[str]:
        """Return the most recent log lines in chronological (oldest-first) order."""
        result = await self._execute("LRANGE", _LOGS_KEY, "0", str(limit - 1))
        if not isinstance(result, list):
            return []
        return [str(x) for x in reversed(result)]

    # ---- Device/visitor registry ----------------------------------------------
    # Tracks every distinct browser session (sid) that has ever hit the app, plus
    # whether it's currently "active" (seen within the last _DEVICE_ACTIVE_TTL_SECONDS).
    # Intentionally does NOT store IP address (imprecise/misleading signal on shared
    # or mobile networks -- see design discussion) or the raw sid itself in any
    # client-facing response (only a one-way, non-reversible short hash of it, so a
    # session cookie value is never exposed back out through this feature).

    async def record_device_seen(self, sid: str, label: str) -> None:
        """Record that `sid` (already-hashed display id, not the raw cookie) was just
        seen, with a human-readable `label` (e.g. "Chrome on Android"). Cheap,
        best-effort: failures here should never break the request that triggered them
        (callers should fire this off as a background task, not await it inline)."""
        try:
            now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
            existing_raw = await self._execute("HGET", _DEVICE_REGISTRY_KEY, sid)
            first_seen = now_iso
            if existing_raw:
                try:
                    existing = _json.loads(existing_raw)
                    first_seen = existing.get("first_seen") or now_iso
                except Exception:
                    pass
            record = _json.dumps({"label": label, "first_seen": first_seen, "last_seen": now_iso})
            await self._execute("HSET", _DEVICE_REGISTRY_KEY, sid, record)
            await self._execute("SET", f"{_DEVICE_ACTIVE_KEY_PREFIX}{sid}", "1", "EX", str(_DEVICE_ACTIVE_TTL_SECONDS))
        except Exception as e:
            log.debug("record_device_seen failed (non-fatal): %s", e)

    async def get_known_devices(self) -> list[dict[str, Any]]:
        """Return every known device (sid) ever recorded, each with its label,
        first/last-seen timestamps, and whether it's currently active. Sorted by
        most-recently-seen first."""
        raw = await self._execute("HGETALL", _DEVICE_REGISTRY_KEY)
        if not raw or not isinstance(raw, list):
            return []
        # Upstash returns HGETALL as a flat [field, value, field, value, ...] list.
        pairs = list(zip(raw[0::2], raw[1::2]))
        devices: list[dict[str, Any]] = []
        for sid, record_raw in pairs:
            try:
                record = _json.loads(record_raw)
            except Exception:
                continue
            active_marker = await self._execute("GET", f"{_DEVICE_ACTIVE_KEY_PREFIX}{sid}")
            devices.append({
                "device_id": sid,
                "label": record.get("label") or "Unknown device",
                "first_seen": record.get("first_seen"),
                "last_seen": record.get("last_seen"),
                "active": bool(active_marker),
            })
        devices.sort(key=lambda d: d.get("last_seen") or "", reverse=True)
        return devices

