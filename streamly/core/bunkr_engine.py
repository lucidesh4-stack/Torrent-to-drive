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
    Universal polite, sequential media downloader for Bunkr and 100+ gallery-dl supported sites.
    Strictly limits concurrency to 1 and applies polite pauses between files
    to prevent CDN IP bans (429/403).
    """

    def __init__(
        self,
        target_base_dir: str,
        cancel_flag: Optional[list] = None,
        pause_event: Optional[asyncio.Event] = None
    ):
        self.target_base_dir = target_base_dir
        self.chunk_size = 1024 * 1024  # 1 MB chunk streaming
        self._cancel_flag = cancel_flag if cancel_flag is not None else [False]
        self._pause_event = pause_event

    def cancel(self):
        self._cancel_flag[0] = True

    async def download_album(
        self,
        url: str,
        progress_callback: Optional[Callable[[int, int, float, int, int, str, str], None]] = None
    ) -> Optional[str]:
        """
        Extracts album and downloads all files sequentially into a dedicated folder.
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

        # 3. Strictly Sequential Download Loop (Concurrency = 1) with Range Resume
        timeout_cfg = httpx.Timeout(180.0, connect=30.0, read=90.0, write=60.0)
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=timeout_cfg, follow_redirects=True, http2=False) as client:
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

                # Announce item start to UI immediately
                if progress_callback:
                    progress_callback(0, 0, 0.0, idx, total_items, target_filename, album_title)

                file_downloaded = 0
                file_total = 0
                max_retries = 5
                download_success = False

                for attempt in range(1, max_retries + 1):
                    if self._cancel_flag and self._cancel_flag[0]:
                        raise asyncio.CancelledError("Download cancelled")
                    if self._pause_event and not self._pause_event.is_set():
                        await self._pause_event.wait()
                        if self._cancel_flag and self._cancel_flag[0]:
                            raise asyncio.CancelledError("Download cancelled")

                    # Check for partial file on disk to resume
                    if os.path.exists(target_path):
                        file_downloaded = os.path.getsize(target_path)
                    else:
                        file_downloaded = 0

                    req_headers = dict(DEFAULT_HEADERS)
                    req_headers["Referer"] = media_url
                    if file_downloaded > 0:
                        req_headers["Range"] = f"bytes={file_downloaded}-"

                    try:
                        async with client.stream("GET", media_url, headers=req_headers) as resp:
                            if resp.status_code == 429:
                                backoff = 3.0 * attempt
                                log.warning("[Bunkr %d/%d] Rate limited (HTTP 429) on %s. Backing off for %.1fs (attempt %d/%d)...", idx, total_items, target_filename, backoff, attempt, max_retries)
                                await asyncio.sleep(backoff)
                                continue

                            if resp.status_code in (403, 404, 410):
                                log.warning("[Bunkr %d/%d] Skipping %s: HTTP %d (File unavailable)", idx, total_items, target_filename, resp.status_code)
                                break

                            if resp.status_code not in (200, 206):
                                log.warning("[Bunkr %d/%d] HTTP %d on %s (attempt %d/%d)", idx, total_items, target_filename, resp.status_code, attempt, max_retries)
                                await asyncio.sleep(2.0 * attempt)
                                continue

                            # Parse total size
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
                                file_downloaded = 0  # Full file stream

                            mode = "ab" if (file_downloaded > 0 and resp.status_code == 206) else "wb"
                            last_progress_time = time.time()
                            last_downloaded = file_downloaded

                            if progress_callback:
                                progress_callback(file_downloaded, file_total, 0.0, idx, total_items, target_filename, album_title)

                            with open(target_path, mode) as f:
                                async for chunk in resp.aiter_bytes(chunk_size=self.chunk_size):
                                    if self._cancel_flag and self._cancel_flag[0]:
                                        raise asyncio.CancelledError("Download cancelled")
                                    if self._pause_event and not self._pause_event.is_set():
                                        await self._pause_event.wait()
                                        if self._cancel_flag and self._cancel_flag[0]:
                                            raise asyncio.CancelledError("Download cancelled")

                                    f.write(chunk)
                                    file_downloaded += len(chunk)

                                    now = time.time()
                                    if now - last_progress_time >= 0.4:
                                        elapsed = now - last_progress_time
                                        diff = file_downloaded - last_downloaded
                                        speed_mbps = (diff / (1024 * 1024) / elapsed) * 8.0 if elapsed > 0 else 0.0
                                        last_progress_time = now
                                        last_downloaded = file_downloaded
                                        if progress_callback:
                                            progress_callback(file_downloaded, file_total, speed_mbps, idx, total_items, target_filename, album_title)

                            if file_total == 0 or file_downloaded >= file_total:
                                download_success = True
                                break

                    except asyncio.CancelledError:
                        log.info("Bunkr download cancelled while downloading %s", target_filename)
                        if os.path.exists(target_path):
                            try:
                                os.remove(target_path)
                            except Exception:
                                pass
                        return None
                    except Exception as err:
                        log.warning("[Bunkr %d/%d] Chunk error on %s (attempt %d/%d, downloaded %d/%d bytes): %s", idx, total_items, target_filename, attempt, max_retries, file_downloaded, file_total, err)
                        await asyncio.sleep(1.5 * attempt)

                if download_success or (os.path.exists(target_path) and os.path.getsize(target_path) > 0):
                    completed_items += 1
                    try:
                        os.utime(target_path, (time.time(), time.time()))
                    except Exception:
                        pass
                    if progress_callback:
                        progress_callback(file_downloaded, file_total, 0.0, completed_items, total_items, target_filename, album_title)
                else:
                    log.warning("[Bunkr %d/%d] Giving up on %s after %d failed attempts", idx, total_items, target_filename, max_retries)
                    if os.path.exists(target_path) and os.path.getsize(target_path) == 0:
                        try:
                            os.remove(target_path)
                        except Exception:
                            pass

                # Polite pause between Bunkr files
                await asyncio.sleep(0.8)

        if self._cancel_flag and self._cancel_flag[0]:
            return None

        log.info("Media Album Finished: %d/%d files downloaded into %s", completed_items, total_items, album_dir)
        return album_dir


# Alias for backward compatibility
BunkrSequentialDownloader = UniversalMediaGrabberDownloader
