"""Upstash Redis REST client wrapper.

Uses Upstash's HTTP REST API (no redis-py / no TCP socket needed) so it works on
any free PaaS without binary deps. Falls back gracefully if not configured.

Sends commands as a JSON array body POSTed to the root endpoint:
    ["SET", "key", "value"]
This is the canonical form that handles arbitrary string values (URLs, secrets,
binary-ish data) without URL-encoding pitfalls or accidental JSON re-quoting.
See: https://upstash.com/docs/redis/features/restapi
"""
from __future__ import annotations

import logging
import secrets
from typing import Optional, Any

import requests

log = logging.getLogger(__name__)

_SECRET_KEY = "streamly:secret_key"
_BRIDGE_KEY = "streamly:bridge_url"
_REFRESH_PREFIX = "streamly:refresh:"
_REFRESH_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days


class RedisStore:
    def __init__(self, url: str, token: str, timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._headers = {"Authorization": f"Bearer {token}"}

    def _execute(self, *command: str) -> Optional[Any]:
        """Send a Redis command as a JSON array to Upstash REST root."""
        try:
            r = requests.post(
                self.url,
                headers=self._headers,
                json=list(command),
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json().get("result")
        except requests.RequestException as e:
            log.warning("Upstash request failed: %s", e)
            return None

    def get(self, key: str) -> Optional[str]:
        result = self._execute("GET", key)
        return result if isinstance(result, str) else None

    def set(self, key: str, value: str) -> bool:
        # Strip accidental wrapping quotes from previously corrupted values
        cleaned = self._strip_wrapping_quotes(value)
        return self._execute("SET", key, cleaned) == "OK"

    @staticmethod
    def _strip_wrapping_quotes(value: str) -> str:
        """Remove any number of wrapping quote layers caused by prior bad serialization."""
        s = (value or "").strip()
        while len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
            s = s[1:-1].strip()
        # Also unescape any \" sequences left over
        s = s.replace('\\"', '"').replace("\\\\", "\\")
        return s

    # --- High level helpers ---

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
        # Always return the single global master token
        return self.get("streamly:master_refresh_token")

    def set_refresh_token(self, token: str | None) -> bool:
        """Persist the refresh token to Redis with 30-day TTL.
        FIX 2: Rejects falsy values (None, empty string) — callers must not pass them."""
        if not token:
            log.warning("set_refresh_token called with empty/None value — skipping Redis write")
            return False
        cleaned = self._strip_wrapping_quotes(token)
        return self._execute("SET", "streamly:master_refresh_token", cleaned, "EX", str(_REFRESH_TTL_SECONDS)) == "OK"

    def delete_refresh_token(self) -> bool:
        return self._execute("DEL", "streamly:master_refresh_token") is not None

    def get_bridge_url(self) -> str:
        raw = self.get(_BRIDGE_KEY) or ""
        return self._strip_wrapping_quotes(raw)

    def set_bridge_url(self, url: str) -> bool:
        return self.set(_BRIDGE_KEY, url)

    # --- History helper ---

    def get_history(self, sid: str) -> list[dict[str, Any]]:
        raw = self.get(f"streamly:history:{sid}")
        if not raw:
            return []
        try:
            import json
            return json.loads(raw)
        except Exception:
            return []

    def save_history(self, sid: str, items: list[dict[str, Any]]) -> bool:
        """Save history to Redis. Returns True on success, False on failure.
        FIX 2: Explicit return value so callers can detect and handle failures."""
        import json
        return self.set(f"streamly:history:{sid}", json.dumps(items))
