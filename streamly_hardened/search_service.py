from __future__ import annotations

from contextlib import contextmanager
import ipaddress
import logging
import socket
import threading
import time
from typing import Any
from urllib.parse import quote

import requests

from .config import AppConfig

log = logging.getLogger(__name__)


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
                    "title": _safe_name_local(item.get("l", "")),
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

def _safe_name_local(value: Any) -> str:
    if not isinstance(value, str):
        value = str(value or "")
    return "".join(ch for ch in value if ch >= " " and ch != "\x7f")[:512]
