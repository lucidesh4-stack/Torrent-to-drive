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


def sanitize_folder_name(name: str) -> str:
    """Sanitizes directory names, removing illegal filesystem characters."""
    clean = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return clean if clean else "Bunkr_Album"


def resolve_bunkr_via_gallery_dl(url: str) -> tuple[str, list[dict]]:
    """
    Uses gallery-dl's built-in extractor to parse Bunkr album/file metadata.
    Returns (album_title, list of {'filename': ..., 'url': ...}).
    """
    try:
        from gallery_dl import job
        results = []
        album_title = "Bunkr_Album"

        class _MemoryDataJob(job.DataJob):
            def handle_url(self, item_url, kwdict):
                nonlocal album_title
                fname = kwdict.get("filename") or kwdict.get("name") or os.path.basename(item_url.split("?")[0])
                if not fname:
                    fname = f"bunkr_{len(results)+1}"
                if kwdict.get("extension") and not fname.endswith(f".{kwdict.get('extension')}"):
                    fname = f"{fname}.{kwdict.get('extension')}"
                if kwdict.get("album_name") and album_title == "Bunkr_Album":
                    album_title = sanitize_folder_name(kwdict.get("album_name"))

                results.append({
                    "filename": fname,
                    "url": item_url
                })

        data_job = _MemoryDataJob(url)
        data_job.run()
        if results:
            log.info("gallery-dl resolved %d items for Bunkr URL %s (Album: %s)", len(results), url, album_title)
            return album_title, results
    except Exception as e:
        log.warning("gallery-dl extraction failed for %s: %s; falling back to direct parser", url, e)

    return "", []


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


class BunkrSequentialDownloader:
    """
    Sequential, polite downloader for Bunkr albums.
    Strictly limits concurrency to 1 and applies polite pauses between files
    to prevent Bunkr CDN IP bans (429/403).
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
        log.info("Starting Bunkr Sequential Album Downloader for: %s", url)

        # 1. Resolve Album Metadata via gallery-dl with fallback
        album_title, items = await asyncio.to_thread(resolve_bunkr_via_gallery_dl, url)
        if not items:
            album_title, items = await resolve_bunkr_fallback(url)

        if not items:
            log.error("Could not extract any media items from Bunkr URL: %s", url)
            return None

        if self._cancel_flag and self._cancel_flag[0]:
            log.info("Bunkr album download cancelled before starting")
            return None

        # 2. Create Album Directory
        album_dir = os.path.join(self.target_base_dir, album_title)
        os.makedirs(album_dir, exist_ok=True)
        log.info("Downloading %d Bunkr items into: %s", len(items), album_dir)

        total_items = len(items)
        completed_items = 0

        # 3. Strictly Sequential Download Loop (Concurrency = 1)
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=60.0, follow_redirects=True) as client:
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

                try:
                    # Stream directly to disk
                    async with client.stream("GET", media_url) as resp:
                        if resp.status_code >= 400:
                            log.warning("[Bunkr %d/%d] Skipping %s: HTTP %d", idx, total_items, target_filename, resp.status_code)
                            continue

                        file_total = int(resp.headers.get("content-length", 0))
                        file_downloaded = 0
                        last_progress_time = time.time()
                        last_downloaded = 0

                        with open(target_path, "wb") as f:
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

                    completed_items += 1
                    if progress_callback:
                        progress_callback(file_downloaded, file_total, 0.0, completed_items, total_items, target_filename, album_title)

                    # Polite CDN Cooldown (0.5s pause) to keep Bunkr CDN connection healthy
                    await asyncio.sleep(0.5)

                except asyncio.CancelledError:
                    log.info("Bunkr download cancelled while downloading %s", target_filename)
                    if os.path.exists(target_path):
                        try:
                            os.remove(target_path)
                        except Exception:
                            pass
                    return None
                except Exception as dl_err:
                    log.warning("[Bunkr %d/%d] Failed to download %s: %s", idx, total_items, target_filename, dl_err)
                    if os.path.exists(target_path) and os.path.getsize(target_path) == 0:
                        try:
                            os.remove(target_path)
                        except Exception:
                            pass

        if self._cancel_flag and self._cancel_flag[0]:
            return None

        log.info("Bunkr Album Finished: %d/%d files downloaded into %s", completed_items, total_items, album_dir)
        return album_dir
