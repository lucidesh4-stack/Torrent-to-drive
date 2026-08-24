"""
Local Telegram Bot API C++ Server Daemon Manager (TDLib Engine)
"""

from __future__ import annotations

import os
import sys
import time
import logging
import asyncio
import subprocess
import httpx
from typing import Optional

log = logging.getLogger(__name__)

LOCAL_BOT_API_URL = os.environ.get("LOCAL_BOT_API_URL", "http://127.0.0.1:8081")
_daemon_process: Optional[subprocess.Popen] = None


def get_local_bot_api_url() -> str:
    return LOCAL_BOT_API_URL.rstrip("/")


async def _download_telegram_bot_api_binary() -> Optional[str]:
    """Downloads pre-compiled Linux x86_64 telegram-bot-api binary into /tmp/bin/telegram-bot-api."""
    if sys.platform == "win32":
        return None
    target_dir = "/tmp/bin"
    target_bin = "/tmp/bin/telegram-bot-api"
    if os.path.exists(target_bin) and os.path.getsize(target_bin) > 1_000_000:
        os.chmod(target_bin, 0o755)
        return target_bin

    os.makedirs(target_dir, exist_ok=True)
    urls = [
        "https://github.com/jakbin/telegram-bot-api-binary/releases/download/2026-08-05/telegram-bot-api",
        "https://github.com/jakbin/telegram-bot-api-binary/releases/download/2026-08-05glibc236/telegram-bot-api",
        "https://github.com/jakbin/telegram-bot-api-binary/releases/download/2026-05-23/telegram-bot-api",
        "https://github.com/jakbin/telegram-bot-api-binary/releases/latest/download/telegram-bot-api",
    ]
    log.info("Downloading pre-compiled C++ telegram-bot-api TDLib binary for Linux x86_64...")
    for download_url in urls:
        try:
            if os.path.exists(target_bin):
                try:
                    os.remove(target_bin)
                except Exception:
                    pass
            log.info("Attempting download from %s ...", download_url)
            cmd = ["curl", "-fsSL", "--connect-timeout", "15", "-o", target_bin, download_url]
            res = subprocess.call(cmd)
            if res == 0 and os.path.exists(target_bin) and os.path.getsize(target_bin) > 1_000_000:
                os.chmod(target_bin, 0o755)
                log.info("Downloaded C++ telegram-bot-api binary via curl: %.2f MB", os.path.getsize(target_bin)/(1024*1024))
                return target_bin
            else:
                sz = os.path.getsize(target_bin) if os.path.exists(target_bin) else 0
                log.warning("curl download from %s returned exit code %d (size: %d bytes)", download_url, res, sz)
        except Exception as c_err:
            log.warning("curl download error for %s: %s", download_url, c_err)

    import urllib.request
    for download_url in urls:
        try:
            log.info("Attempting urllib download from %s ...", download_url)
            req = urllib.request.Request(download_url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
            with urllib.request.urlopen(req, timeout=45) as response:
                content = response.read()
                if len(content) > 1_000_000:
                    with open(target_bin, "wb") as out_file:
                        out_file.write(content)
                    os.chmod(target_bin, 0o755)
                    log.info("Downloaded C++ telegram-bot-api binary via urllib: %.2f MB", len(content)/(1024*1024))
                    return target_bin
        except Exception as u_err:
            log.warning("urllib download error for %s: %s", download_url, u_err)

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            for download_url in urls:
                try:
                    resp = await client.get(download_url)
                    log.info("httpx status for %s: %d (size: %d)", download_url, resp.status_code, len(resp.content))
                    if resp.status_code == 200 and len(resp.content) > 1_000_000:
                        with open(target_bin, "wb") as f:
                            f.write(resp.content)
                        os.chmod(target_bin, 0o755)
                        log.info("Downloaded C++ telegram-bot-api binary via httpx: %.2f MB", len(resp.content)/(1024*1024))
                        return target_bin
                except Exception as d_err:
                    log.warning("httpx download error for %s: %s", download_url, d_err)
    except Exception as e:
        log.warning("Could not auto-download telegram-bot-api binary: %s", e)
    return None


async def ensure_local_bot_api_daemon() -> bool:
    """Verifies or launches local telegram-bot-api server daemon with --local mode."""
    global _daemon_process
    url = f"{get_local_bot_api_url()}/"
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            resp = await client.get(url)
            if resp.status_code in (200, 404):
                return True
    except Exception:
        pass

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH") or os.environ.get("TELEGRAM_api_hash")
    if not api_id or not api_hash:
        log.warning("TELEGRAM_API_ID or TELEGRAM_API_HASH missing in environment; cannot start local C++ TDLib daemon.")
        return False

    # Check for telegram-bot-api binary in system PATH or /tmp/bin
    bin_path = None
    for p in ["/tmp/bin/telegram-bot-api", "/usr/bin/telegram-bot-api", "/usr/local/bin/telegram-bot-api", "telegram-bot-api"]:
        if os.path.exists(p) or (sys.platform != "win32" and subprocess.call(["which", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0):
            bin_path = p
            break

    if not bin_path:
        bin_path = await _download_telegram_bot_api_binary()

    if not bin_path:
        log.warning("Local telegram-bot-api binary not available.")
        return False

    try:
        os.makedirs("/tmp/telegram-bot-api", exist_ok=True)
        log.info("Launching local telegram-bot-api C++ daemon binary on port 8081 with --local flag...")
        _daemon_process = subprocess.Popen(
            [
                bin_path,
                f"--api-id={api_id}",
                f"--api-hash={api_hash}",
                "--local",
                "--http-port=8081",
                "--dir=/tmp/telegram-bot-api"
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE
        )
        for _ in range(25):
            await asyncio.sleep(0.2)
            if _daemon_process.poll() is not None:
                _, err_out = _daemon_process.communicate()
                log.warning("Local telegram-bot-api daemon exited prematurely with code %s: %s", _daemon_process.returncode, err_out.decode('utf-8', errors='ignore'))
                return False
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    resp = await client.get(url)
                    if resp.status_code in (200, 404):
                        log.info("Local C++ TDLib Bot API daemon is UP and LISTENING on port 8081!")
                        return True
            except Exception:
                pass
        return False
    except Exception as e:
        log.warning("Could not launch local telegram-bot-api C++ daemon: %s", e)
        return False


async def upload_via_local_bot_api(
    bot_token: str,
    chat_id: str,
    file_path: str,
    filename: str,
    progress_callback=None
) -> dict:
    """
    High-speed Telegram Bot API uploader engine.
    Uses local C++ TDLib server if active on 127.0.0.1:8081, or streams directly to https://api.telegram.org at 20-30 MB/s.
    """
    local_base_url = get_local_bot_api_url()
    local_url = f"{local_base_url}/bot{bot_token}/sendDocument"
    cloud_url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    file_size = os.path.getsize(file_path)

    log.info("Starting high-speed Bot API upload for file %s (%.2f MB)", filename, file_size / (1024 * 1024))
    start_time = time.time()

    limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
    timeout_config = httpx.Timeout(1200.0, connect=60.0, read=1200.0, write=1200.0)
    async with httpx.AsyncClient(limits=limits, timeout=timeout_config, follow_redirects=True) as client:
        # Try local C++ TDLib server daemon if active on port 8081
        if await ensure_local_bot_api_daemon():
            payload = {
                "chat_id": chat_id,
                "document": f"file://{os.path.abspath(file_path)}",
                "caption": f"File transferred: {filename}"
            }
            done = [False]
            async def _live_progress_ticker():
                start_t = time.time()
                # Estimate ~12 MB/s local daemon throughput for live UI feedback
                est_speed_bytes_sec = 12 * 1024 * 1024
                while not done[0]:
                    await asyncio.sleep(0.3)
                    if done[0]:
                        break
                    elapsed = time.time() - start_t
                    est_bytes = min(int(elapsed * est_speed_bytes_sec), max(0, int(file_size * 0.95)))
                    if progress_callback:
                        try:
                            progress_callback(est_bytes, file_size)
                        except Exception:
                            pass

            ticker_task = asyncio.create_task(_live_progress_ticker())
            try:
                resp = await client.post(local_url, json=payload)
                if resp.status_code == 200:
                    elapsed = time.time() - start_time
                    speed_mbps = (file_size / (1024 * 1024) / elapsed) * 8 if elapsed > 0 else 0.0
                    log.info("Local C++ TDLib Bot API upload succeeded: %.2f MB in %.1fs (%.2f Mbps average)", file_size / (1024 * 1024), elapsed, speed_mbps)
                    return resp.json()
            except Exception as local_err:
                log.debug("Local daemon send skipped: %s", local_err)
            finally:
                done[0] = True
                ticker_task.cancel()
                if progress_callback:
                    try:
                        progress_callback(file_size, file_size)
                    except Exception:
                        pass

        # Stream directly to official Telegram Cloud Bot API (https://api.telegram.org)
        log.info("Streaming file directly to Telegram Cloud Bot API (https://api.telegram.org)")
        
        class _ProgressFileReader:
            def __init__(self, fpath, p_cb):
                self.f = open(fpath, "rb")
                self.file_size = os.path.getsize(fpath)
                self.read_bytes = 0
                self.p_cb = p_cb

            def read(self, size=-1):
                chunk = self.f.read(size)
                if chunk:
                    self.read_bytes += len(chunk)
                    if self.p_cb:
                        try:
                            self.p_cb(self.read_bytes, self.file_size)
                        except Exception:
                            pass
                return chunk

            def close(self):
                self.f.close()

        p_reader = _ProgressFileReader(file_path, progress_callback)
        try:
            files = {"document": (filename, p_reader)}
            data = {"chat_id": str(chat_id), "caption": f"File transferred: {filename}"}
            resp = await client.post(cloud_url, data=data, files=files)
            resp.raise_for_status()
            elapsed = time.time() - start_time
            speed_mbps = (file_size / (1024 * 1024) / elapsed) * 8 if elapsed > 0 else 0.0
            log.info("Official Telegram Cloud Bot API upload complete: %.2f MB in %.1fs (%.2f Mbps average)", file_size / (1024 * 1024), elapsed, speed_mbps)
            return resp.json()
        finally:
            p_reader.close()
