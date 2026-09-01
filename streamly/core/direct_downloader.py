"""
1DM-Style High-Speed Multi-Threaded Range Downloader Engine
Supports dynamic 16-32 chunk range splitting, direct sparse-file disk allocation,
and automatic fallback for servers that do not support range headers.
"""

from __future__ import annotations

import os
import time
import math
import asyncio
import logging
import urllib.parse
import httpx
from typing import Optional, Callable

log = logging.getLogger(__name__)

# Default user agent emulating standard browser to prevent hotlink blocks
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive"
}

class Direct1DMDownloader:
    def __init__(
        self,
        target_dir: str,
        num_connections: int = 16,
        chunk_size: int = 1024 * 1024 * 2, # 2MB buffer
        timeout: float = 30.0
    ):
        self.target_dir = target_dir
        self.num_connections = max(1, min(num_connections, 32))
        self.chunk_size = chunk_size
        self.timeout = timeout
        self._cancel_flag = [False]
        os.makedirs(self.target_dir, exist_ok=True)

    def cancel(self):
        self._cancel_flag[0] = True

    async def probe(self, url: str) -> dict:
        """Probes URL to detect file size, range support, and filename."""
        parsed = urllib.parse.urlparse(url)
        raw_name = os.path.basename(parsed.path) or "download"
        raw_name = urllib.parse.unquote(raw_name)

        probe_info = {
            "url": url,
            "filename": raw_name,
            "content_length": 0,
            "supports_ranges": False,
            "redirected_url": url
        }

        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=15.0, follow_redirects=True) as client:
            # 1. Try HEAD first
            try:
                head_resp = await client.head(url)
                if head_resp.status_code in (200, 206):
                    probe_info["redirected_url"] = str(head_resp.url)
                    cl = head_resp.headers.get("content-length")
                    if cl and cl.isdigit():
                        probe_info["content_length"] = int(cl)
                    ar = head_resp.headers.get("accept-ranges", "").lower()
                    if ar == "bytes" or head_resp.status_code == 206:
                        probe_info["supports_ranges"] = True
                    cd = head_resp.headers.get("content-disposition", "")
                    if "filename=" in cd:
                        fname = cd.split("filename=")[-1].strip('"\'; ')
                        if fname:
                            probe_info["filename"] = urllib.parse.unquote(fname)
            except Exception as e:
                log.debug("HEAD probe failed for %s: %s", url, e)

            # 2. If HEAD didn't give Content-Length, test small Range GET (bytes=0-1)
            if not probe_info["content_length"]:
                try:
                    range_headers = {**DEFAULT_HEADERS, "Range": "bytes=0-1"}
                    async with client.stream("GET", url, headers=range_headers) as r_resp:
                        if r_resp.status_code == 206:
                            probe_info["supports_ranges"] = True
                            cr = r_resp.headers.get("content-range")
                            if cr and "/" in cr:
                                total_str = cr.split("/")[-1].strip()
                                if total_str.isdigit():
                                    probe_info["content_length"] = int(total_str)
                        elif r_resp.status_code == 200:
                            cl = r_resp.headers.get("content-length")
                            if cl and cl.isdigit():
                                probe_info["content_length"] = int(cl)
                        cd = r_resp.headers.get("content-disposition", "")
                        if "filename=" in cd:
                            fname = cd.split("filename=")[-1].strip('"\'; ')
                            if fname:
                                probe_info["filename"] = urllib.parse.unquote(fname)
                except Exception as e:
                    log.debug("Range probe failed for %s: %s", url, e)

        # Sanitize filename
        safe_name = "".join(c for c in probe_info["filename"] if c.isalnum() or c in "._- ()[]{}")
        probe_info["filename"] = safe_name or "downloaded_file"
        return probe_info

    async def download(
        self,
        url: str,
        filename: Optional[str] = None,
        progress_callback: Optional[Callable[[int, int, float], None]] = None
    ) -> str:
        """
        Executes 1DM-style multi-connection high-speed download with dynamic scaling,
        persistent file descriptor writing, and automatic failure cleanup.
        """
        probe_info = await self.probe(url)
        final_filename = filename or probe_info["filename"]
        target_path = os.path.join(self.target_dir, final_filename)
        total_size = probe_info["content_length"]
        supports_ranges = probe_info["supports_ranges"] and total_size > 2 * 1024 * 1024

        # Pre-check NVMe physical disk space
        if total_size > 0:
            import shutil
            free_disk = shutil.disk_usage(self.target_dir).free
            if free_disk < (total_size + 50 * 1024 * 1024):
                raise ValueError(
                    f"Insufficient disk space: {free_disk / (1024**3):.2f} GB free, {total_size / (1024**3):.2f} GB required."
                )

        # Dynamically scale parallel connections (e.g. 1 conn per 2MB minimum)
        active_connections = min(self.num_connections, max(1, total_size // (2 * 1024 * 1024))) if (supports_ranges and total_size > 0) else 1

        log.info(
            "1DM Engine: Downloading %s (Size: %s, Range Support: %s, Parallel Streams: %d)",
            final_filename,
            f"{total_size / (1024*1024):.2f} MB" if total_size else "Unknown",
            supports_ranges,
            active_connections
        )

        start_time = time.time()
        downloaded_bytes = 0
        last_progress_time = start_time
        last_downloaded_bytes = 0
        file_lock = asyncio.Lock()
        download_succeeded = False

        # Allocate file size on disk for sparse writing
        if total_size > 0:
            with open(target_path, "wb") as f:
                f.seek(total_size - 1)
                f.write(b"\0")

        out_file = None
        try:
            if supports_ranges and total_size > 0:
                out_file = open(target_path, "r+b", buffering=0)
                part_size = math.ceil(total_size / active_connections)
                tasks = []

                async def _download_slice(slice_idx: int, start_byte: int, end_byte: int):
                    nonlocal downloaded_bytes, last_progress_time, last_downloaded_bytes
                    slice_headers = {**DEFAULT_HEADERS, "Range": f"bytes={start_byte}-{end_byte}"}
                    
                    async with httpx.AsyncClient(headers=slice_headers, timeout=self.timeout, follow_redirects=True) as client:
                        async with client.stream("GET", probe_info["redirected_url"]) as resp:
                            resp.raise_for_status()
                            current_offset = start_byte
                            async for chunk in resp.aiter_bytes(chunk_size=self.chunk_size):
                                if self._cancel_flag[0]:
                                    raise asyncio.CancelledError("Download cancelled by user")
                                
                                chunk_len = len(chunk)
                                async with file_lock:
                                    out_file.seek(current_offset)
                                    out_file.write(chunk)
                                
                                current_offset += chunk_len
                                downloaded_bytes += chunk_len

                                now = time.time()
                                if now - last_progress_time >= 0.5:
                                    elapsed = now - last_progress_time
                                    bytes_diff = downloaded_bytes - last_downloaded_bytes
                                    speed_mbps = (bytes_diff / (1024 * 1024) / elapsed) * 8.0 if elapsed > 0 else 0.0
                                    last_progress_time = now
                                    last_downloaded_bytes = downloaded_bytes
                                    if progress_callback:
                                        progress_callback(downloaded_bytes, total_size, speed_mbps)

                for i in range(active_connections):
                    s_byte = i * part_size
                    e_byte = min(total_size - 1, (i + 1) * part_size - 1)
                    if s_byte <= e_byte:
                        tasks.append(_download_slice(i, s_byte, e_byte))

                await asyncio.gather(*tasks)

            else:
                async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=self.timeout, follow_redirects=True) as client:
                    async with client.stream("GET", probe_info["redirected_url"]) as resp:
                        resp.raise_for_status()
                        with open(target_path, "wb") as f:
                            async for chunk in resp.aiter_bytes(chunk_size=self.chunk_size):
                                if self._cancel_flag[0]:
                                    raise asyncio.CancelledError("Download cancelled by user")
                                f.write(chunk)
                                downloaded_bytes += len(chunk)

                                now = time.time()
                                if now - last_progress_time >= 0.5:
                                    elapsed = now - last_progress_time
                                    bytes_diff = downloaded_bytes - last_downloaded_bytes
                                    speed_mbps = (bytes_diff / (1024 * 1024) / elapsed) * 8.0 if elapsed > 0 else 0.0
                                    last_progress_time = now
                                    last_downloaded_bytes = downloaded_bytes
                                    if progress_callback:
                                        progress_callback(downloaded_bytes, total_size or downloaded_bytes, speed_mbps)

            download_succeeded = True
            elapsed_total = time.time() - start_time
            avg_speed_mbps = (downloaded_bytes / (1024 * 1024) / elapsed_total) * 8.0 if elapsed_total > 0 else 0.0
            log.info(
                "1DM Download Complete: %s (%.2f MB in %.1fs, %.2f Mbps avg)",
                final_filename,
                downloaded_bytes / (1024 * 1024),
                elapsed_total,
                avg_speed_mbps
            )

            if progress_callback:
                progress_callback(downloaded_bytes, total_size or downloaded_bytes, avg_speed_mbps)

            return target_path

        finally:
            if out_file:
                try:
                    out_file.close()
                except Exception:
                    pass
            # Cleanup incomplete file if download failed or cancelled
            if not download_succeeded and os.path.exists(target_path):
                try:
                    os.remove(target_path)
                    log.info("Cleaned up incomplete 1DM download: %s", target_path)
                except Exception:
                    pass
