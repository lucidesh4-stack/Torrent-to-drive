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
from typing import Optional, Any
import httpx

log = logging.getLogger(__name__)

_SECRET_KEY = "streamly:secret_key"
_REFRESH_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days
_LOGS_KEY = "streamly:logs"
_LOGS_MAX_LINES = 50000


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

    async def get_offcloud_key(self) -> Optional[str]:
        """Retrieve the Offcloud API Key from Redis."""
        return await self.get("streamly:offcloud:api_key")

    async def save_offcloud_key(self, key: str) -> bool:
        """Save the Offcloud API Key to Redis."""
        return await self.set("streamly:offcloud:api_key", key)

    async def get_offcloud_submissions(self) -> list[dict[str, Any]]:
        raw = await self.get("streamly:offcloud:submissions")
        if not raw:
            return []
        try:
            return _json.loads(raw)
        except Exception:
            return []

    async def save_offcloud_submissions(self, items: list[dict[str, Any]]) -> bool:
        """Save the tracked Offcloud submissions list. Capped by the caller to bound growth."""
        return await self.set("streamly:offcloud:submissions", _json.dumps(items))

    async def get_offcloud_deleted_ids(self) -> set[str]:
        """Get the set of deleted Offcloud request IDs."""
        res = await self._execute("SMEMBERS", "streamly:offcloud:deleted_ids")
        if isinstance(res, list):
            return set(str(x) for x in res)
        return set()

    async def add_offcloud_deleted_id(self, request_id: str) -> bool:
        """Add a request ID to the set of deleted Offcloud request IDs."""
        res = await self._execute("SADD", "streamly:offcloud:deleted_ids", request_id)
        return bool(isinstance(res, int) and res > 0)

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
