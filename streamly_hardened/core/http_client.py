"""
Optimized HTTP Client for Seedr downloads targeting 10+ MB/s.

Strategy: Use 3 connections each downloading different file regions.
This achieves 11+ MB/s while avoiding rate limits.

Key optimizations:
1. HTTP/1.1 for better CDN compatibility
2. 3 parallel connections each handling different regions
3. 20MB chunks for balance of speed and reliability
4. Connection pooling and keepalive
"""

import asyncio
import time
from typing import Optional, Tuple, Callable
from contextlib import asynccontextmanager

import httpx


# Browser-like headers optimized for CDN
SEEDR_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Keep-Alive": "timeout=300, max=10",
    "Sec-Fetch-Dest": "video",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "no-cors",
}


class SeedrDownloader:
    """
    High-speed Seedr downloader optimized for 10+ MB/s.
    
    Uses 3-connection region-based download:
    - Connection 1: bytes 0-33%
    - Connection 2: bytes 33-66%
    - Connection 3: bytes 66-100%
    
    This achieves ~11 MB/s while avoiding rate limits.
    """
    
    def __init__(
        self,
        num_connections: int = 3,
        chunk_size: int = 20 * 1024 * 1024,  # 20MB chunks
        timeout: float = 300.0,
        progress_callback: Optional[Callable[[float, float, int], None]] = None,
        # Backward compatibility aliases
        max_connections: Optional[int] = None,
    ):
        # Support old 'max_connections' parameter
        if max_connections is not None:
            num_connections = max_connections
        
        self.num_connections = num_connections
        self.chunk_size = chunk_size
        self.timeout = timeout
        self.progress_callback = progress_callback
        
        self._client: Optional[httpx.AsyncClient] = None
        self._bytes_downloaded = 0
        self._start_time = 0
        self._lock = asyncio.Lock()
        self._total_bytes = 0
    
    async def __aenter__(self):
        limits = httpx.Limits(
            max_connections=self.num_connections + 2,  # Extra for head requests
            max_keepalive_connections=self.num_connections + 2,
            keepalive_expiry=300.0,
        )
        
        self._client = httpx.AsyncClient(
            limits=limits,
            timeout=httpx.Timeout(self.timeout, connect=30.0),
            http2=False,  # HTTP/1.1 for better CDN compatibility
            headers=SEEDR_HEADERS,
            follow_redirects=True,
            trust_env=True,
        )
        
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()
            self._client = None
        return False
    
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Client not initialized. Use 'async with' context.")
        return self._client
    
    async def get_file_info(self, url: str) -> dict:
        """Get file metadata."""
        response = await self.client.head(url)
        response.raise_for_status()
        
        return {
            'size': int(response.headers.get('content-length', 0)),
            'type': response.headers.get('content-type', 'unknown'),
            'ranges': 'bytes' in response.headers.get('accept-ranges', ''),
        }
    
    async def _download_region(
        self,
        url: str,
        start: int,
        end: int,
        region_id: int,
    ) -> list[Tuple[int, bytes]]:
        """Download a region of the file in chunks."""
        results = []
        region_bytes = 0
        
        print(f"  Region {region_id}: Starting (bytes {start:,} - {end:,})")
        
        for chunk_start in range(start, end + 1, self.chunk_size):
            chunk_end = min(chunk_start + self.chunk_size - 1, end)
            retries = 3
            
            while retries > 0:
                try:
                    response = await self.client.get(
                        url,
                        headers={"Range": f"bytes={chunk_start}-{chunk_end}"}
                    )
                    
                    # Handle rate limiting
                    if response.status_code == 429:
                        retry_after = int(response.headers.get("Retry-After", 30))
                        print(f"\n  Region {region_id}: Rate limited, waiting {retry_after}s...")
                        await asyncio.sleep(retry_after)
                        continue
                    
                    response.raise_for_status()
                    data = response.content
                    region_bytes += len(data)
                    results.append((chunk_start, data))
                    
                    # Update progress
                    async with self._lock:
                        self._bytes_downloaded += len(data)
                        elapsed = time.time() - self._start_time
                        speed = (self._bytes_downloaded / 1024 / 1024) / elapsed if elapsed > 0 else 0
                        pct = (self._bytes_downloaded / self._total_bytes) * 100 if self._total_bytes > 0 else 0
                        
                        if self.progress_callback:
                            self.progress_callback(pct, speed, self._bytes_downloaded)
                    
                    break  # Success, exit retry loop
                    
                except Exception as e:
                    retries -= 1
                    if retries == 0:
                        print(f"\n  Region {region_id}: Failed after retries - {e}")
                        raise
                    print(f"\n  Region {region_id}: Retry {3-retries}/3...")
                    await asyncio.sleep(2)
        
        print(f"  Region {region_id}: Complete ({region_bytes:,} bytes)")
        return results
    
    async def download(
        self,
        url: str,
        total_size: int,
        destination: str = None,
    ) -> Tuple[bytes, dict]:
        """
        Download file using multi-region parallel strategy.
        
        Args:
            url: File URL
            total_size: Total file size in bytes
            destination: Optional file path to write directly
        
        Returns:
            Tuple of (file_data, stats_dict)
        """
        self._bytes_downloaded = 0
        self._start_time = time.time()
        self._total_bytes = total_size
        
        # Calculate regions for each connection
        region_size = total_size // self.num_connections
        regions = []
        
        for i in range(self.num_connections):
            start = i * region_size
            if i == self.num_connections - 1:
                end = total_size - 1  # Last region gets the remainder
            else:
                end = (i + 1) * region_size - 1
            regions.append((start, end, i))
        
        print(f"\nDownload config:")
        print(f"  Total size: {total_size:,} bytes ({total_size/1024/1024:.1f} MB)")
        print(f"  Regions: {len(regions)}")
        print(f"  Chunk size: {self.chunk_size//1024//1024} MB")
        for start, end, i in regions:
            print(f"    Region {i}: bytes {start:,} - {end:,} ({((end-start+1)//1024//1024):.1f} MB)")
        
        # Download all regions in parallel
        print(f"\nStarting download with {self.num_connections} connections...")
        start_download = time.time()
        
        tasks = [
            self._download_region(url, start, end, region_id)
            for start, end, region_id in regions
        ]
        
        region_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        download_time = time.time() - start_download
        
        # Check for errors
        all_results = []
        for i, result in enumerate(region_results):
            if isinstance(result, Exception):
                raise result
            all_results.extend(result)
        
        # Sort by start position
        all_results.sort(key=lambda x: x[0])
        
        # Concatenate
        data = b"".join(chunk_data for _, chunk_data in all_results)
        
        # Calculate final stats
        total_bytes = len(data)
        avg_speed = (total_bytes / 1024 / 1024) / download_time if download_time > 0 else 0
        
        stats = {
            'bytes': total_bytes,
            'download_time': download_time,
            'speed_mbps': avg_speed,
            'regions': len(regions),
            'chunks': len(all_results),
        }
        
        print(f"\nDownload complete:")
        print(f"  Bytes: {total_bytes:,}")
        print(f"  Time: {download_time:.2f}s")
        print(f"  Average speed: {avg_speed:.2f} MB/s")
        
        # Write to file if destination provided
        if destination:
            with open(destination, 'wb') as f:
                f.write(data)
            print(f"  Written to: {destination}")
        
        return data, stats
    
    def get_current_stats(self) -> dict:
        elapsed = time.time() - self._start_time
        speed = (self._bytes_downloaded / 1024 / 1024) / elapsed if elapsed > 0 else 0
        return {
            'bytes': self._bytes_downloaded,
            'total': self._total_bytes,
            'elapsed': elapsed,
            'speed_mbps': speed,
            'percent': (self._bytes_downloaded / self._total_bytes * 100) if self._total_bytes > 0 else 0,
        }


@asynccontextmanager
async def managed_seedr_downloader(
    num_connections: int = 3,
    chunk_size: int = 20 * 1024 * 1024,
    **kwargs
):
    """Context manager for Seedr downloader."""
    downloader = SeedrDownloader(
        num_connections=num_connections,
        chunk_size=chunk_size,
        **kwargs
    )
    async with downloader:
        yield downloader


# Alias for backwards compatibility
RateLimitedHTTPClient = SeedrDownloader
managed_http_client = managed_seedr_downloader