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
        timeout: float = 30.0,
        cancel_flag: Optional[list] = None,
        pause_event: Optional[asyncio.Event] = None,
    ):
        self.target_dir = target_dir
        self.num_connections = max(1, min(num_connections, 32))
        self.chunk_size = chunk_size
        self.timeout = timeout
        self._cancel_flag = cancel_flag if cancel_flag is not None else [False]
        self._pause_event = pause_event
        os.makedirs(self.target_dir, exist_ok=True)

    @staticmethod
    def _parse_content_disposition(cd: str) -> Optional[str]:
        if not cd:
            return None
        # RFC 5987 / RFC 6266 filename*=UTF-8''...
        if "filename*=" in cd:
            part = cd.split("filename*=")[-1].split(";")[0].strip()
            if "''" in part:
                encoding, val = part.split("''", 1)
                try:
                    return urllib.parse.unquote(val, encoding=encoding or "utf-8")
                except Exception:
                    return urllib.parse.unquote(val)
            return urllib.parse.unquote(part.strip('"\'; '))
        if "filename=" in cd:
            part = cd.split("filename=")[-1].split(";")[0].strip('"\'; ')
            if part:
                return urllib.parse.unquote(part)
        return None

    def cancel(self):
        self._cancel_flag[0] = True

    async def probe(self, url: str) -> dict:
        """Probes URL to detect file size, true range support (206), and filename."""
        parsed = urllib.parse.urlparse(url)
        raw_name = os.path.basename(parsed.path) or "download"
        raw_name = urllib.parse.unquote(raw_name)

        # 1. Check query parameters for explicit filename overrides (?filename=..., &file=...)
        query_params = urllib.parse.parse_qs(parsed.query)
        for q_key in ("filename", "file_name", "fileName", "file", "name", "response-content-disposition"):
            if q_key in query_params and query_params[q_key]:
                q_val = query_params[q_key][0].strip()
                if "filename=" in q_val:
                    parsed_cd = self._parse_content_disposition(q_val)
                    if parsed_cd:
                        raw_name = parsed_cd
                        break
                elif q_val and not q_val.isdigit():
                    raw_name = urllib.parse.unquote(q_val)
                    break

        probe_info = {
            "url": url,
            "filename": raw_name,
            "content_length": 0,
            "supports_ranges": False,
            "redirected_url": url
        }

        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=15.0, follow_redirects=True) as client:
            # 2. Try lightweight HEAD first to detect redirected_url and Content-Disposition
            try:
                head_resp = await client.head(url)
                if head_resp.status_code in (200, 206):
                    probe_info["redirected_url"] = str(head_resp.url)
                    cl = head_resp.headers.get("content-length")
                    if cl and cl.isdigit():
                        probe_info["content_length"] = int(cl)
                    cd = head_resp.headers.get("content-disposition", "")
                    cd_name = self._parse_content_disposition(cd)
                    if cd_name:
                        probe_info["filename"] = cd_name
            except Exception as e:
                log.debug("HEAD probe failed for %s: %s", url, e)

            # 3. STRICT RANGE VERIFICATION TEST (Range: bytes=0-1):
            # Never trust Accept-Ranges header alone! CDN reverse proxies often return
            # 'Accept-Ranges: bytes' on HEAD, but origins (like dynamic on-the-fly zip generators)
            # return 200 OK and stream from byte 0 when an actual Range GET is sent.
            try:
                range_headers = {**DEFAULT_HEADERS, "Range": "bytes=0-1"}
                async with client.stream("GET", probe_info["redirected_url"], headers=range_headers) as r_resp:
                    probe_info["redirected_url"] = str(r_resp.url)
                    cd = r_resp.headers.get("content-disposition", "")
                    cd_name = self._parse_content_disposition(cd)
                    if cd_name:
                        probe_info["filename"] = cd_name

                    if r_resp.status_code == 206:
                        # Server strictly honored byte ranges!
                        cr = r_resp.headers.get("content-range", "")
                        if cr and "/" in cr:
                            total_str = cr.split("/")[-1].strip()
                            if total_str.isdigit():
                                probe_info["content_length"] = int(total_str)
                                probe_info["supports_ranges"] = True
                    elif r_resp.status_code == 200:
                        # Server completely ignored Range header and returned full stream!
                        # Multi-connection download MUST NOT be used for this URL.
                        probe_info["supports_ranges"] = False
                        cl = r_resp.headers.get("content-length")
                        if cl and cl.isdigit():
                            probe_info["content_length"] = int(cl)
            except Exception as e:
                log.debug("Range verification probe failed for %s: %s", url, e)

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
        strict byte clamping, and automatic sequential single-stream fallback.
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
            "1DM Engine: Downloading %s (Size: %s, Verified Range Support: %s, Parallel Streams: %d)",
            final_filename,
            f"{total_size / (1024*1024):.2f} MB" if total_size else "Unknown (Dynamic Stream)",
            supports_ranges,
            active_connections
        )

        start_time = time.time()
        downloaded_bytes = 0
        last_progress_time = start_time
        last_downloaded_bytes = 0
        file_lock = asyncio.Lock()
        download_succeeded = False

        use_multi_connection = supports_ranges and total_size > 0
        fallback_to_single = False

        try:
            if use_multi_connection:
                # Pre-allocate sparse file on disk for chunked direct writing
                with open(target_path, "wb") as f:
                    f.seek(total_size - 1)
                    f.write(b"\0")

                out_file = None
                shared_client = None
                try:
                    out_file = open(target_path, "r+b", buffering=0)
                    part_size = math.ceil(total_size / active_connections)
                    tasks = []
                    pool_limits = httpx.Limits(
                        max_keepalive_connections=active_connections * 2,
                        max_connections=active_connections * 2
                    )
                    shared_client = httpx.AsyncClient(limits=pool_limits, timeout=self.timeout, follow_redirects=True)

                    async def _download_slice(slice_idx: int, start_byte: int, end_byte: int):
                        nonlocal downloaded_bytes, last_progress_time, last_downloaded_bytes
                        slice_headers = {**DEFAULT_HEADERS, "Range": f"bytes={start_byte}-{end_byte}"}

                        async with shared_client.stream("GET", probe_info["redirected_url"], headers=slice_headers) as resp:
                            if resp.status_code != 206:
                                raise ValueError(
                                    f"Origin returned HTTP {resp.status_code} instead of 206 Partial Content for slice {slice_idx}."
                                )
                            resp.raise_for_status()
                            current_offset = start_byte

                            async for chunk in resp.aiter_bytes(chunk_size=self.chunk_size):
                                if self._cancel_flag and self._cancel_flag[0]:
                                    raise asyncio.CancelledError("Download cancelled by user")
                                if self._pause_event and not self._pause_event.is_set():
                                    await self._pause_event.wait()
                                    if self._cancel_flag and self._cancel_flag[0]:
                                        raise asyncio.CancelledError("Download cancelled by user")

                                # STRICT BYTE CLAMP: never write or read beyond assigned end_byte!
                                remaining = end_byte - current_offset + 1
                                if remaining <= 0:
                                    break
                                if len(chunk) > remaining:
                                    chunk = chunk[:remaining]

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

                                if current_offset > end_byte:
                                    break

                    for i in range(active_connections):
                        s_byte = i * part_size
                        e_byte = min(total_size - 1, (i + 1) * part_size - 1)
                        if s_byte <= e_byte:
                            tasks.append(_download_slice(i, s_byte, e_byte))

                    await asyncio.gather(*tasks)
                    download_succeeded = True

                except Exception as range_err:
                    if self._cancel_flag and self._cancel_flag[0]:
                        raise
                    log.warning(
                        "Multi-connection range download aborted (%s). Gracefully switching to sequential single-stream fallback...",
                        range_err
                    )
                    fallback_to_single = True

                finally:
                    if shared_client:
                        try:
                            await shared_client.aclose()
                        except Exception:
                            pass
                    if out_file:
                        try:
                            out_file.close()
                        except Exception:
                            pass

            # Single-stream mode: used when range requests are unsupported, or on slice fallback
            if not use_multi_connection or fallback_to_single:
                downloaded_bytes = 0
                last_downloaded_bytes = 0
                last_progress_time = time.time()
                limits = httpx.Limits(max_keepalive_connections=10, max_connections=10)

                async with httpx.AsyncClient(headers=DEFAULT_HEADERS, limits=limits, timeout=self.timeout, follow_redirects=True) as client:
                    async with client.stream("GET", probe_info["redirected_url"]) as resp:
                        resp.raise_for_status()

                        # Check if GET stream reveals actual content length
                        resp_cl = resp.headers.get("content-length")
                        if resp_cl and resp_cl.isdigit() and int(resp_cl) > 0:
                            total_size = int(resp_cl)

                        with open(target_path, "wb") as f:
                            async for chunk in resp.aiter_bytes(chunk_size=self.chunk_size):
                                if self._cancel_flag and self._cancel_flag[0]:
                                    raise asyncio.CancelledError("Download cancelled by user")
                                if self._pause_event and not self._pause_event.is_set():
                                    await self._pause_event.wait()
                                    if self._cancel_flag and self._cancel_flag[0]:
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
                                        effective_total = max(total_size, downloaded_bytes) if total_size > 0 else downloaded_bytes
                                        progress_callback(downloaded_bytes, effective_total, speed_mbps)

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
                progress_callback(downloaded_bytes, downloaded_bytes, avg_speed_mbps)

            try:
                os.utime(target_path, (time.time(), time.time()))
            except Exception:
                pass

            return target_path

        finally:
            # Cleanup incomplete file if download failed or cancelled
            if not download_succeeded and os.path.exists(target_path):
                try:
                    os.remove(target_path)
                    log.info("Cleaned up incomplete 1DM download: %s", target_path)
                except Exception:
                    pass
