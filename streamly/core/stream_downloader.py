"""
HLS and DASH Stream Downloader
Supports .m3u8 (HLS) and .mpd (MPEG-DASH) stream manifests with:
- Multi-threaded concurrent fragment downloading (yt-dlp native)
- Lossless remuxing to clean .mp4 via ffmpeg
- Real-time progress, speed, and ETA calculation
- User cancellation support
- Automatic fallback to ffmpeg copy pipeline
"""

from __future__ import annotations

import os
import re
import time
import shutil
import asyncio
import logging
import subprocess
from urllib.parse import urlparse, parse_qs, unquote
from typing import Optional, Callable, Any

log = logging.getLogger(__name__)

HLS_DASH_PATTERNS = re.compile(
    r"(?:\.m3u8|\.mpd)(?:$|[?#])|/hls/|/dash/|/manifest(?:\.mpd|/|$)|\.m3u(?:$|[?#])",
    re.IGNORECASE
)


def is_hls_or_dash_url(url: str) -> bool:
    """Returns True if the URL is an HLS (.m3u8) or DASH (.mpd) stream manifest."""
    if not url or not isinstance(url, str):
        return False
    url_clean = url.strip()
    return bool(HLS_DASH_PATTERNS.search(url_clean))


def derive_stream_filename(url: str, default_ext: str = ".mp4") -> str:
    """Derives a clean output filename from an HLS/DASH manifest URL."""
    try:
        parsed = urlparse(url)
        path = unquote(parsed.path or "").strip().rstrip("/")
        qs = parse_qs(parsed.query)

        # 1. Check query parameters for title/filename
        for param in ("title", "filename", "name", "file", "video"):
            if param in qs and qs[param][0].strip():
                cand = qs[param][0].strip()
                cand = os.path.splitext(cand)[0]
                clean = re.sub(r'[\\/*?:"<>|]', "", cand).strip()
                if clean:
                    return f"{clean}{default_ext}"

        # 2. Check path segments
        segments = [s for s in path.split("/") if s]
        if segments:
            last = segments[-1]
            base = os.path.splitext(last)[0]
            # If the last segment is generic, use parent segment
            if base.lower() in ("master", "playlist", "index", "manifest", "chunklist", "video", "stream", "live"):
                if len(segments) >= 2:
                    parent = segments[-2]
                    clean = re.sub(r'[\\/*?:"<>|]', "", parent).strip()
                    if clean:
                        return f"{clean}{default_ext}"
            else:
                clean = re.sub(r'[\\/*?:"<>|]', "", base).strip()
                if clean:
                    return f"{clean}{default_ext}"
    except Exception as e:
        log.debug("Filename derivation failed: %s", e)

    # 3. Fallback to timestamped filename
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    return f"Stream_{timestamp}{default_ext}"


class HLSStreamDownloader:
    """High-speed HLS (.m3u8) & MPEG-DASH (.mpd) Downloader."""

    def __init__(
        self,
        target_dir: str,
        cancel_flag: Optional[list[bool]] = None,
        pause_event: Optional[asyncio.Event] = None
    ):
        self.target_dir = target_dir
        self.cancel_flag = cancel_flag or [False]
        self.pause_event = pause_event
        self.process: Optional[subprocess.Popen] = None
        self._interrupted = False

    def cancel(self):
        """Signals cancellation to the running downloader."""
        self._interrupted = True
        self.cancel_flag[0] = True
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass

    async def download(
        self,
        url: str,
        custom_filename: Optional[str] = None,
        progress_callback: Optional[Callable[[int, int, float], None]] = None
    ) -> Optional[str]:
        """
        Downloads the stream into target_dir as a single .mp4 file.
        Returns the path to the completed file, or None on failure/cancellation.
        """
        os.makedirs(self.target_dir, exist_ok=True)
        fname = custom_filename or derive_stream_filename(url)
        if not fname.lower().endswith(".mp4"):
            fname = f"{os.path.splitext(fname)[0]}.mp4"

        output_path = os.path.join(self.target_dir, fname)
        base_name = os.path.splitext(fname)[0]

        log.info("Starting HLS/DASH stream download: %s -> %s", url, output_path)

        # 1. Try yt-dlp first (multi-fragment concurrent engine)
        try:
            completed_file = await asyncio.to_thread(
                self._download_via_ytdlp,
                url,
                output_path,
                base_name,
                progress_callback
            )
            if completed_file and os.path.exists(completed_file) and os.path.getsize(completed_file) > 1024:
                return completed_file
        except InterruptedError:
            log.info("HLS stream download was cancelled by user: %s", fname)
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except Exception:
                    pass
            self._cleanup_fragments(base_name)
            return None
        except Exception as e:
            log.warning("yt-dlp stream download failed (%s: %s); falling back to ffmpeg", type(e).__name__, e)

        if self.cancel_flag[0] or self._interrupted:
            self._cleanup_fragments(base_name)
            return None

        # 2. Fallback to FFmpeg native stream capture
        try:
            completed_file = await asyncio.to_thread(
                self._download_via_ffmpeg,
                url,
                output_path,
                progress_callback
            )
            if completed_file and os.path.exists(completed_file) and os.path.getsize(completed_file) > 1024:
                return completed_file
        except InterruptedError:
            log.info("FFmpeg stream download was cancelled: %s", fname)
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except Exception:
                    pass
            self._cleanup_fragments(base_name)
            return None
        except Exception as e:
            log.error("FFmpeg stream fallback failed: %s", e)
            self._cleanup_fragments(base_name)
            raise e

        return None

    def _cleanup_fragments(self, base_name: str):
        """Cleans up leftover fragment parts or ytdl temp files for a given base filename."""
        try:
            for fn in os.listdir(self.target_dir):
                if fn.startswith(base_name) and (".part" in fn or ".ytdl" in fn):
                    fp = os.path.join(self.target_dir, fn)
                    try:
                        os.remove(fp)
                    except Exception:
                        pass
        except Exception as e:
            log.debug("Cleanup fragments error: %s", e)

    def _download_via_ytdlp(
        self,
        url: str,
        output_path: str,
        base_name: str,
        progress_callback: Optional[Callable[[int, int, float], None]]
    ) -> Optional[str]:
        """Downloads stream using yt-dlp with 8 concurrent fragment threads."""
        import yt_dlp

        last_progress_time = 0.0

        def ytdl_hook(d: dict[str, Any]):
            nonlocal last_progress_time
            if self.cancel_flag[0] or self._interrupted:
                raise InterruptedError("Download cancelled by user")

            now = time.time()
            if d.get("status") == "downloading" and (now - last_progress_time >= 0.3):
                last_progress_time = now
                downloaded = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                speed_bytes = d.get("speed") or 0
                speed_mbps = (speed_bytes * 8.0) / (1024 * 1024)

                frag_index = d.get("fragment_index")
                frag_count = d.get("fragment_count")
                if total == 0 and frag_index and frag_count:
                    total = int((downloaded / max(1, frag_index)) * frag_count)

                if progress_callback:
                    try:
                        progress_callback(downloaded, total, speed_mbps)
                    except Exception:
                        pass

        parsed = urlparse(url)
        referer = f"{parsed.scheme}://{parsed.netloc}/"

        ydl_opts = {
            "outtmpl": os.path.join(self.target_dir, f"{base_name}.%(ext)s"),
            "format": "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": 8,
            "hls_prefer_native": True,
            "nocheckcertificate": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [ytdl_hook],
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Accept": "*/*",
                "Accept-Encoding": "gzip, deflate",
                "Referer": referer,
            },
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if os.path.exists(output_path):
            return output_path

        for ext in (".mp4", ".mkv", ".ts"):
            cand = os.path.join(self.target_dir, f"{base_name}{ext}")
            if os.path.exists(cand):
                return cand

        return None

    def _download_via_ffmpeg(
        self,
        url: str,
        output_path: str,
        progress_callback: Optional[Callable[[int, int, float], None]]
    ) -> Optional[str]:
        """Downloads stream using native ffmpeg CLI with -c copy."""
        parsed = urlparse(url)
        headers = f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\nReferer: {parsed.scheme}://{parsed.netloc}/\r\n"

        cmd = [
            "ffmpeg",
            "-y",
            "-headers", headers,
            "-i", url,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            output_path
        ]

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )

        speed_pattern = re.compile(r"speed=\s*([\d\.]+)x")

        downloaded_est = 0
        last_time = time.time()

        while True:
            if self.cancel_flag[0] or self._interrupted:
                self.process.terminate()
                raise InterruptedError("Download cancelled by user")

            line = self.process.stderr.readline()
            if not line and self.process.poll() is not None:
                break

            now = time.time()
            if now - last_time >= 0.5:
                last_time = now
                if os.path.exists(output_path):
                    downloaded_est = os.path.getsize(output_path)

                speed_mbps = 0.0
                sm = speed_pattern.search(line)
                if sm:
                    try:
                        speed_mbps = float(sm.group(1)) * 10.0
                    except Exception:
                        pass

                if progress_callback:
                    try:
                        progress_callback(downloaded_est, 0, speed_mbps)
                    except Exception:
                        pass

        ret = self.process.wait()
        if ret != 0 and not os.path.exists(output_path):
            raise RuntimeError(f"FFmpeg exited with error code {ret}")

        return output_path if os.path.exists(output_path) else None
