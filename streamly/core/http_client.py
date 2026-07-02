from __future__ import annotations
import httpx
import logging
import ssl
from typing import Any, Optional
from ..config import settings

log = logging.getLogger(__name__)

class AsyncHTTPClient:
    def __init__(self):
        # Create a custom SSL context to handle 'UNEXPECTED_EOF_WHILE_READING'
        # by allowing slightly more flexible SSL settings for the proxy
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.set_ciphers('DEFAULT@SECLEVEL=1') 
        
        self.client = httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
            verify=self.ssl_context,
            headers={"User-Agent": "CloudFlow/2.0 (Async Stable)"}
        )

    async def get(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> httpx.Response:
        try:
            resp = await self.client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as e:
            log.error(f"HTTP Error {e.response.status_code} for {url}")
            raise
        except httpx.RequestError as e:
            log.error(f"Request Error for {url}: {e}")
            raise

    async def post(self, url: str, json: Optional[dict] = None, data: Optional[dict] = None, headers: Optional[dict] = None) -> httpx.Response:
        try:
            resp = await self.client.post(url, json=json, data=data, headers=headers)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as e:
            log.error(f"HTTP Error {e.response.status_code} for {url}")
            raise
        except httpx.RequestError as e:
            log.error(f"Request Error for {url}: {e}")
            raise

    async def close(self):
        await self.client.aclose()

http_client = AsyncHTTPClient()
