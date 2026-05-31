"""Upstash Redis REST client wrapper.

Uses Upstash HTTP REST API (no redis-py / no TCP socket needed).
Sends commands as JSON arrays: ["SET", "key", "value"]
See: https://upstash.com/docs/redis/features/restapi
"""
from __future__ import annotations

import json as _json
import logging
import secrets
from typing import Optional, Any

import requests

log = logging.getLogger(__name__)

_SECRET_KEY = "streamly:secret_key"
_REFRESH_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days
_LOGS_KEY = "streamly:logs"
_LOGS_MAX_LINES = 2000


class RedisStore:
    def __init__(self, url: str, token: str, timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._headers = {"Authorization": f"Bearer {token}"}

    def _execute(self, *command: str) -> Optional[Any]:
        try:
            r = requests.post(self.url, headers=self._headers, json=list(command), timeout=self.timeout)
            r.raise_for_status()
            return r.json().get("result")
        except requests.RequestException as e:
            log.warning("Upstash request failed: %s", e)
            return None

    def get(self, key: str) -> Optional[str]:
        result = self._execute("GET", key)
        return result if isinstance(result, str) else None

    def set(self, key: str, value: str) -> bool:
        cleaned = self._strip_wrapping_quotes(value)
        return self._execute("SET", key, cleaned) == "OK"

    @staticmethod
    def _strip_wrapping_quotes(value: str) -> str:
        s = (value or "").strip()
        while len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
            s = s[1:-1].strip()
        s = s.replace('\\"', '"').replace("\\", "\\")
        return s

    def get_or_create_secret(self) -> str:
        existing = self.get(_SECRET_KEY)
        if existing:
            return existing
        new_secret = secrets.token_hex(32)
        if self.set(_SECRET_KEY, new_secret):
            log.info("Generated and persisted new SECRET_KEY to Upstash")
            return new_secret
        log.warning("Could not persist SECRET_KEY; using ephemeral")
        return new_secret

    def get_refresh_token(self) -> Optional[str]:
        return self.get("streamly:master_refresh_token")

    def set_refresh_token(self, token: str | None) -> bool:
        """Persist token to Redis with 30-day TTL. Rejects falsy values."""
        if not token:
            log.warning("set_refresh_token called with empty/None value — skipping Redis write")
            return False
        cleaned = self._strip_wrapping_quotes(token)
        return self._execute("SET", "streamly:master_refresh_token", cleaned, "EX", str(_REFRESH_TTL_SECONDS)) == "OK"

    def delete_refresh_token(self) -> bool:
        return self._execute("DEL", "streamly:master_refresh_token") is not None

    def get_history(self, sid: str) -> list[dict[str, Any]]:
        raw = self.get(f"streamly:history:{sid}")
        if not raw:
            return []
        try:
            return _json.loads(raw)
        except Exception:
            return []

    def save_history(self, sid: str, items: list[dict[str, Any]]) -> bool:
        """Save history to Redis. Returns True on success, False on failure."""
        return self.set(f"streamly:history:{sid}", _json.dumps(items))

    def push_log(self, line: str, max_lines: int = _LOGS_MAX_LINES) -> None:
        """Append a single formatted log line to a capped Redis list.

        Newest entries are at index 0 (LPUSH). The list is trimmed to the most
        recent `max_lines` entries. Failures are silently ignored so that
        logging can never crash the application.
        """
        self._execute("LPUSH", _LOGS_KEY, line)
        self._execute("LTRIM", _LOGS_KEY, "0", str(max_lines - 1))

    def get_logs(self, limit: int = _LOGS_MAX_LINES) -> list[str]:
        """Return the most recent log lines in chronological (oldest-first) order."""
        result = self._execute("LRANGE", _LOGS_KEY, "0", str(limit - 1))
        if not isinstance(result, list):
            return []
        # LRANGE returns newest-first (because we LPUSH); reverse for reading.
        return [str(x) for x in reversed(result)]

    # --- Daily bitsearch request meter -------------------------------------
    @staticmethod
    def _today_key() -> str:
        import datetime as _dt
        return "streamly:bitsearch_count:" + _dt.datetime.utcnow().strftime("%Y-%m-%d")

    def incr_request_count(self, n: int = 1) -> int:
        """Increment today's bitsearch request counter; returns the new total.

        The key auto-expires after 48h so old days clean themselves up.
        Returns -1 on failure (caller should treat as 'unknown').
        """
        key = self._today_key()
        result = self._execute("INCRBY", key, str(max(1, int(n))))
        # best-effort TTL; ignore failures
        self._execute("EXPIRE", key, str(60 * 60 * 48))
        try:
            return int(result)
        except (TypeError, ValueError):
            return -1

    def get_request_count(self) -> int:
        """Return today's bitsearch request count (0 if none / unknown)."""
        raw = self.get(self._today_key())
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
