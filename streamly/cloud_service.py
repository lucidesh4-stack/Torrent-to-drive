from __future__ import annotations

import json
import logging
import ssl
from typing import Any, Callable, Optional
import httpx
from seedrcc.token import Token

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

    async def _api_post(self, client: AsyncSeedrClient, endpoint: str, data: dict[str, Any] = None) -> dict[str, Any]:
        http_client = await self._get_client()
        url = f"https://www.seedr.cc/api{endpoint}"
        headers = {
            "Authorization": f"Bearer {client.access_token}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        resp = await http_client.post(url, data=data or {}, headers=headers, timeout=15.0)
        resp.raise_for_status()
        return resp.json()

    async def _api_get(self, client: AsyncSeedrClient, url: str) -> dict[str, Any]:
        http_client = await self._get_client()
        headers = {"Authorization": f"Bearer {client.access_token}"}
        resp = await http_client.get(url, headers=headers, timeout=15.0)
        resp.raise_for_status()
        return resp.json()

    async def login(self, email: str, password: str) -> tuple[AsyncSeedrClient, str]:
        http_client = await self._get_client()
        url = "https://www.seedr.cc/api/account"
        data = {"username": email, "password": password}
        try:
            resp = await http_client.post(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15.0)
            resp.raise_for_status()
            result = resp.json()
            if result.get("error"):
                raise PermissionError(result["error"])
            token_str = result.get("token")
            if not token_str:
                raise PermissionError("No token in response")
            # Create Token object
            token = Token(access_token=token_str)
            username = _safe_name(result.get("account", {}).get("username", ""))
            return AsyncSeedrClient(token, username), username
        except httpx.HTTPError as e:
            log.warning("Seedr network/provider error during login: %s", e)
            raise ConnectionError("Provider unavailable") from None
        except Exception as e:
            log.exception("Unexpected error during Seedr login: %s", e)
            raise PermissionError("Authentication failed") from None

    async def login_with_saved_token(self, token_b64: str) -> tuple[AsyncSeedrClient, str]:
        if not token_b64 or not isinstance(token_b64, str):
            raise PermissionError("No saved token available")
        try:
            token = Token.from_base64(token_b64)
            client = AsyncSeedrClient(token)
            settings = await self._api_post(client, "/account")
            username = _safe_name(settings.get("account", {}).get("username", ""))
            client.username = username
            return client, username
        except httpx.HTTPError as e:
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

    async def _serialize_transfer(self, client: AsyncSeedrClient, torrent: Any) -> dict[str, Any]:
        progress_url = torrent.get("progress_url")
        name = _safe_name(torrent.get("name", ""))
        size = max(0, _safe_int(torrent.get("size", 0)))
        progress = _safe_float(torrent.get("progress", 0.0))
        stopped = _safe_int(torrent.get("stopped", 0))
        download_rate = max(0.0, _safe_float(torrent.get("download_rate", 0.0)))
        seeders = max(0, _safe_int(torrent.get("seeders", 0)))
        warnings = _safe_name(torrent.get("warnings", ""))

        if isinstance(progress_url, str) and progress_url:
            try:
                details = await self._api_get(client, progress_url)
                name = _safe_name(details.get("title", name) or name)
                size = max(size, _safe_int(details.get("size", size)))
                progress = _safe_float(details.get("progress", progress))
                stopped = _safe_int(details.get("stopped", stopped))
                download_rate = max(download_rate, _safe_float(details.get("download_rate", download_rate)))
                warnings = _safe_name(details.get("warnings", warnings) or warnings)
                stats = details.get("stats")
                if isinstance(stats, dict):
                    seeders = max(seeders, _safe_int(stats.get("seeders", seeders)))
                    size = max(size, _safe_int(stats.get("size", size)))
                    progress = max(progress, _safe_float(stats.get("progress", progress)))
                    download_rate = max(download_rate, _safe_float(stats.get("download_rate", download_rate)))
            except Exception as exc:
                log.info("Seedr transfer progress unavailable for torrent %s: %s", torrent.get("id", "?"), exc)

        progress = min(100.0, max(0.0, progress))
        status = "Stopped" if stopped else ("Finalizing" if progress >= 100 else "Loading")
        if warnings:
            status = warnings[:80]

        return {
            "id": _safe_int(torrent.get("id", 0)),
            "name": name or "Loading torrent",
            "size": size,
            "progress": progress,
            "status": status,
            "download_rate": download_rate,
            "seeders": seeders,
            "stopped": stopped,
            "last_update": torrent.get("last_update"),
        }

    async def list_items(self, client: AsyncSeedrClient, folder_id: int) -> dict[str, Any]:
        try:
            contents = await self._api_post(client, "/folder", {"folder_id": folder_id})
            
            space_used = contents.get("space_used")
            space_max = contents.get("space_max")
            
            if space_used is None or space_max is None:
                try:
                    settings = await self._api_post(client, "/account")
                    account = settings.get("account", {})
                    if space_used is None:
                        space_used = account.get("space_used")
                    if space_max is None:
                        space_max = account.get("space_max")
                except Exception:
                    pass
            
            used_val = _safe_int(space_used) if space_used is not None else 0
            max_val = _safe_int(space_max) if space_max is not None else 1

            transfers_raw = contents.get("torrents", []) or []
            transfers = []
            for torrent in transfers_raw[:100]:
                serialized = await self._serialize_transfer(client, torrent)
                transfers.append(serialized)

            return {
                "parent": _safe_int(contents.get("parent_id", contents.get("parent", 0))),
                "folders": [
                    {
                        "id": _safe_int(folder.get("id", 0)),
                        "name": _safe_name(folder.get("name", "")),
                        "size": max(0, _safe_int(folder.get("size", 0))),
                        "last_update": folder.get("last_update"),
                    }
                    for folder in (contents.get("folders", []) or [])[:1000]
                ],
                "files": [
                    {
                        "id": _safe_int(file.get("folder_file_id", file.get("id", 0))),
                        "name": _safe_name(file.get("name", "")),
                        "size": max(0, _safe_int(file.get("size", 0))),
                        "last_update": file.get("last_update"),
                    }
                    for file in (contents.get("files", []) or [])[:1000]
                ],
                "transfers": transfers,
                "used": max(0, used_val),
                "max": max(1, max_val),
            }
        except Exception as e:
            log.exception("Error listing items for folder %s: %s", folder_id, e)
            raise ConnectionError("Provider failed to provide storage/item data") from e

    async def delete_item(self, client: AsyncSeedrClient, item_type: str, item_id: int) -> None:
        if item_type == "folder":
            await self._api_post(client, "/folder/delete", {"folder_id": item_id})
        elif item_type == "file":
            await self._api_post(client, "/file/delete", {"file_id": item_id})
        else:
            raise ValidationError("Invalid type")

    async def delete_transfer(self, client: AsyncSeedrClient, torrent_id: int) -> None:
        await self._api_post(client, "/torrent/delete", {"torrent_id": torrent_id})

    async def get_devices(self, client: AsyncSeedrClient) -> list[dict[str, Any]]:
        try:
            raw = await self._api_post(client, "/devices")
            # Convert raw to a list if not already
            devices_list = raw if isinstance(raw, list) else []
        except httpx.HTTPError as e:
            log.warning("Seedr network error fetching devices: %s", e)
            raise ConnectionError("Provider unavailable") from None
        except Exception:
            log.exception("Unexpected error fetching Seedr devices")
            raise

        out: list[dict[str, Any]] = []
        for d in devices_list:
            if not isinstance(d, dict):
                continue
            name = d.get("client_name")
            cid = d.get("client_id")
            out.append({
                "name": _safe_name(name or "Unknown client"),
                "id": _safe_name(str(cid or "")),
            })
        return out

    async def add_magnet(self, client: AsyncSeedrClient, magnet: str) -> None:
        try:
            result = await self._api_post(client, "/torrent", {"torrent": magnet})
            if result.get("error"):
                raise ConnectionError(result["error"])
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
            result = await self._api_post(client, "/file", {"file_id": file_id})
            url = result.get("url", "")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                return ""
            return url
        except httpx.HTTPError as e:
            log.warning("Provider error fetching stream URL: %s", e)
            raise ConnectionError("Provider unavailable") from None
        except Exception:
            log.exception("Failed fetching stream URL")
            return ""

    async def _fetch_archive_url(self, token: str, archive_arr: list) -> str:
        http_client = await self._get_client()
        try:
            response = await http_client.post(
                "https://www.seedr.cc/oauth_test/resource.php",
                data={
                    "access_token": token,
                    "func": "fetch_archive",
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
