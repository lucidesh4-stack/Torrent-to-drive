import ssl
import httpx
import asyncio
import time
import os
import urllib.parse
from pathlib import Path
from typing import Optional, Callable, Dict, Any

def create_ssl_context() -> ssl.SSLContext:
    """Create a custom SSL context with SECLEVEL=1 to prevent UNEXPECTED_EOF_WHILE_READING errors."""
    ctx = ssl.create_default_context()
    ctx.set_ciphers('DEFAULT@SECLEVEL=1')
    return ctx

class HttpClientManager:
    """Singleton to manage the shared httpx.AsyncClient instance."""
    _instance: Optional['HttpClientManager'] = None

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._ssl_context = create_ssl_context()
        self._lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> 'HttpClientManager':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            async with self._lock:
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(
                        verify=self._ssl_context,
                        timeout=30.0,
                        http2=True
                    )
        return self._client

    async def close(self):
        if self._client is not None and not self._client.is_closed:
            async with self._lock:
                if self._client is not None and not self._client.is_closed:
                    await self._client.aclose()
                    self._client = None


class RateLimitedHTTPClient:
    """Wrapper around httpx.AsyncClient to support retry policies and keep-alive limits."""
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def post(self, url: str, data: Any = None, json: Any = None, headers: Any = None, retry_count: int = 3, **kwargs):
        for attempt in range(1, retry_count + 1):
            try:
                resp = await self.client.post(url, data=data, json=json, headers=headers, **kwargs)
                resp.raise_for_status()
                return resp
            except (httpx.HTTPError, httpx.NetworkError) as e:
                if attempt == retry_count:
                    raise
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))

    async def get(self, url: str, params: Any = None, headers: Any = None, retry_count: int = 3, **kwargs):
        for attempt in range(1, retry_count + 1):
            try:
                resp = await self.client.get(url, params=params, headers=headers, **kwargs)
                resp.raise_for_status()
                return resp
            except (httpx.HTTPError, httpx.NetworkError) as e:
                if attempt == retry_count:
                    raise
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))


from contextlib import asynccontextmanager

@asynccontextmanager
async def managed_http_client(max_connections: int = 10, timeout: float = 30.0):
    """Context manager yielding a RateLimitedHTTPClient with isolated pool settings."""
    ssl_context = create_ssl_context()
    limits = httpx.Limits(max_keepalive_connections=max_connections, max_connections=max_connections)
    async with httpx.AsyncClient(verify=ssl_context, limits=limits, timeout=timeout, http2=True) as client:
        yield RateLimitedHTTPClient(client)


class OptimizedDownloader:
    """High-speed downloader using Cloudflare Worker proxy."""
    def __init__(
        self,
        worker_url: str = "https://streamly-proxy.lucidesh.workers.dev/",
        temp_dir: str = None,
        timeout: float = 600.0,
        **kwargs
    ):
        self.worker_url = worker_url.rstrip("/") + "/"
        self.temp_dir = temp_dir or os.environ.get('TEMP_DIR', '/tmp/streamly_downloads')
        self.timeout = timeout
        self._worker_blocked = False
        self._stats = {
            'total_bytes': 0,
            'total_time': 0,
            'downloads': 0,
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    def _ensure_temp_dir(self):
        Path(self.temp_dir).mkdir(parents=True, exist_ok=True)

    async def _download_via_worker(
        self,
        url: str,
        dest_path: Path,
        progress_callback: Optional[Callable] = None,
    ) -> Optional[Dict]:
        encoded_url = urllib.parse.quote(url, safe='')
        worker_endpoint = f"{self.worker_url}?url={encoded_url}"
        
        start_time = time.time()
        bytes_downloaded = 0
        ssl_ctx = create_ssl_context()
        
        async with httpx.AsyncClient(verify=ssl_ctx, timeout=self.timeout) as client:
            async with client.stream("GET", worker_endpoint) as response:
                if response.status_code == 403:
                    self._worker_blocked = True
                    return None
                response.raise_for_status()
                
                with open(dest_path, 'wb') as f:
                    async for chunk in response.aiter_bytes(chunk_size=512*1024):
                        f.write(chunk)
                        bytes_downloaded += len(chunk)
                        
                        if progress_callback:
                            elapsed = time.time() - start_time
                            speed = (bytes_downloaded / 1024 / 1024 / elapsed) * 8 if elapsed > 0 else 0
                            progress_callback(bytes_downloaded, speed)
        
        elapsed = time.time() - start_time
        speed_mbps = (bytes_downloaded / 1024 / 1024 / elapsed) * 8 if elapsed > 0 else 0
        
        return {
            'path': str(dest_path),
            'size': bytes_downloaded,
            'speed_mbps': speed_mbps,
            'time_s': elapsed,
            'method': 'Worker',
        }

    async def _download_via_direct(
        self,
        url: str,
        dest_path: Path,
        progress_callback: Optional[Callable] = None,
    ) -> Dict:
        start_time = time.time()
        bytes_downloaded = 0
        ssl_ctx = create_ssl_context()
        
        async with httpx.AsyncClient(verify=ssl_ctx, timeout=self.timeout, http2=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                with open(dest_path, 'wb') as f:
                    async for chunk in response.aiter_bytes(chunk_size=512*1024):
                        f.write(chunk)
                        bytes_downloaded += len(chunk)
                        
                        if progress_callback:
                            elapsed = time.time() - start_time
                            speed = (bytes_downloaded / 1024 / 1024 / elapsed) * 8 if elapsed > 0 else 0
                            progress_callback(bytes_downloaded, speed)
        
        elapsed = time.time() - start_time
        speed_mbps = (bytes_downloaded / 1024 / 1024 / elapsed) * 8 if elapsed > 0 else 0
        
        return {
            'path': str(dest_path),
            'size': bytes_downloaded,
            'speed_mbps': speed_mbps,
            'time_s': elapsed,
            'method': 'Direct',
        }

    async def download(
        self,
        url: str,
        filename: str = None,
        progress_callback: Optional[Callable] = None,
    ) -> Dict:
        self._ensure_temp_dir()
        if not filename:
            filename = url.split('/')[-1].split('?')[0] or 'download.bin'
        
        dest_path = Path(self.temp_dir) / filename
        
        if not self._worker_blocked:
            try:
                result = await self._download_via_worker(url, dest_path, progress_callback)
                if result:
                    self._update_stats(result)
                    return result
            except Exception:
                pass
        
        result = await self._download_via_direct(url, dest_path, progress_callback)
        self._update_stats(result)
        return result

    def _update_stats(self, result: Dict):
        self._stats['total_bytes'] += result['size']
        self._stats['total_time'] += result['time_s']
        self._stats['downloads'] += 1

    def get_stats(self) -> Dict:
        total_time = self._stats['total_time']
        if self._stats['downloads'] > 0 and total_time > 0:
            avg_speed = (self._stats['total_bytes'] / 1024 / 1024 / total_time) * 8
        else:
            avg_speed = 0
        
        return {
            'total_bytes': self._stats['total_bytes'],
            'total_time_s': total_time,
            'downloads': self._stats['downloads'],
            'avg_speed_mbps': avg_speed,
            'worker_blocked': self._worker_blocked,
        }

    @property
    def temp_directory(self) -> str:
        return self.temp_dir

# Backwards compatibility alias
SeedrDownloader = OptimizedDownloader