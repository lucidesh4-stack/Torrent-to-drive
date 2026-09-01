"""
Universal Media & Direct Link Resolver Engine
Extracts video resolutions, formats, audio streams, and direct file metadata
using yt-dlp with seamless fallback to 1DM Direct Downloader.
"""

from __future__ import annotations

import os
import time
import asyncio
import logging
from typing import Optional, Dict, Any, List

log = logging.getLogger(__name__)

def _format_bytes(size: Optional[int]) -> str:
    if not size or size <= 0:
        return "Unknown size"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"

def _format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return ""
    mins = seconds // 60
    secs = seconds % 60
    hours = mins // 60
    if hours > 0:
        return f"{hours}h {mins % 60}m {secs:02d}s"
    return f"{mins}m {secs:02d}s"

class MediaResolver:
    @staticmethod
    async def probe_url(url: str) -> Dict[str, Any]:
        """
        Probes a URL using yt-dlp and direct HTTP range check.
        Returns metadata: title, thumbnail, duration, is_media, formats list.
        """
        url = url.strip()
        
        # 1. Try yt-dlp first for web media / video sites
        def _extract_ytdlp():
            try:
                import yt_dlp
            except ImportError as ie:
                log.error("yt-dlp not available: %s", ie)
                return None

            base_opts = {
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
                'extract_flat': False,
                'socket_timeout': 15,
                'noplaylist': True,
            }

            # Pass 1: Try standard extractor (works for YouTube, Reddit, Twitter, TikTok, Vimeo, etc.)
            try:
                with yt_dlp.YoutubeDL(base_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if info:
                        return info
            except Exception as e1:
                log.warning("Standard yt-dlp extraction failed for %s: %s", url, e1)

            # Pass 2: Try with impersonate for Cloudflare protected hosts
            try:
                opts_imp = {**base_opts, 'impersonate': 'chrome'}
                with yt_dlp.YoutubeDL(opts_imp) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if info:
                        return info
            except Exception as e2:
                log.warning("Impersonate yt-dlp extraction failed for %s: %s", url, e2)

            return None

        info = await asyncio.to_thread(_extract_ytdlp)

        if info and ("formats" in info or "entries" in info):
            # Single video or entry
            title = info.get("title") or "Media Stream"
            thumb = info.get("thumbnail") or ""
            duration = _format_duration(info.get("duration"))
            uploader = info.get("uploader") or info.get("channel") or ""

            formats_list = []
            seen_res = set()

            # Process formats (Sort from highest resolution to lowest)
            raw_formats = info.get("formats", [])
            
            # Common desired heights: 1080, 720, 480, 360, audio
            for fmt in reversed(raw_formats):
                h = fmt.get("height")
                vcodec = fmt.get("vcodec", "none")
                acodec = fmt.get("acodec", "none")
                ext = fmt.get("ext", "mp4")
                fmt_id = fmt.get("format_id")
                filesize = fmt.get("filesize") or fmt.get("filesize_approx")
                
                # Check video qualities
                if h and h >= 240 and vcodec != "none":
                    label = f"{h}p"
                    if label not in seen_res:
                        seen_res.add(label)
                        formats_list.append({
                            "format_id": f"bestvideo[height<={h}]+bestaudio/best[height<={h}]",
                            "raw_format_id": fmt_id,
                            "label": f"{label} HD" if h >= 720 else f"{label} SD",
                            "resolution": f"{h}p",
                            "ext": "mp4",
                            "filesize": filesize,
                            "filesize_str": _format_bytes(filesize),
                            "is_audio": False,
                            "note": fmt.get("format_note") or f"{ext.upper()} Video"
                        })

            # Add Best Audio Only option
            best_audio = next((f for f in reversed(raw_formats) if f.get("vcodec") == "none" and f.get("acodec") != "none"), None)
            if best_audio:
                ab_size = best_audio.get("filesize") or best_audio.get("filesize_approx")
                formats_list.append({
                    "format_id": "bestaudio/best",
                    "raw_format_id": best_audio.get("format_id"),
                    "label": "Audio Only (MP3/M4A)",
                    "resolution": "Audio",
                    "ext": "mp3",
                    "filesize": ab_size,
                    "filesize_str": _format_bytes(ab_size),
                    "is_audio": True,
                    "note": f"{best_audio.get('abr', 128)} kbps Audio"
                })

            if formats_list:
                return {
                    "is_media": True,
                    "title": title,
                    "thumbnail": thumb,
                    "duration": duration,
                    "uploader": uploader,
                    "formats": formats_list
                }

        # 2. Fallback: Direct 1DM Downloader Probe (Files, zips, direct streams)
        from .direct_downloader import Direct1DMDownloader
        downloader = Direct1DMDownloader(target_dir="/tmp")
        probe = await downloader.probe(url)

        fname = probe.get("filename") or "download"
        sz = probe.get("content_length") or 0

        return {
            "is_media": False,
            "title": fname,
            "thumbnail": "",
            "duration": "",
            "uploader": "",
            "formats": [
                {
                    "format_id": "direct",
                    "label": "Direct High-Speed Download",
                    "resolution": "Original File",
                    "ext": fname.split(".")[-1] if "." in fname else "bin",
                    "filesize": sz,
                    "filesize_str": _format_bytes(sz),
                    "is_audio": False,
                    "note": "1DM Turbo Engine (16 Range Threads)"
                }
            ]
        }

    @staticmethod
    async def download_media(
        url: str,
        target_dir: str,
        format_id: Optional[str] = None,
        progress_callback = None
    ) -> str:
        """Downloads media via yt-dlp or direct 1DM engine."""
        url = url.strip()
        os.makedirs(target_dir, exist_ok=True)

        if format_id and format_id != "direct":
            def _ytdlp_download():
                import yt_dlp
                out_path = [None]
                def _hook(d):
                    if d.get("status") == "finished":
                        out_path[0] = d.get("filename")

                ydl_opts = {
                    'format': format_id,
                    'outtmpl': os.path.join(target_dir, '%(title)s.%(ext)s'),
                    'merge_output_format': 'mp4',
                    'progress_hooks': [_hook],
                    'quiet': True,
                    'no_warnings': True,
                    'socket_timeout': 30,
                    'impersonate': 'chrome',
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    return out_path[0] or ydl.prepare_filename(info)

            final_file = await asyncio.to_thread(_ytdlp_download)
            return final_file

        else:
            from .direct_downloader import Direct1DMDownloader
            downloader = Direct1DMDownloader(target_dir=target_dir, num_connections=16)
            return await downloader.download(url, progress_callback=progress_callback)
