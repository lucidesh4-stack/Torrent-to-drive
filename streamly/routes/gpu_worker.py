from __future__ import annotations

import os
import time
import uuid
import asyncio
import logging
from typing import Optional, Dict, List, Any
from fastapi import APIRouter, Request, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse

from ..auth_utils import verify_user_session, ensure_sid
from ..security import rate_limited
from .temp_cloud import get_user_temp_dir

log = logging.getLogger(__name__)

gpu_router = APIRouter()

GPU_WORKER_SECRET = os.getenv('STREAMLY_GPU_SECRET', 'cloudflow_t4_gpu')

class GPUTask:
    def __init__(
        self,
        task_id: str,
        sid: str,
        file_id: str,
        filename: str,
        source_type: str = 'temp_cloud',
        mode: str = 'VBR',
        target_bitrate_k: int = 1500,
        destination: str = 'temp_cloud',
        source_url: Optional[str] = None
    ):
        self.task_id = task_id
        self.sid = sid
        self.file_id = file_id
        self.filename = filename
        self.source_type = source_type
        self.mode = mode
        self.target_bitrate_k = target_bitrate_k
        self.destination = destination
        self.source_url = source_url
        
        self.status = 'QUEUED'
        self.progress = 0.0
        self.fps = '0'
        self.speed_x = '0x'
        self.time_str = '00:00:00'
        self.orig_size_mb = 0.0
        self.new_size_mb = 0.0
        self.saved_pct = 0.0
        self.error: Optional[str] = None
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.completed_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            'task_id': self.task_id,
            'file_id': self.file_id,
            'filename': self.filename,
            'source_type': self.source_type,
            'source_url': self.source_url,
            'mode': self.mode,
            'target_bitrate_k': self.target_bitrate_k,
            'destination': self.destination,
            'status': self.status,
            'progress': round(self.progress, 1),
            'fps': self.fps,
            'speed_x': self.speed_x,
            'time_str': self.time_str,
            'orig_size_mb': round(self.orig_size_mb, 2),
            'new_size_mb': round(self.new_size_mb, 2),
            'saved_pct': round(self.saved_pct, 1),
            'error': self.error,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'completed_at': self.completed_at
        }


class GPUWorkerManager:
    _instance: Optional[GPUWorkerManager] = None

    def __init__(self):
        self._tasks: Dict[str, GPUTask] = {}
        self._queue: List[str] = []
        self._lock = asyncio.Lock()
        self.last_heartbeat = 0.0
        self.gpu_name = 'Offline'
        self.worker_info = {}

    @classmethod
    def get_instance(cls) -> GPUWorkerManager:
        if cls._instance is None:
            cls._instance = GPUWorkerManager()
        return cls._instance

    def is_online(self) -> bool:
        return (time.time() - self.last_heartbeat) < 30.0

    async def enqueue_task(
        self,
        sid: str,
        file_id: str,
        filename: str,
        source_type: str = 'temp_cloud',
        mode: str = 'VBR',
        target_bitrate_k: int = 1500,
        destination: str = 'temp_cloud',
        source_url: Optional[str] = None
    ) -> GPUTask:
        task_id = f'gpu_{uuid.uuid4().hex[:10]}'
        task = GPUTask(
            task_id=task_id,
            sid=sid,
            file_id=file_id,
            filename=filename,
            source_type=source_type,
            mode=mode,
            target_bitrate_k=target_bitrate_k,
            destination=destination,
            source_url=source_url
        )
        async with self._lock:
            self._tasks[task_id] = task
            self._queue.append(task_id)
        log.info('Enqueued Colab GPU task %s for file: %s', task_id, filename)
        return task

    async def pop_next_task(self) -> Optional[GPUTask]:
        async with self._lock:
            while self._queue:
                tid = self._queue.pop(0)
                t = self._tasks.get(tid)
                if t and t.status == 'QUEUED':
                    t.status = 'PROCESSING'
                    t.started_at = time.time()
                    return t
        return None

    def get_task(self, task_id: str) -> Optional[GPUTask]:
        return self._tasks.get(task_id)

    def list_tasks_for_user(self, sid: str) -> dict:
        user_tasks = [t.to_dict() for t in self._tasks.values() if t.sid == sid or sid == 'admin']
        user_tasks.sort(key=lambda x: x['created_at'], reverse=True)
        active = [t for t in user_tasks if t['status'] in ('QUEUED', 'PROCESSING')]
        completed = [t for t in user_tasks if t['status'] in ('COMPLETED', 'FAILED', 'CANCELLED')][:20]
        return {
            'online': self.is_online(),
            'gpu_name': self.gpu_name if self.is_online() else 'Offline',
            'active': active,
            'completed': completed
        }


gpu_mgr = GPUWorkerManager.get_instance()


@gpu_router.get('/api/gpu/status')
async def gpu_status():
    is_up = gpu_mgr.is_online()
    return {
        'online': is_up,
        'gpu_name': gpu_mgr.gpu_name if is_up else 'Offline',
        'last_seen_seconds': round(time.time() - gpu_mgr.last_heartbeat, 1) if gpu_mgr.last_heartbeat else None,
        'active_jobs': len([t for t in gpu_mgr._tasks.values() if t.status == 'PROCESSING']),
        'queued_jobs': len(gpu_mgr._queue)
    }


@gpu_router.post('/api/gpu/poll')
async def gpu_poll(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    secret = body.get('secret', '')
    if secret != GPU_WORKER_SECRET:
        raise HTTPException(status_code=403, detail='Invalid worker secret')

    gpu_mgr.last_heartbeat = time.time()
    gpu_mgr.gpu_name = body.get('gpu_name', 'NVIDIA GPU')
    gpu_mgr.worker_info = body.get('info', {})

    task = await gpu_mgr.pop_next_task()
    if task:
        if not task.source_url:
            task.source_url = f'/api/temp_cloud/stream?file_id={task.file_id}'
        return {'task': task.to_dict()}

    return {'task': None}


@gpu_router.post('/api/gpu/progress')
async def gpu_progress(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={'error': 'Invalid JSON'})

    secret = data.get('secret', '')
    if secret != GPU_WORKER_SECRET:
        raise HTTPException(status_code=403, detail='Invalid worker secret')

    task_id = data.get('task_id')
    task = gpu_mgr.get_task(task_id)
    if task:
        task.progress = float(data.get('progress', task.progress))
        task.fps = str(data.get('fps', task.fps))
        task.speed_x = str(data.get('speed_x', task.speed_x))
        task.time_str = str(data.get('time_str', task.time_str))
        return {'success': True}

    return {'success': False, 'error': 'Task not found'}


@gpu_router.post('/api/gpu/complete')
async def gpu_complete(
    task_id: str = Form(...),
    secret: str = Form(...),
    orig_mb: float = Form(0.0),
    new_mb: float = Form(0.0),
    file: UploadFile = File(...)
):
    if secret != GPU_WORKER_SECRET:
        raise HTTPException(status_code=403, detail='Invalid worker secret')

    task = gpu_mgr.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail='Task not found')

    user_dir = get_user_temp_dir(task.sid)
    dest_filename = file.filename or f'compressed_{task.filename}'
    if not dest_filename.endswith('.mp4'):
        dest_filename = f'{dest_filename}.mp4'

    out_dir = os.path.dirname(os.path.join(user_dir, task.file_id)) if task.file_id else user_dir
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, dest_filename)

    with open(out_path, 'wb') as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)

    task.status = 'COMPLETED'
    task.progress = 100.0
    task.completed_at = time.time()
    task.orig_size_mb = orig_mb
    task.new_size_mb = new_mb or (os.path.getsize(out_path) / (1024 * 1024))
    if task.orig_size_mb > 0:
        task.saved_pct = ((task.orig_size_mb - task.new_size_mb) / task.orig_size_mb) * 100.0

    log.info('GPU Task %s complete! Saved %s (%.1f MB -> %.1f MB, %.1f%% reduction)', task_id, out_path, task.orig_size_mb, task.new_size_mb, task.saved_pct)
    return {'success': True, 'saved_path': dest_filename}


@gpu_router.post('/api/gpu/compress')
@rate_limited(cost=1.0)
async def enqueue_compression(request: Request, _auth = Depends(verify_user_session)):
    sid = request.session.get('sid') or ensure_sid(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={'success': False, 'error': 'Invalid request'})

    file_id = body.get('file_id', '').strip()
    filename = body.get('filename') or os.path.basename(file_id)
    mode = body.get('mode', 'VBR')
    target_k = int(body.get('target_bitrate_k', 1500))
    source_type = body.get('source_type', 'temp_cloud')
    source_url = body.get('source_url')

    if not file_id and not source_url:
        return JSONResponse(status_code=400, content={'success': False, 'error': 'Missing file_id or source_url'})

    task = await gpu_mgr.enqueue_task(
        sid=sid,
        file_id=file_id,
        filename=filename,
        source_type=source_type,
        mode=mode,
        target_bitrate_k=target_k,
        source_url=source_url
    )

    return {'success': True, 'task': task.to_dict()}


@gpu_router.get('/api/gpu/tasks')
@rate_limited(cost=1.0)
async def list_gpu_tasks(request: Request, _auth = Depends(verify_user_session)):
    sid = request.session.get('sid') or ensure_sid(request)
    return gpu_mgr.list_tasks_for_user(sid)
