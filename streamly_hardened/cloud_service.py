from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable, Protocol
import requests

from .config import AppConfig
from .security import ValidationError

log = logging.getLogger(__name__)


class SeedrClientProtocol(Protocol):
    token: Any

    def get_settings(self) -> Any: ...
    def list_contents(self, folder_id: int) -> Any: ...
    def delete_folder(self, folder_id: int) -> Any: ...
    def delete_file(self, file_id: int) -> Any: ...
    def add_torrent(self, magnet: str) -> Any: ...
    def fetch_file(self, file_id: int) -> Any: ...
    def get_torrent_progress(self, progress_url: str) -> Any: ...


ClientFactory = Callable[[str, str], SeedrClientProtocol]


def default_seedr_client_factory(email: str, password: str) -> SeedrClientProtocol:
    from seedrcc import Seedr
    return Seedr.from_password(email, password)


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
    def __init__(self, config: AppConfig, client_factory: ClientFactory = default_seedr_client_factory):
        self.config = config
        self.client_factory = client_factory
        self.http = requests.Session()

    def login(self, email: str, password: str) -> tuple[SeedrClientProtocol, str]:
        try:
            client = self.client_factory(email, password)
            settings = client.get_settings()
            username = _safe_name(getattr(getattr(settings, "account", None), "username", ""))
            return client, username
        except requests.RequestException as e:
            log.warning("Seedr network/provider error during login: %s", e)
            raise ConnectionError("Provider unavailable") from None
        except (AttributeError, ValueError, TypeError):
            log.exception("Seedr login failed: provider response malformed")
            raise PermissionError("Invalid credentials") from None
        except Exception:
            log.exception("Unexpected error during Seedr login")
            raise PermissionError("Authentication failed") from None

    def login_with_saved_token(self, token_b64: str) -> tuple[SeedrClientProtocol, str]:
        if not token_b64 or not isinstance(token_b64, str):
            raise PermissionError("No saved token available")
        try:
            from seedrcc import Seedr
            from seedrcc.token import Token
            token = Token.from_base64(token_b64)
            client = Seedr(token=token)
            settings = client.get_settings()
            username = _safe_name(getattr(getattr(settings, "account", None), "username", ""))
            return client, username
        except requests.RequestException as e:
            log.warning("Seedr network/provider error during saved-token login: %s", e)
            raise ConnectionError("Provider unavailable") from None
        except (AttributeError, ValueError, TypeError):
            log.exception("Seedr saved-token login failed: provider response malformed")
            raise PermissionError("Saved token invalid or expired") from None
        except Exception:
            log.exception("Unexpected error during Seedr saved-token login")
            raise PermissionError("Authentication failed") from None

    @staticmethod
    def serialize_token(client: SeedrClientProtocol) -> str | None:
        """Serialize Token to base64 for Redis persistence. Returns None on failure."""
        token_obj = getattr(client, "token", None)
        if token_obj is None:
            return None
        try:
            b64 = token_obj.to_base64()
            if not isinstance(b64, str) or not b64:
                return None
            return b64
        except Exception:
            return None

    def _progress_error_types(self) -> tuple[type[BaseException], ...]:
        types: list[type[BaseException]] = [
            ConnectionError,
            TimeoutError,
            requests.RequestException,
            ValueError,
            TypeError,
            AttributeError,
        ]
        try:
            import httpx
            types.append(httpx.HTTPError)
        except ImportError:
            pass
        try:
            from seedrcc.exceptions import SeedrError
            types.append(SeedrError)
        except ImportError:
            pass
        return tuple(types)

    def _serialize_transfer(self, client: SeedrClientProtocol, torrent: Any) -> dict[str, Any]:
        progress_url = getattr(torrent, "progress_url", None)
        name = _safe_name(getattr(torrent, "name", ""))
        size = max(0, _safe_int(getattr(torrent, "size", 0)))
        progress = _safe_float(getattr(torrent, "progress", 0.0))
        stopped = _safe_int(getattr(torrent, "stopped", 0))
        download_rate = max(0.0, _safe_float(getattr(torrent, "download_rate", 0.0)))
        seeders = max(0, _safe_int(getattr(torrent, "seeders", 0)))
        warnings = _safe_name(getattr(torrent, "warnings", ""))

        if isinstance(progress_url, str) and progress_url:
            try:
                details = client.get_torrent_progress(progress_url)
                name = _safe_name(getattr(details, "title", name) or name)
                size = max(size, _safe_int(getattr(details, "size", size)))
                progress = _safe_float(getattr(details, "progress", progress))
                stopped = _safe_int(getattr(details, "stopped", stopped))
                download_rate = max(download_rate, _safe_float(getattr(details, "download_rate", download_rate)))
                warnings = _safe_name(getattr(details, "warnings", warnings) or warnings)
                stats = getattr(details, "stats", None)
                if stats is not None:
                    seeders = max(seeders, _safe_int(getattr(stats, "seeders", seeders)))
            except self._progress_error_types() as exc:
                log.info("Seedr transfer progress unavailable for torrent %s: %s", getattr(torrent, "id", "?"), exc)

        progress = min(100.0, max(0.0, progress))
        status = "Stopped" if stopped else ("Finalizing" if progress >= 100 else "Loading")
        if warnings:
            status = warnings[:80]

        return {
            "id": _safe_int(getattr(torrent, "id", 0)),
            "name": name or "Loading torrent",
            "size": size,
            "progress": progress,
            "status": status,
            "download_rate": download_rate,
            "seeders": seeders,
            "stopped": stopped,
            "last_update": getattr(torrent, "last_update", None),
        }

    def list_items(self, client: SeedrClientProtocol, folder_id: int) -> dict[str, Any]:
        try:
            contents = client.list_contents(folder_id)
            settings = client.get_settings()
            account = getattr(settings, "account", None)
            transfers = [
                self._serialize_transfer(client, torrent)
                for torrent in list(getattr(contents, "torrents", []) or [])[:100]
            ]
            return {
                "parent": _safe_int(getattr(contents, "parent_id", getattr(contents, "parent", 0))),
                "folders": [
                    {
                        "id": _safe_int(getattr(folder, "id", 0)),
                        "name": _safe_name(getattr(folder, "name", "")),
                        "size": max(0, _safe_int(getattr(folder, "size", 0))),
                        "last_update": getattr(folder, "last_update", None),
                    }
                    for folder in list(getattr(contents, "folders", []) or [])[:1000]
                ],
                "files": [
                    {
                        "id": _safe_int(getattr(file, "folder_file_id", 0)),
                        "name": _safe_name(getattr(file, "name", "")),
                        "size": max(0, _safe_int(getattr(file, "size", 0))),
                        "last_update": getattr(file, "last_update", None),
                    }
                    for file in list(getattr(contents, "files", []) or [])[:1000]
                ],
                "transfers": transfers,
                "used": max(0, _safe_int(getattr(account, "space_used", getattr(contents, "space_used", 0)))),
                "max": max(1, _safe_int(getattr(account, "space_max", getattr(contents, "space_max", 1)), 1)),
            }
        except Exception as e:
            log.exception("Error listing items for folder %s: %s", folder_id, e)
            raise ConnectionError("Provider failed to provide storage/item data") from e

    def delete_item(self, client: SeedrClientProtocol, item_type: str, item_id: int) -> None:
        if item_type == "folder":
            client.delete_folder(item_id)
        elif item_type == "file":
            client.delete_file(item_id)
        else:
            raise ValidationError("Invalid type")

    def add_magnet(self, client: SeedrClientProtocol, magnet: str) -> None:
        try:
            client.add_torrent(magnet)
        except Exception as e:
            # seedrcc raises APIError for provider rejections. A 413 ("Payload
            # Too Large") from Seedr means the torrent is too big for the
            # account's free space/quota — not a server bug. Surface it as a
            # clear ConnectionError so the route returns a meaningful message
            # instead of a generic 500.
            name = type(e).__name__
            text = str(e).lower()
            resp = getattr(e, "response", None)
            status = getattr(resp, "status_code", None)
            if name == "APIError" or status == 413 or "413" in text or "too large" in text:
                log.warning("Seedr rejected add_torrent (likely storage full / too large): %s", e)
                raise ConnectionError(
                    "Seedr rejected the torrent — it's too large for your available space."
                ) from None
            raise

    def get_stream_url(self, client: SeedrClientProtocol, file_id: int) -> str:
        try:
            url = getattr(client.fetch_file(file_id), "url", "")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                return ""
            return url
        except requests.RequestException as e:
            log.warning("Provider error fetching stream URL: %s", e)
            raise ConnectionError("Provider unavailable") from None
        except Exception:
            log.exception("Failed fetching stream URL")
            return ""

    def _fetch_archive_url(self, token: str, archive_arr: list) -> str:
        try:
            response = self.http.post(
                "https://www.seedr.cc/oauth_test/resource.php",
                data={
                    "access_token": token,
                    "func": "fetch_archive",
                    "archive_arr": __import__("json").dumps(archive_arr, separators=(",", ":")),
                },
                timeout=self.config.archive_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            url = payload.get("archive_url") or payload.get("url") or ""
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url
            return ""
        except requests.RequestException as e:
            log.warning("Archive URL request failed: %s", e)
            raise ConnectionError("Provider unavailable") from None
        except Exception:
            log.exception("Failed fetching archive URL")
            return ""

    def get_zip_url_bulk(self, client: SeedrClientProtocol, items: list) -> str:
        token_obj = getattr(client, "token", None)
        token = getattr(token_obj, "access_token", None)
        if not isinstance(token, str) or not token:
            raise PermissionError("Provider token unavailable")
        result = self._fetch_archive_url(token, items)
        if not result:
            raise ConnectionError("Failed to create zip — provider returned no URL")
        return result

    def get_zip_url(self, client: SeedrClientProtocol, item_type: str, item_id: int) -> str:
        token_obj = getattr(client, "token", None)
        token = getattr(token_obj, "access_token", None)
        if not isinstance(token, str) or not token:
            raise PermissionError("Provider token unavailable")
        return self._fetch_archive_url(token, [{"type": item_type, "id": item_id}])


def format_size(num_bytes: int) -> str:
    b = max(0, int(num_bytes))
    if b >= 1024**4: return f"{b / (1024**4):.2f} TB"
    if b >= 1024**3: return f"{b / (1024**3):.2f} GB"
    if b >= 1024**2: return f"{b / (1024**2):.1f} MB"
    if b >= 1024:    return f"{b / 1024:.1f} KB"
    return f"{b} B"
