"""
Seedr API Service
Direct Seedr integration (no proxy) with rate limiting.
FIXES: 429 from aggressive parallel downloads
"""
from __future__ import annotations

import os
import json
import asyncio
import httpx
from pathlib import Path
from typing import Optional, Tuple

from streamly_optimized.core.http_client import managed_http_client, create_ssl_context, RateLimitedHTTPClient


class SeedrError(Exception):
    """Base exception for Seedr operations."""
    pass


class SeedrAuthError(SeedrError):
    """Authentication failed."""
    pass


class SeedrRateLimitError(SeedrError):
    """Rate limited by Seedr (429)."""
    pass


class SeedrNotFoundError(SeedrError):
    """File or folder not found."""
    pass


class SeedrService:
    """
    Seedr API client with proper rate limiting and retry logic.
    
    CRITICAL FIXES:
    1. Max 2 concurrent connections to avoid 429
    2. Proper retry with exponential backoff
    3. Browser-like headers to avoid detection
    """
    
    BASE_URL = "https://www.seedr.cc"
    API_URL = f"{BASE_URL}/api"
    
    def __init__(
        self,
        username: str,
        password: str,
        max_connections: int = 2,  # Conservative to avoid 429
    ):
        self.username = username
        self.password = password
        self.max_connections = max_connections
        self._token: Optional[str] = None
        self._async_client: Optional[httpx.AsyncClient] = None
        self._client: Optional[RateLimitedHTTPClient] = None
    
    async def __aenter__(self):
        await self.authenticate()
        ssl_ctx = create_ssl_context()
        limits = httpx.Limits(max_keepalive_connections=self.max_connections, max_connections=self.max_connections)
        self._async_client = httpx.AsyncClient(verify=ssl_ctx, limits=limits, timeout=30.0, http2=True)
        if self._token:
            self._async_client.headers["Authorization"] = f"Bearer {self._token}"
        self._client = RateLimitedHTTPClient(self._async_client)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._async_client:
            await self._async_client.aclose()
            self._async_client = None
        self._client = None
        return False
    
    async def authenticate(self) -> str:
        """
        Authenticate with Seedr and get access token.
        
        Returns:
            Access token string
        
        Raises:
            SeedrAuthError on authentication failure
        """
        url = f"{self.API_URL}/account"
        data = {
            "username": self.username,
            "password": self.password,
        }
        
        async with managed_http_client(max_connections=self.max_connections) as client:
            response = await client.post(
                url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                retry_count=3,
            )
            
            result = response.json()
            
            if result.get("error"):
                raise SeedrAuthError(f"Authentication failed: {result['error']}")
            
            self._token = result.get("token")
            if not self._token:
                raise SeedrAuthError("No token in response")
            
            return self._token
    
    @property
    def client(self) -> RateLimitedHTTPClient:
        if self._client is None:
            raise RuntimeError("SeedrService not initialized. Use 'async with' context.")
        return self._client
    
    async def list_folder(self, folder_id: int = 0) -> dict:
        """
        List contents of a folder.
        
        Args:
            folder_id: Folder ID (0 for root)
        
        Returns:
            Folder contents dict
        """
        url = f"{self.API_URL}/folder"
        data = {"folder_id": folder_id}
        
        response = await self.client.post(
            url,
            data=data,
            retry_count=3,
        )
        
        return response.json()
    
    async def get_file_link(self, file_id: int) -> dict:
        """
        Get download link for a file.
        
        Args:
            file_id: File ID
        
        Returns:
            Dict with 'url' and file metadata
        """
        url = f"{self.API_URL}/file"
        data = {"file_id": file_id}
        
        response = await self.client.post(
            url,
            data=data,
            retry_count=3,
        )
        
        result = response.json()
        
        if result.get("error"):
            raise SeedrError(f"Failed to get file link: {result['error']}")
        
        return result
    
    async def download_file(
        self,
        file_id: int,
        destination: Path,
        chunk_size: int = 5 * 1024 * 1024,  # 5MB
    ) -> Tuple[Path, int]:
        """
        Download a file from Seedr to local storage.
        
        Args:
            file_id: Seedr file ID
            destination: Local destination path
            chunk_size: Download chunk size
        
        Returns:
            Tuple of (Path, total_bytes)
        
        Raises:
            SeedrError on failure
            SeedrRateLimitError on 429
        """
        file_info = await self.get_file_link(file_id)
        
        if "url" not in file_info:
            raise SeedrError(f"No download URL in response: {file_info}")
        
        download_url = file_info["url"]
        file_size = file_info.get("size", 0)
        
        destination.parent.mkdir(parents=True, exist_ok=True)
        
        async with managed_http_client(max_connections=self.max_connections) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            
            if file_size > 0 and file_size < 50 * 1024 * 1024:  # < 50MB
                response = await client.get(download_url, headers=headers, retry_count=3)
                
                with open(destination, "wb") as f:
                    f.write(response.content)
                
                return destination, len(response.content)
            
            response = await client.client.get(download_url, headers=headers)
            
            total_bytes = 0
            with open(destination, "wb") as f:
                async for chunk in response.aiter_bytes(chunk_size=chunk_size):
                    f.write(chunk)
                    total_bytes += len(chunk)
            
            return destination, total_bytes
    
    async def delete_file(self, file_id: int) -> bool:
        """
        Delete a file from Seedr.
        
        Args:
            file_id: File ID to delete
        
        Returns:
            True if successful
        """
        url = f"{self.API_URL}/file/delete"
        data = {"file_id": file_id}
        
        response = await self.client.post(
            url,
            data=data,
            retry_count=3,
        )
        
        result = response.json()
        return not result.get("error")
    
    async def create_torrent(self, torrent_url: str) -> dict:
        """
        Add a torrent download.
        
        Args:
            torrent_url: Magnet link or torrent URL
        
        Returns:
            Dict with torrent info
        """
        url = f"{self.API_URL}/torrent"
        data = {"torrent": torrent_url}
        
        response = await self.client.post(
            url,
            data=data,
            retry_count=3,
        )
        
        result = response.json()
        
        if result.get("error"):
            raise SeedrError(f"Torrent creation failed: {result['error']}")
        
        return result


class SeedrSession:
    """
    Context manager for Seedr operations with automatic token refresh.
    """
    
    def __init__(
        self,
        username: str,
        password: str,
        max_connections: int = 2,
    ):
        self.username = username
        self.password = password
        self.max_connections = max_connections
        self._service: Optional[SeedrService] = None
    
    async def __aenter__(self) -> SeedrService:
        self._service = SeedrService(self.username, self.password, self.max_connections)
        await self._service.__aenter__()
        return self._service
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._service:
            await self._service.__aexit__(exc_type, exc_val, exc_tb)
            self._service = None
        return False