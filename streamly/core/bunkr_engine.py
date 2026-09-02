import os
import re
import math
import time
import json
import logging
import asyncio
import httpx
from typing import Optional, Callable

log = logging.getLogger(__name__)

# All known active and legacy Bunkr domains & TLDs
BUNKR_DOMAIN_PATTERN = re.compile(
    r"^(?:https?://)?(?:www\.|app\.)?(bunkr+|bunkrr)\.(?:s[kiu]|c[ir]|fi|p[hks]|ru|la|is|to|a[cx]|black|cat|media|red|site|ws|org|su|se)(?:/.*)?$",
    re.IGNORECASE
)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,video/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://bunkr.site/",
}


def is_bunkr_url(url: str) -> bool:
    """Returns True if the URL points to a Bunkr album or media file."""
    if not url or not isinstance(url, str):
        return False
    return bool(BUNKR_DOMAIN_PATTERN.match(url.strip()))


def is_gallery_dl_url(url: str) -> bool:
    """Returns True if the URL points to Bunkr or any site supported by gallery-dl."""
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if is_bunkr_url(url):
        return True
    try:
        from gallery_dl import extractor
        extr = extractor.find(url)
        if extr is not None:
            category = getattr(extr, "category", "")
            if category and category.lower() not in ("oauth", "generic"):
                return True
    except Exception:
        pass
    return False


def sanitize_folder_name(name: str) -> str:
    """Sanitizes directory names, removing illegal filesystem characters."""
    clean = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return clean if clean else "Media_Album"


def resolve_media_via_gallery_dl(url: str) -> tuple[str, list[dict]]:
    """
    Uses gallery-dl's built-in extractors to parse media metadata from 100+ sites.
    Returns (album_or_site_title, list of {'filename': ..., 'url': ...}).
    """
    try:
        from gallery_dl import job
        results = []
        album_title = ""

        class _MemoryDataJob(job.DataJob):
            def handle_url(self, item_url, kwdict):
                nonlocal album_title
                fname = kwdict.get("filename") or kwdict.get("name") or os.path.basename(item_url.split("?")[0])
                if not fname:
                    fname = f"media_{len(results)+1}"
                if kwdict.get("extension") and not fname.endswith(f".{kwdict.get('extension')}"):
                    fname = f"{fname}.{kwdict.get('extension')}"
                if not album_title:
                    title_candidate = kwdict.get("album_name") or kwdict.get("title") or kwdict.get("category") or kwdict.get("user")
                    if title_candidate:
                        album_title = sanitize_folder_name(str(title_candidate))

                results.append({
                    "filename": fname,
                    "url": item_url
                })

        data_job = _MemoryDataJob(url)
        data_job.run()
        if results:
            if not album_title:
                album_title = sanitize_folder_name(results[0]["filename"].rsplit(".", 1)[0]) if results else "Media_Album"
            log.info("gallery-dl resolved %d items for URL %s (Title: %s)", len(results), url, album_title)
            return album_title, results
    except Exception as e:
        log.warning("gallery-dl extraction failed for %s: %s; falling back to direct parser if applicable", url, e)

    return "", []


resolve_bunkr_via_gallery_dl = resolve_media_via_gallery_dl


async def resolve_bunkr_fallback(url: str) -> tuple[str, list[dict]]:
    """Fallback parser for Bunkr albums if gallery-dl encounters issues."""
    try:
        from bs4 import BeautifulSoup
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return "", []

            soup = BeautifulSoup(resp.text, "html.parser")
            
            # Extract Album Title
            album_title = "Bunkr_Album"
            h1_tag = soup.find("h1")
            if h1_tag and h1_tag.get_text().strip():
                album_title = sanitize_folder_name(h1_tag.get_text().strip())

            # Find media links in album
            items = []
            links = soup.find_all("a", href=True)
            for a in links:
                href = a["href"]
                if any(x in href for x in ("/v/", "/i/", "/d/")):
                    full_item_url = httpx.URL(url).join(href)
                    fname = os.path.basename(href.split("?")[0]) or f"file_{len(items)+1}"
                    items.append({
                        "filename": fname,
                        "url": str(full_item_url)
                    })

            return album_title, items
    except Exception as e:
        log.error("Fallback Bunkr parsing failed: %s", e)
        return "", []


class UniversalMediaGrabberDownloader:
    """
    Universal High-Speed, Resilient Media Downloader for Bunkr & 100+ gallery-dl sites.
    Features:
    - Open-ended standard Range streaming (Range: bytes=offset-).
    - aiter_raw() packet-level direct disk persistence (zero progress loss on TCP drop).
    - Up to 25x automatic seamless resume attempts.
    - Zero-keepalive fresh client instantiation per attempt to prevent broken socket reuse.
    - Polite backoff on rate limits.
    """

    def __init__(
        self,
        target_base_dir: str,
        cancel_flag: Optional[list] = None,
        pause_event: Optional[asyncio.Event] = None
    ):
        self.target_base_dir = target_base_dir
        self._cancel_flag = cancel_flag if cancel_flag is not None else [False]
        self._pause_event = pause_event

    def cancel(self):
        self._cancel_flag[0] = True

    async def _download_file(
        self,
        media_url: str,
        target_path: str,
        idx: int,
        total_items: int,
        target_filename: str,
        album_title: str,
        progress_callback: Optional[Callable] = None
    ) -> bool:
        """Downloads a media file with packet-level persistence and open-ended range resume."""
        file_downloaded = os.path.getsize(target_path) if os.path.exists(target_path) else 0
        file_total = 0
        max_retries = 25

        for attempt in range(1, max_retries + 1):
            if self._cancel_flag and self._cancel_flag[0]:
                raise asyncio.CancelledError("Download cancelled")
            if self._pause_event and not self._pause_event.is_set():
                await self._pause_event.wait()
                if self._cancel_flag and self._cancel_flag[0]:
                    raise asyncio.CancelledError("Download cancelled")

            if os.path.exists(target_path):
                file_downloaded = os.path.getsize(target_path)
            else:
                file_downloaded = 0

            # If total was known and we already have all bytes, done!
            if file_total > 0 and file_downloaded >= file_total:
                return True

            req_headers = dict(DEFAULT_HEADERS)
            req_headers["Referer"] = media_url
            if file_downloaded > 0:
                req_headers["Range"] = f"bytes={file_downloaded}-"

            timeout_cfg = httpx.Timeout(90.0, connect=20.0, read=45.0, write=30.0)
            limits_cfg = httpx.Limits(max_keepalive_connections=0, keepalive_expiry=0)

            try:
                async with httpx.AsyncClient(headers=req_headers, timeout=timeout_cfg, limits=limits_cfg, follow_redirects=True, http2=False) as client:
                    async with client.stream("GET", media_url) as resp:
                        if resp.status_code == 429:
                            backoff = min(15.0, 2.5 * attempt)
                            log.warning("[Bunkr %d/%d] Rate limited (HTTP 429) on %s (attempt %d/%d). Backing off %.1fs...", idx, total_items, target_filename, attempt, max_retries, backoff)
                            await asyncio.sleep(backoff)
                            continue

                        if resp.status_code in (403, 404, 410):
                            log.warning("[Bunkr %d/%d] Skipping %s: HTTP %d (File unavailable)", idx, total_items, target_filename, resp.status_code)
                            return False

                        if resp.status_code not in (200, 206):
                            log.warning("[Bunkr %d/%d] HTTP %d on %s (attempt %d/%d)", idx, total_items, target_filename, resp.status_code, attempt, max_retries)
                            await asyncio.sleep(min(6.0, 1.0 * attempt))
                            continue

                        # Resolve total file size
                        if resp.status_code == 206:
                            crange = resp.headers.get("content-range", "")
                            if "/" in crange:
                                try:
                                    file_total = int(crange.split("/")[-1])
                                except Exception:
                                    file_total = file_downloaded + int(resp.headers.get("content-length", 0))
                            else:
                                file_total = file_downloaded + int(resp.headers.get("content-length", 0))
                        else:
                            file_total = int(resp.headers.get("content-length", 0))
                            file_downloaded = 0  # Server sent entire file from byte 0

                        mode = "ab" if (file_downloaded > 0 and resp.status_code == 206) else "wb"
                        last_progress_time = time.time()
                        last_downloaded = file_downloaded

                        if progress_callback:
                            progress_callback(file_downloaded, file_total, 0.0, idx, total_items, target_filename, album_title)

                        with open(target_path, mode) as f:
                            async for raw_packet in resp.aiter_raw():
                                if self._cancel_flag and self._cancel_flag[0]:
                                    raise asyncio.CancelledError("Download cancelled")
                                if self._pause_event and not self._pause_event.is_set():
                                    await self._pause_event.wait()
                                    if self._cancel_flag and self._cancel_flag[0]:
                                        raise asyncio.CancelledError("Download cancelled")

                                f.write(raw_packet)
                                file_downloaded += len(raw_packet)

                                now = time.time()
                                if now - last_progress_time >= 0.35:
                                    elapsed = now - last_progress_time
                                    diff = file_downloaded - last_downloaded
                                    speed_mbps = (diff / (1024 * 1024) / elapsed) * 8.0 if elapsed > 0 else 0.0
                                    last_progress_time = now
                                    last_downloaded = file_downloaded
                                    if progress_callback:
                                        progress_callback(file_downloaded, file_total, speed_mbps, idx, total_items, target_filename, album_title)

                        if file_total == 0 or file_downloaded >= file_total:
                            return True

            except asyncio.CancelledError:
                raise
            except Exception as err:
                log.warning("[Bunkr %d/%d] Connection drop on %s (attempt %d/%d, downloaded %d/%d bytes): %s", idx, total_items, target_filename, attempt, max_retries, file_downloaded, file_total, err)
                await asyncio.sleep(min(5.0, 0.8 * attempt))

        return os.path.exists(target_path) and os.path.getsize(target_path) > 0

    async def download_album(
        self,
        url: str,
        progress_callback: Optional[Callable[[int, int, float, int, int, str, str], None]] = None
    ) -> Optional[str]:
        """
        Extracts album and downloads all files into a dedicated folder with seamless auto-resume.
        """
        log.info("Starting Universal Media Grabber for: %s", url)

        # 1. Resolve Media Metadata via gallery-dl with fallback
        if progress_callback:
            progress_callback(0, 0, 0.0, 0, 1, "Resolving media items with gallery-dl...", "Media Grabber")

        album_title, items = await asyncio.to_thread(resolve_media_via_gallery_dl, url)
        if not items and is_bunkr_url(url):
            album_title, items = await resolve_bunkr_fallback(url)

        if not items:
            log.error("Could not extract any media items from URL: %s", url)
            return None

        if self._cancel_flag and self._cancel_flag[0]:
            log.info("Media download cancelled before starting")
            return None

        # 2. Determine Album Directory
        album_dir = os.path.join(self.target_base_dir, album_title)
        os.makedirs(album_dir, exist_ok=True)
        log.info("Downloading %d media items into: %s", len(items), album_dir)

        total_items = len(items)
        completed_items = 0

        if progress_callback:
            progress_callback(0, 0, 0.0, 0, total_items, f"Found {total_items} items in {album_title}", album_title)

        for idx, item in enumerate(items, 1):
            if self._cancel_flag and self._cancel_flag[0]:
                log.info("Bunkr album download cancelled by user")
                return None
            if self._pause_event and not self._pause_event.is_set():
                await self._pause_event.wait()
                if self._cancel_flag and self._cancel_flag[0]:
                    return None

            target_filename = item.get("filename") or f"file_{idx}"
            target_path = os.path.join(album_dir, target_filename)
            media_url = item["url"]

            log.info("[Bunkr %d/%d] Downloading: %s", idx, total_items, target_filename)

            if progress_callback:
                progress_callback(0, 0, 0.0, idx, total_items, target_filename, album_title)

            download_success = False
            try:
                download_success = await self._download_file(
                    media_url=media_url,
                    target_path=target_path,
                    idx=idx,
                    total_items=total_items,
                    target_filename=target_filename,
                    album_title=album_title,
                    progress_callback=progress_callback
                )
            except asyncio.CancelledError:
                log.info("Download cancelled for %s", target_filename)
                if os.path.exists(target_path):
                    try:
                        os.remove(target_path)
                    except Exception:
                        pass
                return None
            except Exception as e:
                log.error("[Bunkr %d/%d] Download exception on %s: %s", idx, total_items, target_filename, e)
                download_success = False

            if download_success or (os.path.exists(target_path) and os.path.getsize(target_path) > 0):
                completed_items += 1
                try:
                    os.utime(target_path, (time.time(), time.time()))
                except Exception:
                    pass
                if progress_callback:
                    cur_size = os.path.getsize(target_path) if os.path.exists(target_path) else 0
                    progress_callback(cur_size, cur_size, 0.0, completed_items, total_items, target_filename, album_title)
            else:
                log.warning("[Bunkr %d/%d] Incomplete file: %s", idx, total_items, target_filename)

            # Polite pause between files to avoid CDN triggers
            await asyncio.sleep(0.5)

        if self._cancel_flag and self._cancel_flag[0]:
            return None

        log.info("Media Album Finished: %d/%d files downloaded into %s", completed_items, total_items, album_dir)
        return album_dir


# Alias for backward compatibility
BunkrSequentialDownloader = UniversalMediaGrabberDownloader
