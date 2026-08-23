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
        "https://github.com/aiogram/telegram-bot-api-executables/releases/download/7.10.0/telegram-bot-api-linux-amd64",
        "https://github.com/aiogram/telegram-bot-api-executables/releases/download/7.9.0/telegram-bot-api-linux-amd64",
        "https://github.com/aiogram/telegram-bot-api-executables/releases/download/7.7.0/telegram-bot-api-linux-amd64",
        "https://github.com/aiogram/telegram-bot-api-executables/releases/download/7.0.0/telegram-bot-api-linux-amd64",
    ]
    log.info("Downloading pre-compiled C++ telegram-bot-api TDLib binary for Linux x86_64...")
    for download_url in urls:
        try:
            cmd = ["curl", "-sSL", "--connect-timeout", "15", "-o", target_bin, download_url]
            res = subprocess.call(cmd)
            if res == 0 and os.path.exists(target_bin) and os.path.getsize(target_bin) > 1_000_000:
                os.chmod(target_bin, 0o755)
                log.info("Downloaded C++ telegram-bot-api binary via curl: %.2f MB", os.path.getsize(target_bin)/(1024*1024))
                return target_bin
        except Exception as c_err:
            log.debug("curl download from %s failed: %s", download_url, c_err)

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            for download_url in urls:
                try:
                    resp = await client.get(download_url)
                    if resp.status_code == 200 and len(resp.content) > 1_000_000:
                        with open(target_bin, "wb") as f:
                            f.write(resp.content)
                        os.chmod(target_bin, 0o755)
                        log.info("Downloaded C++ telegram-bot-api binary via httpx: %.2f MB", len(resp.content)/(1024*1024))
                        return target_bin
                except Exception as d_err:
                    log.debug("httpx download from %s failed: %s", download_url, d_err)
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
            [bin_path, f"--api-id={api_id}", f"--api-hash={api_hash}", "--local", "--http-port=8081", "--dir=/tmp/telegram-bot-api"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        for _ in range(10):
            await asyncio.sleep(1.0)
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

    timeout_config = httpx.Timeout(1200.0, connect=60.0, read=1200.0, write=1200.0)
    async with httpx.AsyncClient(timeout=timeout_config, follow_redirects=True) as client:
        # Try local C++ TDLib server daemon if active on port 8081
        if await ensure_local_bot_api_daemon():
            payload = {
                "chat_id": chat_id,
                "document": f"file://{os.path.abspath(file_path)}",
                "caption": f"File transferred: {filename}"
            }
            try:
                resp = await client.post(local_url, json=payload)
                if resp.status_code == 200:
                    elapsed = time.time() - start_time
                    speed_mbps = (file_size / (1024 * 1024) / elapsed) * 8 if elapsed > 0 else 0.0
                    log.info("Local C++ TDLib Bot API upload succeeded: %.2f MB in %.1fs (%.2f Mbps average)", file_size / (1024 * 1024), elapsed, speed_mbps)
                    return resp.json()
            except Exception as local_err:
                log.debug("Local daemon send skipped: %s", local_err)

        # Stream directly to official Telegram Cloud Bot API (https://api.telegram.org)
        log.info("Streaming file directly to Telegram Cloud Bot API (https://api.telegram.org)")
        with open(file_path, "rb") as f:
            files = {"document": (filename, f)}
            data = {"chat_id": str(chat_id), "caption": f"File transferred: {filename}"}
            resp = await client.post(cloud_url, data=data, files=files)
            resp.raise_for_status()
            elapsed = time.time() - start_time
            speed_mbps = (file_size / (1024 * 1024) / elapsed) * 8 if elapsed > 0 else 0.0
            log.info("Official Telegram Cloud Bot API upload complete: %.2f MB in %.1fs (%.2f Mbps average)", file_size / (1024 * 1024), elapsed, speed_mbps)
            return resp.json()
