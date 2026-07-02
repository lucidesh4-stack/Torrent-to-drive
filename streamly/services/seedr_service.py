from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional, Tuple
from ..core.http_client import http_client

log = logging.getLogger(__name__)

class SeedrError(Exception): pass
class SeedrAuthError(SeedrError): pass
class SeedrRateLimitError(SeedrError): pass
class SeedrNotFoundError(SeedrError): pass

class SeedrService:
    BASE_URL = "https://www.seedr.cc"
    API_URL = f"{BASE_URL}/api"
    
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self._token: Optional[str] = None

    async def authenticate(self) -> str:
        url = f"{self.API_URL}/account"
        data = {"username": self.username, "password": self.password}
        try:
            resp = await http_client.post(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
            result = resp.json()
            if result.get("error"): raise SeedrAuthError(f"Authentication failed: {result['error']}")
            self._token = result.get("token")
            if not self._token: raise SeedrAuthError("No token in response")
            return self._token
        except Exception as e:
            log.error(f"Seedr authentication error: {e}")
            raise SeedrAuthError(f"Authentication failed: {e}")

    async def _get_auth_headers(self) -> dict:
        if not self._token:
            await self.authenticate()
        return {"Authorization": f"Bearer {self._token}"}

    async def list_folder(self, folder_id: int = 0) -> dict:
        url = f"{self.API_URL}/folder"
        data = {"folder_id": folder_id}
        headers = await self._get_auth_headers()
        resp = await http_client.post(url, data=data, headers=headers)
        return resp.json()

    async def get_file_link(self, file_id: int) -> dict:
        url = f"{self.API_URL}/file"
        data = {"file_id": file_id}
        headers = await self._get_auth_headers()
        resp = await http_client.post(url, data=data, headers=headers)
        result = resp.json()
        if result.get("error"): raise SeedrError(f"Failed to get file link: {result['error']}")
        return result

    async def download_file(self, file_id: int, destination: Path, chunk_size: int = 5 * 1024 * 1024) -> Tuple[Path, int]:
        file_info = await self.get_file_link(file_id)
        if "url" not in file_info: raise SeedrError(f"No download URL in response: {file_info}")
        download_url = file_info["url"]
        file_size = file_info.get("size", 0)
        destination.parent.mkdir(parents=True, exist_ok=True)
        
        # Using httpx streaming
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", download_url) as response:
                response.raise_for_status()
                total_bytes = 0
                with open(destination, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=chunk_size):
                        f.write(chunk)
                        total_bytes += len(chunk)
                return destination, total_bytes

    async def delete_file(self, file_id: int) -> bool:
        url = f"{self.API_URL}/file/delete"
        data = {"file_id": file_id}
        headers = await self._get_auth_headers()
        resp = await http_client.post(url, data=data, headers=headers)
        result = resp.json()
        return not result.get("error")

    async def create_torrent(self, torrent_url: str) -> dict:
        url = f"{self.API_URL}/torrent"
        data = {"torrent": torrent_url}
        headers = await self._get_auth_headers()
        resp = await http_client.post(url, data=data, headers=headers)
        result = resp.json()
        if result.get("error"): raise SeedrError(f"Torrent creation failed: {result['error']}")
        return result
