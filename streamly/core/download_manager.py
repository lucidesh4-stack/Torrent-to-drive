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
from .bunkr_engine import is_bunkr_url, BunkrSequentialDownloader

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
        task_type = "bunkr" if is_bunkr_url(url) else "direct"
        filename = os.path.basename(url.split("?")[0]) or ("Bunkr Album" if task_type == "bunkr" else "file")

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
            if task.downloader_instance and hasattr(task.downloader_instance, "cancel"):
                task.downloader_instance.cancel()

            if task.asyncio_task and not task.asyncio_task.done():
                task.asyncio_task.cancel()

            if task.status in ("QUEUED", "DOWNLOADING", "PAUSED"):
                task.status = "CANCELLED"
                task.error = "Cancelled by user"
                task.completed_at = time.time()
                self.notify_update()
                log.info("Cancelled download task: %s", task_id)
                return True

            return False

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
                    task.status = "FAILED"
                    task.error = str(e)
                finally:
                    task.completed_at = time.time()
                    self.notify_update()
                    self._queue.task_done()

            except Exception as e:
                log.error("Queue worker error: %s", e)
                await asyncio.sleep(1.0)

    async def _run_task(self, task: DownloadTask):
        """Executes a single download task."""
        if task.task_type == "bunkr":
            await self._run_bunkr_task(task)
        else:
            await self._run_direct_1dm_task(task)

    async def _run_direct_1dm_task(self, task: DownloadTask):
        downloader = Direct1DMDownloader(target_dir=task.target_dir, num_connections=16)
        task.downloader_instance = downloader

        # Progress hook
        def _on_progress(downloaded: int, total: int, speed_mbps: float):
            task.downloaded_bytes = downloaded
            task.total_bytes = total
            task.progress = (downloaded / total * 100.0) if total > 0 else 0.0
            task.speed_mbps = speed_mbps
            self.notify_update()

        downloaded_file = await downloader.download(task.url, progress_callback=_on_progress)
        if downloaded_file:
            task.filename = os.path.basename(downloaded_file)
            if task.auto_unzip and is_archive(downloaded_file):
                task.status = "EXTRACTING"
                self.notify_update()
                await asyncio.to_thread(safe_extract_archive, downloaded_file, task.target_dir, delete_archive=True)

            task.status = "COMPLETED"
            task.progress = 100.0
            task.speed_mbps = 0.0

    async def _run_bunkr_task(self, task: DownloadTask):
        downloader = BunkrSequentialDownloader(target_base_dir=task.target_dir)
        task.downloader_instance = downloader

        def _on_bunkr_progress(current: int, total: int, current_file: str):
            task.current_item = current
            task.total_items = total
            task.filename = f"{current_file} ({current}/{total})"
            task.progress = (current / total * 100.0) if total > 0 else 0.0
            self.notify_update()

        album_dir = await downloader.download_album(task.url, progress_callback=_on_bunkr_progress)
        if album_dir:
            task.album_name = os.path.basename(album_dir)
            task.filename = f"Album: {task.album_name}"
            task.status = "COMPLETED"
            task.progress = 100.0
            task.speed_mbps = 0.0
        else:
            task.status = "FAILED"
            task.error = "Could not resolve or download Bunkr album items"
