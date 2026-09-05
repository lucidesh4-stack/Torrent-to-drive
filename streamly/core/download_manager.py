"""
Cloudflow Central Download Manager
Tracks and orchestrates all 1DM Direct Downloads and Bunkr Albums with live progress,
speed calculation, cancellation, pause/resume, and real-time SSE updates.
"""

from __future__ import annotations

import os
import time
import uuid
import asyncio
import logging
from typing import Optional, Dict, List, Any
from .direct_downloader import Direct1DMDownloader
from .archive_extractor import is_archive, safe_extract_archive
from .bunkr_engine import is_bunkr_url, is_gallery_dl_url, UniversalMediaGrabberDownloader, BunkrSequentialDownloader
from .stream_downloader import is_hls_or_dash_url, derive_stream_filename, HLSStreamDownloader

log = logging.getLogger(__name__)


class DownloadTask:
    def __init__(
        self,
        task_id: str,
        url: str,
        filename: str,
        target_dir: str,
        auto_unzip: bool = False,
        task_type: str = "direct",
    ):
        self.task_id = task_id
        self.url = url
        self.filename = filename
        self.target_dir = target_dir
        self.auto_unzip = auto_unzip
        self.task_type = task_type
        self.status = "QUEUED"  # QUEUED, DOWNLOADING, EXTRACTING, COMPLETED, FAILED, CANCELLED, PAUSED
        self.total_bytes = 0
        self.downloaded_bytes = 0
        self.progress = 0.0
        self.speed_mbps = 0.0
        self.error: Optional[str] = None
        self.album_name: Optional[str] = None
        self.current_item = 0
        self.total_items = 1
        self.created_at = time.time()
        self.completed_at: Optional[float] = None
        
        self.cancel_flag = [False]
        self.pause_event = asyncio.Event()
        self.pause_event.set()  # Not paused by default
        self.downloader_instance: Any = None
        self.asyncio_task: Optional[asyncio.Task] = None

    def to_dict(self) -> dict:
        speed_mb = round(self.speed_mbps / 8.0, 2) if self.speed_mbps else 0.0
        return {
            "task_id": self.task_id,
            "url": self.url,
            "filename": self.filename,
            "type": self.task_type,
            "status": self.status,
            "total_bytes": self.total_bytes,
            "downloaded_bytes": self.downloaded_bytes,
            "progress": round(self.progress, 1),
            "speed_mbps": round(self.speed_mbps, 2),
            "speed_mb": speed_mb,
            "error": self.error,
            "album_name": self.album_name,
            "current_item": self.current_item,
            "total_items": self.total_items,
            "created_at": self.created_at,
            "completed_at": self.completed_at
        }


class DownloadManager:
    _instance: Optional[DownloadManager] = None

    def __init__(self):
        self._tasks: Dict[str, DownloadTask] = {}
        self._queue: asyncio.Queue[DownloadTask] = asyncio.Queue()
        self._worker_running = False
        self._subscribers: List[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> DownloadManager:
        if cls._instance is None:
            cls._instance = DownloadManager()
        return cls._instance

    @property
    def tasks(self) -> Dict[str, DownloadTask]:
        return self._tasks

    @property
    def active_tasks(self) -> Dict[str, DownloadTask]:
        return {tid: t for tid, t in self._tasks.items() if t.status in ("DOWNLOADING", "QUEUED", "EXTRACTING", "PAUSED")}

    def notify_update(self):
        """Notify all SSE listeners that progress has updated."""
        state = self.get_state()
        for q in list(self._subscribers):
            try:
                q.put_nowait(state)
            except Exception:
                pass

    def get_state(self) -> dict:
        active = []
        queue = []
        completed = []

        # Sort tasks by creation time
        sorted_tasks = sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)

        for t in sorted_tasks:
            d = t.to_dict()
            if t.status in ("DOWNLOADING", "EXTRACTING", "PAUSED"):
                active.append(d)
            elif t.status == "QUEUED":
                queue.append(d)
            else:
                completed.append(d)

        return {
            "active": active,
            "queue": queue,
            "completed": completed[:25]  # Keep recent 25 completed/failed
        }

    async def enqueue(
        self,
        url: str,
        target_dir: str,
        auto_unzip: bool = True
    ) -> DownloadTask:
        task_id = f"dl_{uuid.uuid4().hex[:10]}"
        is_stream = is_hls_or_dash_url(url)
        is_gallery = not is_stream and is_gallery_dl_url(url)

        if is_stream:
            task_type = "stream"
            filename = derive_stream_filename(url)
        elif is_gallery:
            task_type = "media_grabber"
            filename = "Media Album"
        else:
            task_type = "direct"
            filename = os.path.basename(url.split("?")[0]) or "file"

        task = DownloadTask(
            task_id=task_id,
            url=url,
            filename=filename,
            target_dir=target_dir,
            auto_unzip=auto_unzip,
            task_type=task_type
        )

        async with self._lock:
            self._tasks[task_id] = task

        # Ensure background queue worker is running
        if not self._worker_running:
            self._worker_running = True
            asyncio.create_task(self._process_queue_worker())

        await self._queue.put(task)
        self.notify_update()
        log.info("Enqueued download task %s: %s (%s)", task_id, url, task_type)
        return task

    async def cancel_task(self, task_id: str) -> bool:
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            task.cancel_flag[0] = True
            task.pause_event.set()  # Unblock event if paused so worker can cleanly exit

            if task.downloader_instance and hasattr(task.downloader_instance, "cancel"):
                task.downloader_instance.cancel()

            if task.asyncio_task and not task.asyncio_task.done():
                task.asyncio_task.cancel()

            task.status = "CANCELLED"
            task.error = "Cancelled by user"
            task.completed_at = time.time()
            self.notify_update()
            log.info("Cancelled download task: %s", task_id)
            return True

    async def pause_task(self, task_id: str) -> bool:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task and task.status == "DOWNLOADING":
                task.pause_event.clear()
                task.status = "PAUSED"
                task.speed_mbps = 0.0
                self.notify_update()
                return True
        return False

    async def resume_task(self, task_id: str) -> bool:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task and task.status == "PAUSED":
                task.pause_event.set()
                task.status = "DOWNLOADING"
                self.notify_update()
                return True
        return False

    async def _process_queue_worker(self):
        """Worker loop processing downloads sequentially from the queue."""
        log.info("Starting Cloudflow Download Queue Worker")
        while True:
            try:
                task = await self._queue.get()
                if task.cancel_flag[0] or task.status == "CANCELLED":
                    self._queue.task_done()
                    continue

                task.status = "DOWNLOADING"
                self.notify_update()

                # Run download in a managed subtask
                t = asyncio.create_task(self._run_task(task))
                task.asyncio_task = t
                try:
                    await t
                except asyncio.CancelledError:
                    task.status = "CANCELLED"
                    task.error = "Cancelled by user"
                except Exception as e:
                    if task.cancel_flag[0] or task.status == "CANCELLED":
                        task.status = "CANCELLED"
                        task.error = "Cancelled by user"
                    else:
                        task.status = "FAILED"
                        task.error = str(e)
                finally:
                    if task.cancel_flag[0]:
                        task.status = "CANCELLED"
                        task.error = "Cancelled by user"
                    task.completed_at = time.time()
                    self.notify_update()
                    self._queue.task_done()

            except Exception as e:
                log.error("Queue worker error: %s", e)
                await asyncio.sleep(1.0)

    async def _run_task(self, task: DownloadTask):
        """Executes a single download task."""
        if task.task_type == "stream":
            await self._run_stream_task(task)
        elif task.task_type in ("bunkr", "media_grabber"):
            await self._run_media_grabber_task(task)
        else:
            await self._run_direct_1dm_task(task)

    async def _run_stream_task(self, task: DownloadTask):
        downloader = HLSStreamDownloader(
            target_dir=task.target_dir,
            cancel_flag=task.cancel_flag,
            pause_event=task.pause_event
        )
        task.downloader_instance = downloader

        def _on_stream_progress(downloaded: int, total: int, speed_mbps: float):
            task.downloaded_bytes = downloaded
            task.total_bytes = total
            if total > 0:
                task.progress = min(99.0, (downloaded / total * 100.0))
            task.speed_mbps = speed_mbps
            self.notify_update()

        downloaded_file = await downloader.download(
            task.url,
            custom_filename=task.filename,
            progress_callback=_on_stream_progress
        )
        if downloaded_file and os.path.exists(downloaded_file):
            task.filename = os.path.basename(downloaded_file)
            task.total_bytes = os.path.getsize(downloaded_file)
            task.downloaded_bytes = task.total_bytes
            task.progress = 100.0
            task.status = "COMPLETED"
            task.speed_mbps = 0.0
        else:
            if not task.cancel_flag[0]:
                task.status = "FAILED"
                task.error = task.error or "Stream capture failed"

    async def _run_direct_1dm_task(self, task: DownloadTask):
        downloader = Direct1DMDownloader(
            target_dir=task.target_dir,
            num_connections=16,
            cancel_flag=task.cancel_flag,
            pause_event=task.pause_event
        )
        task.downloader_instance = downloader

        # Progress hook
        def _on_progress(downloaded: int, total: int, speed_mbps: float):
            task.downloaded_bytes = downloaded
            if total > 0:
                effective_total = max(total, downloaded)
                task.total_bytes = effective_total
                task.progress = min(99.9, (downloaded / effective_total * 100.0))
            else:
                task.total_bytes = downloaded
                task.progress = 0.0
            task.speed_mbps = speed_mbps
            self.notify_update()

        downloaded_file = await downloader.download(task.url, progress_callback=_on_progress)
        if downloaded_file:
            task.filename = os.path.basename(downloaded_file)
            if task.auto_unzip and is_archive(downloaded_file):
                task.status = "EXTRACTING"
                self.notify_update()
                await asyncio.to_thread(safe_extract_archive, downloaded_file, task.target_dir, delete_archive=True)

            if not task.cancel_flag[0]:
                task.status = "COMPLETED"
                task.progress = 100.0
                task.speed_mbps = 0.0

    async def _run_media_grabber_task(self, task: DownloadTask):
        downloader = UniversalMediaGrabberDownloader(
            target_base_dir=task.target_dir,
            cancel_flag=task.cancel_flag,
            pause_event=task.pause_event
        )
        task.downloader_instance = downloader

        def _on_media_progress(file_downloaded: int, file_total: int, speed_mbps: float, current: int, total: int, current_file: str, album_title: str):
            task.downloaded_bytes = file_downloaded
            task.total_bytes = file_total
            task.speed_mbps = speed_mbps
            task.current_item = current
            task.total_items = total
            task.filename = f"[{current}/{total}] {current_file}"
            task.album_name = album_title
            file_ratio = (file_downloaded / file_total) if file_total > 0 else 0.0
            task.progress = min(100.0, max(0.0, ((current - 1 + file_ratio) / total * 100.0))) if total > 0 else 0.0
            self.notify_update()

        album_dir = await downloader.download_album(task.url, progress_callback=_on_media_progress)
        if album_dir and not task.cancel_flag[0]:
            task.album_name = os.path.basename(album_dir)
            task.filename = f"Album: {task.album_name}"
            task.status = "COMPLETED"
            task.progress = 100.0
            task.speed_mbps = 0.0
        elif task.cancel_flag[0]:
            task.status = "CANCELLED"
            task.error = "Cancelled by user"
        else:
            task.status = "FAILED"
            task.error = "Could not resolve or download media items"

    _run_bunkr_task = _run_media_grabber_task
