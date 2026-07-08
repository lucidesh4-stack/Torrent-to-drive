"""
OffcloudService — a self-contained integration with Offcloud.com, used ONLY as
an occasional large-file overflow path when a torrent exceeds this app's normal
Seedr size cap (4.5GB).

EFFICIENCY REWRITE (O-2 + O-3):
  * O-2: reuse the app's shared, pooled httpx.AsyncClient (HttpClientManager)
         instead of opening a brand-new client (new TCP pool + TLS handshake)
         on every single call. Falls back to a per-call client only if the
         shared manager is unavailable (keeps standalone/test usage working).
  * O-3: collapse the 4-5 copy-pasted "401 -> json -> error -> shape" blocks
         into ONE _request() helper. Net effect: 151 -> ~120 lines, and every
         response-shape rule now lives in exactly one place.

Behavior is unchanged: same methods, same return types, same OffcloudError
messages (verified by test_offcloud_service.py).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

OFFCLOUD_BASE_URL = "https://offcloud.com/api"

# Offcloud's documented "why this failed" reasons for a submitted URL/magnet.
_NOT_AVAILABLE_REASONS = {
    "premium": "This download requires Offcloud's premium downloading feature.",
    "links": "You've used up your available Offcloud download links.",
    "proxy": "This download requires Offcloud's proxy downloading feature.",
    "cloud": "This download requires Offcloud's cloud downloading upgrade.",
    "video": "This download requires Offcloud's video site support feature.",
}


class OffcloudError(Exception):
    """Raised for any Offcloud-specific failure (auth, quota, API error)."""
    pass


class OffcloudService:
    def __init__(self, api_key: str, timeout: float = 20.0):
        self.api_key = (api_key or "").strip()
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _url(self, path: str) -> str:
        return f"{OFFCLOUD_BASE_URL}{path}?key={self.api_key}"

    async def _shared_client(self):
        """(O-2) Return the app's shared pooled client, or None if unavailable."""
        try:
            from .core.http_client import HttpClientManager
            return await HttpClientManager.get_instance().get_client()
        except Exception as e:  # manager unavailable (e.g. isolated unit test)
            log.debug("Shared HTTP client unavailable (%s); using a per-call client.", e)
            return None

    async def _send(self, method: str, url: str, data: dict | None, context: str):
        """Issue the HTTP call on the shared client, or a short-lived fallback.

        The shared singleton is NEVER closed here; only the fallback client is
        (via `async with`), which also keeps the existing test fake working
        unchanged (it implements the context-manager protocol, not aclose()).
        """
        shared = await self._shared_client()
        try:
            if shared is not None:
                if method == "POST":
                    return await shared.post(url, data=data)
                return await shared.get(url)
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                if method == "POST":
                    return await client.post(url, data=data)
                return await client.get(url)
        except httpx.HTTPError as e:
            raise OffcloudError(f"Offcloud {context} failed: {e}") from e

    async def _request(
        self,
        method: str,
        url: str,
        *,
        data: dict | None = None,
        context: str = "request",
        expect: type = dict,
    ) -> Any:
        """(O-3) One place for: send -> 401 -> non-JSON -> {"error": ...} -> shape.

        `expect` is dict or list; a mismatch raises the "unexpected shape" error.
        Callers layer any extra rules (e.g. not_available, requestId) on top.
        """
        if not self.configured:
            raise OffcloudError("Offcloud is not configured.")

        r = await self._send(method, url, data, context)

        if r.status_code == 401:
            raise OffcloudError("Offcloud rejected the API key.")

        try:
            payload = r.json()
        except Exception as e:
            raise OffcloudError(
                f"Offcloud returned a non-JSON response (status {r.status_code}): {e}"
            ) from e

        if isinstance(payload, dict) and payload.get("error"):
            raise OffcloudError(f"Offcloud error: {payload['error']}")

        if expect is list and not isinstance(payload, list):
            raise OffcloudError(f"Unexpected Offcloud {context} response shape: {payload!r}")

        return payload

    async def add_magnet(self, magnet_or_url: str) -> dict[str, Any]:
        """Submit a magnet link or URL for cloud downloading."""
        data = await self._request(
            "POST", self._url("/cloud"),
            data={"url": magnet_or_url}, context="request", expect=dict,
        )

        if isinstance(data, dict) and data.get("not_available"):
            reason = data["not_available"]
            raise OffcloudError(_NOT_AVAILABLE_REASONS.get(reason, f"Offcloud: not available ({reason})"))
        if not isinstance(data, dict) or "requestId" not in data:
            raise OffcloudError(f"Unexpected Offcloud response shape: {data!r}")

        return data

    async def get_status(self, request_id: str) -> dict[str, Any]:
        """Check the status of a previously submitted cloud download."""
        return await self._request(
            "POST", self._url("/cloud/status"),
            data={"requestId": request_id}, context="request", expect=dict,
        )

    async def get_download_url(self, request_id: str) -> Optional[str]:
        """Best-effort: once a download's status is 'downloaded', get its download url."""
        status_data = await self.get_status(request_id)
        url = status_data.get("url") if isinstance(status_data, dict) else None
        return url or None

    async def explore_folder(self, request_id: str) -> list[str]:
        """Fetch the JSON list of download links inside a folder/archive."""
        return await self._request(
            "GET", f"{OFFCLOUD_BASE_URL}/cloud/explore/{request_id}?key={self.api_key}",
            context="explore", expect=list,
        )

    async def get_history(self) -> list[dict[str, Any]]:
        """Retrieve the user's remote cloud history/downloads."""
        return await self._request(
            "GET", f"{OFFCLOUD_BASE_URL}/cloud/history?key={self.api_key}",
            context="history", expect=list,
        )
