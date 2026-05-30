from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import ipaddress
import logging
import socket
import threading
import time
from typing import Any, Callable, Protocol
from urllib.parse import quote

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


ClientFactory = Callable[[str, str], SeedrClientProtocol]


def default_seedr_client_factory(email: str, password: str) -> SeedrClientProtocol:
    from seedrcc import Seedr
    return Seedr.from_password(email, password)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
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
        except Exception:
            log.exception("Seedr login failed")
            raise PermissionError("Invalid credentials") from None

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
        except Exception:
            log.exception("Seedr saved-token login failed")
            raise PermissionError("Saved token invalid or expired") from None

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

    def list_items(self, client: SeedrClientProtocol, folder_id: int) -> dict[str, Any]:
        contents = client.list_contents(folder_id)
        settings = client.get_settings()
        account = getattr(settings, "account", None)
        return {
            "parent": _safe_int(getattr(contents, "parent_id", 0)),
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
            "used": max(0, _safe_int(getattr(account, "space_used", 0))),
            "max": max(1, _safe_int(getattr(account, "space_max", 1), 1)),
        }

    def delete_item(self, client: SeedrClientProtocol, item_type: str, item_id: int) -> None:
        if item_type == "folder":
            client.delete_folder(item_id)
        elif item_type == "file":
            client.delete_file(item_id)
        else:
            raise ValidationError("Invalid type")

    def add_magnet(self, client: SeedrClientProtocol, magnet: str) -> None:
        client.add_torrent(magnet)

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


_BITSEARCH_DNS_LOCK = threading.RLock()
_BITSEARCH_IP_CACHE: tuple[str, float] | None = None


def _is_name_resolution_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "name or service not known" in text or "getaddrinfo failed" in text or "failed to resolve" in text


def _resolve_bitsearch_via_doh(timeout: float) -> str | None:
    global _BITSEARCH_IP_CACHE
    now = time.monotonic()
    if _BITSEARCH_IP_CACHE and _BITSEARCH_IP_CACHE[1] > now:
        return _BITSEARCH_IP_CACHE[0]
    try:
        response = requests.get(
            "https://1.1.1.1/dns-query",
            params={"name": "bitsearch.eu", "type": "A"},
            headers={"accept": "application/dns-json", "User-Agent": "Streamly/1.0"},
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        for answer in data.get("Answer", []) if isinstance(data, dict) else []:
            candidate = answer.get("data")
            try:
                ip = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if ip.version == 4 and not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast):
                _BITSEARCH_IP_CACHE = (str(ip), now + 300)
                return str(ip)
    except (requests.RequestException, ValueError) as exc:
        log.warning("Cloudflare DoH fallback for bitsearch.eu failed: %s", exc)
    return None


@contextmanager
def _temporary_bitsearch_resolution(ip: str):
    old_getaddrinfo = socket.getaddrinfo

    def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host == "bitsearch.eu":
            return old_getaddrinfo(ip, port, family or socket.AF_INET, type, proto, flags)
        return old_getaddrinfo(host, port, family, type, proto, flags)

    with _BITSEARCH_DNS_LOCK:
        socket.getaddrinfo = patched_getaddrinfo
        try:
            yield
        finally:
            socket.getaddrinfo = old_getaddrinfo


class SearchService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.http = requests.Session()

    def imdb_suggestions(self, q: str) -> list[dict[str, Any]]:
        url = self.config.imdb_suggest_template.format(query=quote(q.lower(), safe=""))
        try:
            response = self.http.get(url, timeout=self.config.request_timeout_seconds)
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError):
            log.info("IMDb suggestion request failed", exc_info=True)
            return []
        suggestions: list[dict[str, Any]] = []
        for item in data.get("d", []) if isinstance(data, dict) else []:
            imdb_id = item.get("id")
            if not isinstance(imdb_id, str) or not imdb_id.startswith("tt"):
                continue
            image = item.get("i", {}) if isinstance(item.get("i"), dict) else {}
            suggestions.append(
                {
                    "title": _safe_name(item.get("l", "")),
                    "year": item.get("y", "N/A"),
                    "poster": image.get("imageUrl", "") if isinstance(image.get("imageUrl", ""), str) else "",
                    "id": imdb_id,
                }
            )
            if len(suggestions) >= 10:
                break
        return suggestions

    def bitsearch(self, q: str, category: str, sort: str, order: str, page: int = 1) -> dict[str, Any]:
        page = max(1, int(page or 1))
        params = {"q": q, "sort": sort, "order": order, "page": page, "limit": 50}
        if category:
            params["category"] = category

        def request_payload() -> dict[str, Any]:
            response = self.http.get(
                self.config.bitsearch_url,
                params=params,
                headers={"User-Agent": "Streamly/1.0"},
                timeout=self.config.request_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}

        try:
            payload = request_payload()
        except (requests.RequestException, ValueError) as exc:
            if not _is_name_resolution_error(exc):
                log.warning("Bitsearch request failed: %s", exc)
                return {"results": [], "pagination": {"page": page, "perPage": 50, "total": 0, "totalPages": 1, "hasNext": False, "hasPrev": page > 1}, "took": None}
            ip = _resolve_bitsearch_via_doh(self.config.request_timeout_seconds)
            if not ip:
                log.warning("Bitsearch DNS failed and DoH fallback returned no usable IP: %s", exc)
                return {"results": [], "pagination": {"page": page, "perPage": 50, "total": 0, "totalPages": 1, "hasNext": False, "hasPrev": page > 1}, "took": None}
            try:
                with _temporary_bitsearch_resolution(ip):
                    payload = request_payload()
                log.info("Bitsearch request succeeded through scoped DNS fallback ip=%s", ip)
            except (requests.RequestException, ValueError) as retry_exc:
                log.warning("Bitsearch request failed after DNS fallback: %s", retry_exc)
                return {"results": [], "pagination": {"page": page, "perPage": 50, "total": 0, "totalPages": 1, "hasNext": False, "hasPrev": page > 1}, "took": None}

        raw_results = payload.get("results", []) if isinstance(payload, dict) else []
        raw_results = raw_results[:50] if isinstance(raw_results, list) else []
        pagination = payload.get("pagination", {}) if isinstance(payload.get("pagination", {}), dict) else {}

        def as_int(*values: Any, default: int = 0) -> int:
            for value in values:
                try:
                    if value is not None and value != "":
                        return int(value)
                except (TypeError, ValueError):
                    continue
            return default

        per_page = as_int(pagination.get("perPage"), pagination.get("limit"), payload.get("perPage"), payload.get("limit"), default=50)
        per_page = max(1, min(50, per_page))
        total = as_int(
            pagination.get("total"), pagination.get("totalResults"), pagination.get("count"),
            payload.get("total"), payload.get("totalResults"), payload.get("count"),
            default=len(raw_results),
        )
        total_pages = as_int(
            pagination.get("totalPages"), pagination.get("pages"),
            payload.get("totalPages"), payload.get("pages"),
            default=max(1, (total + per_page - 1) // per_page),
        )
        return {
            "results": raw_results,
            "pagination": {
                "page": as_int(pagination.get("page"), payload.get("page"), default=page),
                "perPage": per_page,
                "total": total,
                "totalPages": total_pages,
                "hasNext": bool(pagination.get("hasNext", page < total_pages)),
                "hasPrev": bool(pagination.get("hasPrev", page > 1)),
            },
            "took": payload.get("took"),
        }


def format_size(num_bytes: int) -> str:
    b = max(0, int(num_bytes))
    if b >= 1024**4: return f"{b / (1024**4):.2f} TB"
    if b >= 1024**3: return f"{b / (1024**3):.2f} GB"
    if b >= 1024**2: return f"{b / (1024**2):.1f} MB"
    if b >= 1024:    return f"{b / 1024:.1f} KB"
    return f"{b} B"
