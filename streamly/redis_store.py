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
# Retention: keep roughly a month of logs in Redis (~50k lines ≈ ~10 MB, well
# under the 256 MB storage cap). This is separate from how often we FLUSH.
_LOGS_MAX_LINES = 50000


class RedisStore:
    def __init__(self, url: str, token: str, timeout: float = 3.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._headers = {"Authorization": f"Bearer {token}"}

    def _execute(self, *command: str) -> Optional[Any]:
        import time
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            try:
                r = requests.post(self.url, headers=self._headers, json=list(command), timeout=self.timeout)
                r.raise_for_status()
                return r.json().get("result")
            except requests.RequestException as e:
                cmd_name = command[0] if command else "UNKNOWN"
                if attempt < max_attempts:
                    log.warning("Upstash request failed (attempt %d/%d) for command %s: %s. Retrying...", attempt, max_attempts, cmd_name, e)
                    time.sleep(0.5)
                else:
                    log.warning("Upstash request failed final (attempt %d/%d) for command %s: %s", attempt, max_attempts, cmd_name, e)
                    return None

    def get(self, key: str) -> Optional[str]:
        result = self._execute("GET", key)
        return result if isinstance(result, str) else None

    def set(self, key: str, value: str, ex: int | None = None) -> bool:
        cleaned = self._strip_wrapping_quotes(value)
        if ex is not None:
            return self._execute("SET", key, cleaned, "EX", str(ex)) == "OK"
        return self._execute("SET", key, cleaned) == "OK"

    def delete(self, key: str) -> bool:
        res = self._execute("DEL", key)
        return bool(isinstance(res, int) and res > 0)


    @staticmethod
    def _strip_wrapping_quotes(value: str) -> str:
        s = (value or "").strip()
        while len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
            s = s[1:-1].strip()
        s = s.replace('\\"', '"').replace("\\\\", "\\")
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

    def push_logs(self, lines: list[str], max_lines: int = _LOGS_MAX_LINES) -> bool:
        """Append MANY log lines in ONE batched LPUSH (+ one LTRIM).

        This is the cost-efficient path: N lines cost 2 Redis commands total
        instead of 2*N. `lines` should be oldest-first; we reverse so the list
        stays newest-first (index 0 = newest), matching push_log/get_logs.
        Returns True on success. Never raises (logging must not crash the app).
        """
        if not lines:
            return True
        try:
            # LPUSH a b c  => list becomes c b a (last arg ends up at index 0).
            # We want the newest line at index 0, so push oldest-first as given:
            # LPUSH oldest ... newest  => newest at index 0. Correct.
            self._execute("LPUSH", _LOGS_KEY, *lines)
            self._execute("LTRIM", _LOGS_KEY, "0", str(max_lines - 1))
            return True
        except Exception as e:
            log.warning("push_logs batch failed: %s", e)
            return False

    def get_logs(self, limit: int = _LOGS_MAX_LINES) -> list[str]:
        """Return the most recent log lines in chronological (oldest-first) order."""
        result = self._execute("LRANGE", _LOGS_KEY, "0", str(limit - 1))
        if not isinstance(result, list):
            return []
        # LRANGE returns newest-first (because we LPUSH); reverse for reading.
        return [str(x) for x in reversed(result)]


