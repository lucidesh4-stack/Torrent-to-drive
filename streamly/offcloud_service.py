"""
OffcloudService — a self-contained, independent integration with Offcloud.com,
used ONLY as an occasional large-file overflow path when a torrent exceeds
this app's normal Seedr size cap (4.5GB).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

OFFCLOUD_BASE_URL = "https://offcloud.com/api"

# Offcloud's documented "why this failed" reasons for a submitted URL/magnet.
# Surfaced back to the caller as a clear, human-readable message rather than a
# raw API code, since these usually mean "you need to upgrade/pay", not a bug.
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

    async def add_magnet(self, magnet_or_url: str) -> dict[str, Any]:
        """Submit a magnet link or URL for cloud downloading."""
        if not self.configured:
            raise OffcloudError("Offcloud is not configured.")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                r = await client.post(self._url("/cloud"), data={"url": magnet_or_url})
            except httpx.HTTPError as e:
                raise OffcloudError(f"Offcloud request failed: {e}") from e

        if r.status_code == 401:
            raise OffcloudError("Offcloud rejected the API key.")
        try:
            data = r.json()
        except Exception as e:
            raise OffcloudError(f"Offcloud returned a non-JSON response (status {r.status_code}): {e}") from e

        if isinstance(data, dict) and data.get("not_available"):
            reason = data["not_available"]
            raise OffcloudError(_NOT_AVAILABLE_REASONS.get(reason, f"Offcloud: not available ({reason})"))
        if isinstance(data, dict) and data.get("error"):
            raise OffcloudError(f"Offcloud error: {data['error']}")
        if not isinstance(data, dict) or "requestId" not in data:
            raise OffcloudError(f"Unexpected Offcloud response shape: {data!r}")

        return data

    async def get_status(self, request_id: str) -> dict[str, Any]:
        """Check the status of a previously submitted cloud download."""
        if not self.configured:
            raise OffcloudError("Offcloud is not configured.")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                r = await client.post(self._url("/cloud/status"), data={"requestId": request_id})
            except httpx.HTTPError as e:
                raise OffcloudError(f"Offcloud request failed: {e}") from e

        if r.status_code == 401:
            raise OffcloudError("Offcloud rejected the API key.")
        try:
            data = r.json()
        except Exception as e:
            raise OffcloudError(f"Offcloud returned a non-JSON response (status {r.status_code}): {e}") from e

        if isinstance(data, dict) and data.get("error"):
            raise OffcloudError(f"Offcloud error: {data['error']}")

        return data

    async def get_download_url(self, request_id: str) -> Optional[str]:
        """Best-effort: once a download's status is 'downloaded', get its download url."""
        status_data = await self.get_status(request_id)
        url = status_data.get("url") if isinstance(status_data, dict) else None
        return url or None

    async def explore_folder(self, request_id: str) -> list[str]:
        """Fetch the JSON list of download links inside a folder/archive."""
        if not self.configured:
            raise OffcloudError("Offcloud is not configured.")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                r = await client.get(f"{OFFCLOUD_BASE_URL}/cloud/explore/{request_id}?key={self.api_key}")
            except httpx.HTTPError as e:
                raise OffcloudError(f"Offcloud explore request failed: {e}") from e

        if r.status_code == 401:
            raise OffcloudError("Offcloud rejected the API key.")
        try:
            data = r.json()
        except Exception as e:
            raise OffcloudError(f"Offcloud explore returned non-JSON (status {r.status_code}): {e}") from e

        if isinstance(data, dict) and data.get("error"):
            raise OffcloudError(f"Offcloud error: {data['error']}")
        if not isinstance(data, list):
            raise OffcloudError(f"Unexpected Offcloud explore response shape: {data!r}")

        return data

    async def get_history(self) -> list[dict[str, Any]]:
        """Retrieve the user's remote cloud history/downloads."""
        if not self.configured:
            raise OffcloudError("Offcloud is not configured.")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                r = await client.get(f"{OFFCLOUD_BASE_URL}/cloud/history?key={self.api_key}")
            except httpx.HTTPError as e:
                raise OffcloudError(f"Offcloud history request failed: {e}") from e

        if r.status_code == 401:
            raise OffcloudError("Offcloud rejected the API key.")
        try:
            data = r.json()
        except Exception as e:
            raise OffcloudError(f"Offcloud history returned non-JSON (status {r.status_code}): {e}") from e

        if isinstance(data, dict) and data.get("error"):
            raise OffcloudError(f"Offcloud error: {data['error']}")
        if not isinstance(data, list):
            raise OffcloudError(f"Unexpected Offcloud history response shape: {data!r}")

        return data
