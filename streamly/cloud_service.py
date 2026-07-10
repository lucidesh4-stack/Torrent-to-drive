from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Any
import httpx
from seedrcc import AsyncSeedr
from seedrcc.token import Token
from seedrcc.exceptions import NetworkError, ServerError, AuthenticationError, APIError

from .config import AppConfig
from .security import ValidationError

log = logging.getLogger(__name__)


class AsyncSeedrClient:
    """Async wrapper representing a Seedr API Client session."""
    def __init__(self, token: Token, username: str = ""):
        self.token = token
        self.username = username

    @property
    def access_token(self) -> str:
        return self.token.access_token


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, str):
            value = value.strip().rstrip("%")
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_name(value: Any) -> str:
    if not isinstance(value, str):
        value = str(value or "")
    return "".join(ch for ch in value if ch >= " " and ch != "\x7f")[:512]


class CloudService:
    def __init__(self, config: AppConfig):
        self.config = config

    async def _get_client(self) -> httpx.AsyncClient:
        from .core.http_client import HttpClientManager
        try:
            return await HttpClientManager.get_instance().get_client()
        except Exception:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.set_ciphers('DEFAULT@SECLEVEL=1')
            return httpx.AsyncClient(verify=ssl_ctx, timeout=30.0)

    async def login(self, email: str, password: str) -> tuple[AsyncSeedrClient, str]:
        http_client = await self._get_client()
        try:
            async_seedr = await AsyncSeedr.from_password(
                username=email,
                password=password,
                httpx_client=http_client
            )
            client = AsyncSeedrClient(async_seedr.token)
            settings = await async_seedr.get_settings()
            username = _safe_name(settings.account.username)
            client.username = username
            return client, username
        except (NetworkError, ServerError, httpx.HTTPError) as e:
            log.warning("Seedr network/provider error during login: %s", e)
            raise ConnectionError("Provider unavailable") from None
        except (AuthenticationError, APIError) as e:
            raise PermissionError(str(e)) from None
        except Exception as e:
            log.exception("Unexpected error during Seedr login: %s", e)
            raise PermissionError("Authentication failed") from None

    async def login_with_saved_token(self, token_b64: str) -> tuple[AsyncSeedrClient, str]:
        if not token_b64 or not isinstance(token_b64, str):
            raise PermissionError("No saved token available")
        try:
            token = Token.from_base64(token_b64)
            client = AsyncSeedrClient(token)
            http_client = await self._get_client()
            async with AsyncSeedr(token=token, httpx_client=http_client) as async_seedr:
                settings = await async_seedr.get_settings()
                username = _safe_name(settings.account.username)
                client.username = username
            return client, username
        except (NetworkError, ServerError, httpx.HTTPError) as e:
            log.warning("Seedr network/provider error during saved-token login: %s", e)
            raise ConnectionError("Provider unavailable") from None
        except Exception as e:
            log.exception("Seedr saved-token login failed: %s", e)
            raise PermissionError("Saved token invalid or expired") from None

    @staticmethod
    def serialize_token(client: AsyncSeedrClient) -> str | None:
        if not client or not client.token:
            return None
        try:
            return client.token.to_base64()
        except Exception:
            return None

    async def _serialize_transfer(self, async_seedr: AsyncSeedr, torrent: Torrent) -> dict[str, Any]:
        progress_url = torrent.progress_url
        name = _safe_name(torrent.name)
        size = max(0, torrent.size)
        progress = _safe_float(torrent.progress)
        stopped = torrent.stopped
        download_rate = max(0.0, _safe_float(torrent.download_rate))
        seeders = max(0, torrent.seeders)
        warnings = _safe_name(torrent.warnings or "")

        if progress_url:
            try:
                details = await async_seedr.get_torrent_progress(progress_url)
                name = _safe_name(details.title or name)
                size = max(size, details.size)
                progress = _safe_float(details.progress)
                stopped = details.stopped
                download_rate = max(download_rate, _safe_float(details.download_rate))
                warnings = _safe_name(details.warnings or warnings)
                stats = details.stats
                if stats:
                    seeders = max(seeders, stats.seeders)
                    size = max(size, stats.size)
                    progress = max(progress, _safe_float(stats.progress))
                    download_rate = max(download_rate, _safe_float(stats.download_rate))
            except Exception as exc:
                log.info("Seedr transfer progress unavailable for torrent %s: %s", torrent.id, exc)

        progress = min(100.0, max(0.0, progress))
        status = "Stopped" if stopped else ("Finalizing" if progress >= 100 else "Loading")
        if warnings:
            status = warnings[:80]

        return {
            "id": torrent.id,
            "name": name or "Loading torrent",
            "size": size,
            "progress": progress,
            "status": status,
            "download_rate": download_rate,
            "seeders": seeders,
            "stopped": stopped,
            "last_update": str(torrent.last_update) if torrent.last_update else None,
        }

    async def list_items(self, client: AsyncSeedrClient, folder_id: int) -> dict[str, Any]:
        try:
            http_client = await self._get_client()
            async with AsyncSeedr(token=client.token, httpx_client=http_client) as async_seedr:
                contents = await async_seedr.list_contents(folder_id=str(folder_id))
                
                space_used = contents.space_used
                space_max = contents.space_max
                
                if space_used is None or space_max is None or space_max == 0:
                    try:
                        usage = await async_seedr.get_memory_bandwidth()
                        space_used = usage.space_used
                        space_max = usage.space_max
                    except Exception as e:
                        log.info("Fallback get_memory_bandwidth() call also failed; storage usage may be reported as 0/1: %s", e)
                
                used_val = _safe_int(space_used)
                max_val = _safe_int(space_max) if space_max else 1

                transfers = await asyncio.gather(
                    *[self._serialize_transfer(async_seedr, t) for t in (contents.torrents or [])[:100]],
                    return_exceptions=True,
                )
                transfers = [t for t in transfers if not isinstance(t, Exception)]

                return {
                    "parent": _safe_int(contents.parent or 0),
                    "folders": [
                        {
                            "id": _safe_int(folder.id),
                            "name": _safe_name(folder.name),
                            "size": max(0, _safe_int(folder.size)),
                            "last_update": str(folder.last_update) if folder.last_update else None,
                        }
                        for folder in (contents.folders or [])[:1000]
                    ],
                    "files": [
                        {
                            "id": _safe_int(file.folder_file_id or file.file_id),
                            "name": _safe_name(file.name),
                            "size": max(0, _safe_int(file.size)),
                            "last_update": str(file.last_update) if file.last_update else None,
                        }
                        for file in (contents.files or [])[:1000]
                    ],
                    "transfers": transfers,
                    "used": max(0, used_val),
                    "max": max(1, max_val),
                }
        except Exception as e:
            log.exception("Error listing items for folder %s: %s", folder_id, e)
            raise ConnectionError("Provider failed to provide storage/item data") from e

    async def get_storage_info(self, client: AsyncSeedrClient) -> dict:
        """Returns {used, max, active_transfers} without fetching full content listing."""
        http_client = await self._get_client()
        async with AsyncSeedr(token=client.token, httpx_client=http_client) as seedr:
            contents = await seedr.list_contents(folder_id="0")
            return {
                "used": _safe_int(getattr(contents, "space_used", 0)),
                "max": max(1, _safe_int(getattr(contents, "space_max", 1))),
                "active_transfers": len(contents.torrents or []),
            }

    async def delete_item(self, client: AsyncSeedrClient, item_type: str, item_id: int) -> None:
        http_client = await self._get_client()
        async with AsyncSeedr(token=client.token, httpx_client=http_client) as async_seedr:
            if item_type == "folder":
                await async_seedr.delete_folder(str(item_id))
            elif item_type == "file":
                await async_seedr.delete_file(str(item_id))
            else:
                raise ValidationError("Invalid type")

    async def delete_transfer(self, client: AsyncSeedrClient, torrent_id: int) -> None:
        http_client = await self._get_client()
        async with AsyncSeedr(token=client.token, httpx_client=http_client) as async_seedr:
            await async_seedr.delete_torrent(str(torrent_id))

    async def get_devices(self, client: AsyncSeedrClient) -> list[dict[str, Any]]:
        try:
            http_client = await self._get_client()
            async with AsyncSeedr(token=client.token, httpx_client=http_client) as async_seedr:
                devices = await async_seedr.get_devices()
                return [
                    {
                        "name": _safe_name(d.client_name or "Unknown client"),
                        "id": _safe_name(d.client_id or ""),
                    }
                    for d in devices
                ]
        except (NetworkError, ServerError, httpx.HTTPError) as e:
            log.warning("Seedr network error fetching devices: %s", e)
            raise ConnectionError("Provider unavailable") from None
        except Exception:
            log.exception("Unexpected error fetching Seedr devices")
            raise

    async def add_magnet(self, client: AsyncSeedrClient, magnet: str) -> None:
        try:
            http_client = await self._get_client()
            async with AsyncSeedr(token=client.token, httpx_client=http_client) as async_seedr:
                result = await async_seedr.add_torrent(magnet_link=magnet)
                if not result.result:
                    raise ConnectionError("Failed to add torrent")
        except Exception as e:
            name = type(e).__name__
            text = str(e).lower()
            status = getattr(getattr(e, "response", None), "status_code", None)
            if name == "APIError" or status == 413 or "413" in text or "too large" in text:
                log.warning("Seedr rejected add_torrent (likely storage full / too large): %s", e)
                raise ConnectionError(
                    "Seedr rejected the torrent — it's too large for your available space."
                ) from None
            raise

    async def get_stream_url(self, client: AsyncSeedrClient, file_id: int) -> str:
        try:
            http_client = await self._get_client()
            async with AsyncSeedr(token=client.token, httpx_client=http_client) as async_seedr:
                result = await async_seedr.fetch_file(str(file_id))
                if not result.result or not result.url:
                    return ""
                return result.url
        except httpx.HTTPError as e:
            log.warning("Provider error fetching stream URL: %s", e)
            raise ConnectionError("Provider unavailable") from None
        except Exception:
            log.exception("Failed fetching stream URL")
            return ""

    async def _fetch_archive_url(self, token: str, archive_arr: list) -> str:
        http_client = await self._get_client()
        try:
            # Pass access_token and func as query parameters, and archive_arr in post body.
            response = await http_client.post(
                "https://www.seedr.cc/oauth_test/resource.php",
                params={
                    "access_token": token,
                    "func": "fetch_archive",
                },
                data={
                    "archive_arr": json.dumps(archive_arr, separators=(",", ":")),
                },
                timeout=self.config.archive_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            url = payload.get("archive_url") or payload.get("url") or ""
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url
            return ""
        except httpx.HTTPError as e:
            log.warning("Archive URL request failed: %s", e)
            raise ConnectionError("Provider unavailable") from None
        except Exception:
            log.exception("Failed fetching archive URL")
            return ""

    async def get_zip_url_bulk(self, client: AsyncSeedrClient, items: list) -> str:
        token = client.access_token
        if not token:
            raise PermissionError("Provider token unavailable")
        result = await self._fetch_archive_url(token, items)
        if not result:
            raise ConnectionError("Failed to create zip — provider returned no URL")
        return result

    async def get_zip_url(self, client: AsyncSeedrClient, item_type: str, item_id: int) -> str:
        token = client.access_token
        if not token:
            raise PermissionError("Provider token unavailable")
        return await self._fetch_archive_url(token, [{"type": item_type, "id": item_id}])


def format_size(num_bytes: int) -> str:
    b = max(0, int(num_bytes))
    if b >= 1024**4: return f"{b / (1024**4):.2f} TB"
    if b >= 1024**3: return f"{b / (1024**3):.2f} GB"
    if b >= 1024**2: return f"{b / (1024**2):.1f} MB"
    if b >= 1024:    return f"{b / 1024:.1f} KB"
    return f"{b} B"
