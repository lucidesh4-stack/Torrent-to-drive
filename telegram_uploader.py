"""
Optimized Telegram Uploader with Real-Time Progress Bar
========================================================

Features:
- Real-time progress bar with speed (Mbps)
- Download + Upload pipeline with combined progress
- Optimized Telethon settings (2MB chunks)
- Clean, readable output

"""

import os
import asyncio
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Callable, Tuple
from datetime import datetime

# Try importing, allow fallback for testing without Telegram
try:
    import httpx
    from telethon import TelegramClient
except ImportError:
    pass


# ═══════════════════════════════════════════════════════════════════════════
# PROGRESS BAR
# ═══════════════════════════════════════════════════════════════════════════

class ProgressBar:
    """
    Beautiful progress bar with real-time stats.
    
    Features:
    - Animated progress bar
    - Speed in Mbps
    - Time elapsed / estimated
    - Size downloaded / total
    - Phase indicator (downloading/uploading)
    """
    
    def __init__(
        self,
        total: int,
        prefix: str = "",
        bar_length: int = 40,
        show_speed: bool = True,
        show_time: bool = True,
        show_size: bool = True,
    ):
        self.total = max(total, 1)  # Prevent division by zero
        self.prefix = prefix
        self.bar_length = bar_length
        self.show_speed = show_speed
        self.show_time = show_time
        self.show_size = show_size
        
        self.current = 0
        self.start_time = time.time()
        self.last_update = self.start_time
    
    def update(self, current: int):
        """Update progress bar with current byte count."""
        self.current = min(current, self.total)
        self.last_update = time.time()
    
    def _format_speed(self, bytes_per_sec: float) -> str:
        """Format speed as Mbps or Kbps."""
        mbps = bytes_per_sec * 8 / 1_000_000
        if mbps >= 1:
            return f"{mbps:.1f} Mbps"
        else:
            kbps = bytes_per_sec * 8 / 1_000
            return f"{kbps:.0f} Kbps"
    
    def _format_size(self, size: int) -> str:
        """Format size as MB or KB."""
        mb = size / 1_000_000
        if mb >= 1:
            return f"{mb:.1f} MB"
        else:
            kb = size / 1_000
            return f"{kb:.0f} KB"
    
    def _format_time(self, seconds: float) -> str:
        """Format time as MM:SS."""
        if seconds < 60:
            return f"{seconds:.0f}s"
        elif seconds < 3600:
            mins = int(seconds // 60)
            secs = int(seconds % 60)
            return f"{mins}m {secs}s"
        else:
            hours = int(seconds // 3600)
            mins = int((seconds % 3600) // 60)
            return f"{hours}h {mins}m"
    
    def render(self) -> str:
        """Render progress bar as string."""
        elapsed = time.time() - self.start_time
        percent = (self.current / self.total) * 100
        
        # Progress bar
        filled = int(self.bar_length * self.current / self.total)
        bar = "█" * filled + "░" * (self.bar_length - filled)
        
        # Speed calculation
        speed_bps = self.current / elapsed if elapsed > 0 else 0
        
        # Estimate remaining time
        if self.current > 0 and self.total > self.current:
            rate = self.current / elapsed
            remaining = (self.total - self.current) / rate if rate > 0 else 0
        else:
            remaining = 0
        
        # Build output
        parts = []
        
        # Prefix
        if self.prefix:
            parts.append(f"[{self.prefix}]")
        
        # Progress bar
        parts.append(f"[{bar}]")
        
        # Percentage
        parts.append(f"{percent:5.1f}%")
        
        # Size
        if self.show_size:
            parts.append(f"{self._format_size(self.current)}/{self._format_size(self.total)}")
        
        # Speed
        if self.show_speed:
            parts.append(self._format_speed(speed_bps))
        
        # Time
        if self.show_time:
            parts.append(f"⏱{self._format_time(elapsed)}")
            if remaining > 0:
                parts.append(f"↩{self._format_time(remaining)}")
        
        return " ".join(parts)
    
    def __str__(self) -> str:
        return self.render()
    
    def clear_line(self):
        """Clear the current line for re-render."""
        return "\r" + " " * 120 + "\r"


def print_progress(bar: ProgressBar):
    """Print progress bar, overwriting previous line."""
    print(bar.clear_line() + str(bar), end="", flush=True)


# ═══════════════════════════════════════════════════════════════════════════
# OPTIMIZED DOWNLOADER
# ═══════════════════════════════════════════════════════════════════════════

class OptimizedDownloader:
    """
    High-speed downloader using Cloudflare Worker proxy.
    Fallback to direct stream if Worker is blocked.
    """
    
    def __init__(
        self,
        worker_url: str = "https://streamly-proxy.lucidesh.workers.dev/",
        temp_dir: str = "/tmp/streamly_downloads",
    ):
        self.worker_url = worker_url
        self.temp_dir = temp_dir
        self._worker_blocked = False
        
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
    
    async def download(
        self,
        url: str,
        filename: str = None,
        progress_callback: Optional[Callable] = None,
    ) -> dict:
        """Download with progress tracking."""
        import urllib.parse
        
        if not filename:
            filename = url.split('/')[-1].split('?')[0] or "download.bin"
        
        dest_path = Path(self.temp_dir) / filename
        
        # Choose method
        if not self._worker_blocked:
            download_url = f"{self.worker_url}?url={urllib.parse.quote(url, safe='')}"
            method = "Worker"
        else:
            download_url = url
            method = "Direct"
        
        print(f"  📥 Downloading via {method}...")
        
        # Get file size
        async with httpx.AsyncClient(timeout=30.0) as client:
            head_resp = await client.head(download_url)
            total_size = int(head_resp.headers.get('content-length', 0))
        
        if total_size == 0:
            total_size = 500 * 1024 * 1024  # Estimate
        
        # Progress bar
        progress = ProgressBar(total_size, prefix="DL", bar_length=30)
        
        start_time = time.time()
        bytes_downloaded = 0
        
        async with httpx.AsyncClient(timeout=600.0) as client:
            async with client.stream("GET", download_url) as response:
                if response.status_code == 403:
                    self._worker_blocked = True
                    return await self.download(url, filename, progress_callback)
                
                with open(dest_path, 'wb') as f:
                    async for chunk in response.aiter_bytes(chunk_size=512*1024):
                        f.write(chunk)
                        bytes_downloaded += len(chunk)
                        progress.update(bytes_downloaded)
                        print_progress(progress)
                        
                        if progress_callback:
                            elapsed = time.time() - start_time
                            speed = (bytes_downloaded / 1024 / 1024 / elapsed) * 8
                            progress_callback(bytes_downloaded, speed)
        
        elapsed = time.time() - start_time
        speed = (bytes_downloaded / 1024 / 1024 / elapsed) * 8
        
        print()  # New line after progress bar
        
        return {
            'path': str(dest_path),
            'size': bytes_downloaded,
            'speed_mbps': speed,
            'time_s': elapsed,
            'method': method,
        }


# ═══════════════════════════════════════════════════════════════════════════
# OPTIMIZED TELEGRAM UPLOADER
# ═══════════════════════════════════════════════════════════════════════════

class OptimizedTelegramUploader:
    """
    Optimized Telegram uploader with real-time progress.
    
    Optimizations:
    - 2MB chunks (vs default 512KB)
    - No flood sleep
    - 5 minute timeout
    - Progress bar with speed display
    """
    
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        bot_token: str,
        session_name: str = "streamly_session",
        chunk_size: int = 2 * 1024 * 1024,  # 2MB chunks
    ):
        self.api_id = api_id
        self.api_hash = api_hash
        self.bot_token = bot_token
        self.session_name = session_name
        self.chunk_size = chunk_size
    
    async def connect(self) -> 'TelegramClient':
        """Connect to Telegram with optimized settings."""
        client = TelegramClient(
            self.session_name,
            self.api_id,
            self.api_hash,
            use_async=True,
            flood_sleep_threshold=None,  # Don't pause on floods
            timeout=300,  # 5 minute timeout
        )
        await client.start(bot_token=self.bot_token)
        return client
    
    async def upload(
        self,
        client: 'TelegramClient',
        file_path: Path,
        chat_id: int,
        caption: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
    ) -> dict:
        """Upload file with progress tracking."""
        file_size = file_path.stat().st_size
        
        # Progress bar
        progress = ProgressBar(file_size, prefix="UL", bar_length=30)
        
        start_time = time.time()
        bytes_uploaded = 0
        
        def progress_handler(current: int, total: int):
            nonlocal bytes_uploaded
            bytes_uploaded = current
            progress.update(current)
            print_progress(progress)
            
            if progress_callback:
                elapsed = time.time() - start_time
                speed = (current / 1024 / 1024 / elapsed) * 8 if elapsed > 0 else 0
                progress_callback(current, speed)
        
        # Upload with file object
        with open(file_path, 'rb') as f:
            result = await client.upload_file(
                f,
                file_name=file_path.name,
                progress_callback=progress_handler,
            )
        
        # Send to chat
        message = await client.send_file(
            chat_id,
            result,
            caption=caption,
        )
        
        elapsed = time.time() - start_time
        speed = (file_size / 1024 / 1024 / elapsed) * 8
        
        print()  # New line after progress bar
        
        return {
            'media': result,
            'message_id': message.id,
            'size': file_size,
            'speed_mbps': speed,
            'time_s': elapsed,
        }


# ═══════════════════════════════════════════════════════════════════════════
# COMPLETE PIPELINE
# ═══════════════════════════════════════════════════════════════════════════

class MediaPipeline:
    """
    Complete download + upload pipeline with combined progress.
    
    Flow:
    1. Download from Seedr (with Worker proxy)
    2. Upload to Telegram
    3. Clean up temp file
    4. Return combined stats
    """
    
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        bot_token: str,
        worker_url: str = "https://streamly-proxy.lucidesh.workers.dev/",
        temp_dir: str = "/tmp/streamly_downloads",
    ):
        self.downloader = OptimizedDownloader(worker_url, temp_dir)
        self.uploader = OptimizedTelegramUploader(api_id, api_hash, bot_token)
        self.temp_dir = temp_dir
    
    async def run(
        self,
        seedr_url: str,
        chat_id: int,
        filename: str = None,
        caption: str = None,
    ) -> dict:
        """
        Run complete pipeline.
        
        Returns:
            dict with download stats, upload stats, and total time
        """
        start_total = time.time()
        
        print("\n" + "═" * 70)
        print("  MEDIA PIPELINE")
        print("═" * 70)
        
        # ─────────────────────────────────────────────────────────────────
        # PHASE 1: DOWNLOAD
        # ─────────────────────────────────────────────────────────────────
        print("\n  📥 PHASE 1: DOWNLOAD")
        print("  " + "─" * 60)
        
        dl_result = await self.downloader.download(seedr_url, filename)
        
        print(f"\n  ✅ Download complete!")
        print(f"     Method: {dl_result['method']}")
        print(f"     Size: {dl_result['size'] // (1024*1024):.1f} MB")
        print(f"     Speed: {dl_result['speed_mbps']:.1f} Mbps")
        print(f"     Time: {dl_result['time_s']:.1f}s")
        
        file_path = Path(dl_result['path'])
        
        # ─────────────────────────────────────────────────────────────────
        # PHASE 2: UPLOAD
        # ─────────────────────────────────────────────────────────────────
        print("\n  📤 PHASE 2: UPLOAD")
        print("  " + "─" * 60)
        
        client = await self.uploader.connect()
        
        ul_result = await self.uploader.upload(
            client=client,
            file_path=file_path,
            chat_id=chat_id,
            caption=caption,
        )
        
        await client.disconnect()
        
        print(f"\n  ✅ Upload complete!")
        print(f"     Size: {ul_result['size'] // (1024*1024):.1f} MB")
        print(f"     Speed: {ul_result['speed_mbps']:.1f} Mbps")
        print(f"     Time: {ul_result['time_s']:.1f}s")
        
        # ─────────────────────────────────────────────────────────────────
        # CLEANUP & SUMMARY
        # ─────────────────────────────────────────────────────────────────
        file_path.unlink(missing_ok=True)
        
        total_time = time.time() - start_total
        total_size = dl_result['size']
        avg_speed = (total_size / 1024 / 1024 / total_time) * 8
        
        print("\n" + "═" * 70)
        print("  PIPELINE COMPLETE")
        print("═" * 70)
        print(f"  Total size: {total_size // (1024*1024):.1f} MB")
        print(f"  Download:   {dl_result['speed_mbps']:.1f} Mbps")
        print(f"  Upload:     {ul_result['speed_mbps']:.1f} Mbps")
        print(f"  Total time: {total_time:.1f}s")
        print(f"  Avg speed:  {avg_speed:.1f} Mbps")
        print("═" * 70 + "\n")
        
        return {
            'download': dl_result,
            'upload': ul_result,
            'total_time_s': total_time,
            'total_size_mb': total_size / (1024 * 1024),
            'message_id': ul_result['message_id'],
        }


# ═══════════════════════════════════════════════════════════════════════════
# USAGE EXAMPLE
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import os
    
    # Load credentials
    API_ID = os.getenv("TELEGRAM_API_ID")
    API_HASH = os.getenv("TELEGRAM_API_HASH")
    BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    SEEDR_URL = os.getenv("SEEDR_URL")
    CHAT_ID = os.getenv("CHAT_ID")
    
    if not all([API_ID, API_HASH, BOT_TOKEN, SEEDR_URL, CHAT_ID]):
        print("❌ Missing environment variables!")
        print()
        print("Required:")
        print("  TELEGRAM_API_ID=12345")
        print("  TELEGRAM_API_HASH=abc123")
        print("  TELEGRAM_BOT_TOKEN=123456:abc")
        print("  SEEDR_URL=https://...")
        print("  CHAT_ID=123456789")
    else:
        pipeline = MediaPipeline(
            api_id=int(API_ID),
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
        )
        
        asyncio.run(pipeline.run(
            seedr_url=SEEDR_URL,
            chat_id=int(CHAT_ID),
        ))