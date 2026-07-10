"""
TelegramClientManager + Managed Upload Helpers (A1 Phase 2 complete)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional, Callable

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.network import ConnectionTcpIntermediate

log = logging.getLogger(__name__)

FLOOD_SLEEP_THRESHOLD = 300


@dataclass
class TelegramClientStats:
    created: int = 0
    connected: int = 0
    disconnected: int = 0
    errors: int = 0
    active: int = 0


class TelegramClientManager:
    def __init__(self):
        self._active_clients: set[TelegramClient] = set()
        self.stats = TelegramClientStats()
        self._on_connect: Optional[Callable] = None
        self._on_disconnect: Optional[Callable] = None

    def set_hooks(self, *, on_connect=None, on_disconnect=None):
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect

    def create_client(self, session_str: str, *, api_id=None, api_hash=None, app=None) -> TelegramClient:
        if api_id is None or api_hash is None:
            if app is not None:
                api_id = app.state.config.telegram_api_id
                api_hash = app.state.config.telegram_api_hash
            else:
                from ..config import AppConfig
                cfg = AppConfig.from_env()
                api_id = cfg.telegram_api_id
                api_hash = cfg.telegram_api_hash

        if not api_id or not api_hash:
            raise ValueError("Telegram credentials missing in configuration")

        client = TelegramClient(
            StringSession(session_str),
            api_id, api_hash,
            connection=ConnectionTcpIntermediate,
            flood_sleep_threshold=FLOOD_SLEEP_THRESHOLD,
        )
        self.stats.created += 1
        self.stats.active += 1
        self._active_clients.add(client)
        return client

    async def safe_connect(self, client: TelegramClient):
        if not client.is_connected():
            await client.connect()
            self.stats.connected += 1
            if self._on_connect:
                try:
                    if asyncio.iscoroutinefunction(self._on_connect):
                        await self._on_connect(client)
                    else:
                        self._on_connect(client)
                except Exception as e:
                    # A failing hook shouldn't break the connection itself, but is
                    # still worth a trace since it means the hook silently did nothing.
                    log.debug("on_connect hook raised: %s", e)

    async def safe_disconnect(self, client: TelegramClient):
        if client is None:
            return
        try:
            if client.is_connected():
                await client.disconnect()
                self.stats.disconnected += 1
            if self._on_disconnect:
                try:
                    if asyncio.iscoroutinefunction(self._on_disconnect):
                        await self._on_disconnect(client)
                    else:
                        self._on_disconnect(client)
                except Exception as e:
                    log.debug("on_disconnect hook raised: %s", e)
        except Exception as e:
            self.stats.errors += 1
            log.warning("safe_disconnect error: %s", e)
        finally:
            self._active_clients.discard(client)
            self.stats.active = max(0, self.stats.active - 1)

    @asynccontextmanager
    async def get_client(self, session_str: str, *, api_id=None, api_hash=None, app=None):
        client = self.create_client(session_str, api_id=api_id, api_hash=api_hash, app=app)
        try:
            await self.safe_connect(client)
            yield client
        finally:
            await self.safe_disconnect(client)

    def get_upload_client(self, session_str: str, *, api_id=None, api_hash=None, app=None) -> TelegramClient:
        client = self.create_client(session_str, api_id=api_id, api_hash=api_hash, app=app)
        setattr(client, "_streamly_use", "upload")
        return client

    async def cleanup_all(self):
        for c in list(self._active_clients):
            await self.safe_disconnect(c)

manager = TelegramClientManager()

def get_telegram_client(session_str: str, app=None):
    return manager.create_client(session_str, app=app)

async def safe_disconnect(client):
    await manager.safe_disconnect(client)

# Default hooks
async def _log_connect(c): log.debug("TG client connected")
async def _log_disconnect(c): log.debug("TG client disconnected")
manager.set_hooks(on_connect=_log_connect, on_disconnect=_log_disconnect)
