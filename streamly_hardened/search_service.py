from __future__ import annotations

from contextlib import contextmanager
import ipaddress
import logging
import re
import socket
import threading
import time
from typing import Any
from urllib.parse import quote

import requests

from .config import AppConfig

log = logging.getLogger(__name__)


def _dedup_by_infohash(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicate torrents that share the same infohash.

    Keeps the representative with the highest seeder count. Order of first
    appearance is preserved. Items with a missing/blank infohash are never
    merged (each is kept as-is) so nothing is silently dropped.
    """
    def seeders_of(item: dict[str, Any]) -> int:
        # Raw bitsearch items use "seeders"; normalized UI rows use "seeds".
        val = item.get("seeders")
        if val is None:
            val = item.get("seeds", 0)
        try:
            return int(val or 0)
        except (TypeError, ValueError):
            return 0

    best: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    passthrough: list[dict[str, Any]] = []
    for item in items:
        h = str(item.get("infohash", "")).strip().lower()
        if not h:
            passthrough.append(item)
            continue
        if h not in best:
            best[h] = item
            order.append(h)
        elif seeders_of(item) > seeders_of(best[h]):
            best[h] = item
    return [best[h] for h in order] + passthrough


# --- Series Mode parsing & grouping -----------------------------------------

# SxxExx / sNeM (allows 1-2 digit season, 1-3 digit episode)
_SE_RE = re.compile(r"\bS(\d{1,2})E(\d{1,3})\b", re.IGNORECASE)
# Season pack markers: "S01.COMPLETE", "Season 2", "S02" with no episode token
_PACK_RE = re.compile(
    r"\b(?:S(\d{1,2})\.?COMPLETE|SEASON[\s._-]?(\d{1,2})|COMPLETE[\s._-]?SEASON)\b",
    re.IGNORECASE,
)
# Encoder: trailing "-GROUP" (optionally before a file ext / site tag) or "[GROUP]"
_ENCODER_DASH_RE = re.compile(r"-([A-Za-z0-9]{2,})(?:\[[^\]]*\])?(?:\.[a-z0-9]{2,4})?\s*$")
_ENCODER_BRACKET_RE = re.compile(r"\[([A-Za-z0-9][A-Za-z0-9 ._-]{1,})\]")
# Quality tokens
_RES_RE = re.compile(r"\b(2160p|1080p|720p|480p)\b", re.IGNORECASE)
_CODEC_RE = re.compile(r"\b(x265|x264|h\.?265|h\.?264|hevc|av1)\b", re.IGNORECASE)
_SOURCE_RE = re.compile(r"\b(web-?dl|web|bluray|bdrip|brrip|hdtv|dvdrip)\b", re.IGNORECASE)

# Site / tracker tags that should never be treated as the encoder group.
_SITE_TAGS = {
    "EZTV", "EZTVRE", "EZTVX", "TGX", "RARBG", "YTS", "YIFY", "ETTV",
    "MKV", "MP4", "AVI", "TO", "RE", "AG", "COM", "ETHD",
}


def _normalize_encoder(s: str) -> str:
    """Loose normalization: uppercase + strip non-alphanumeric. No fuzzy matching."""
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()


def _extract_quality(title: str) -> str:
    """Build a human-readable quality label, e.g. '1080p x265' or '1080p WEB-DL'."""
    parts: list[str] = []
    m = _RES_RE.search(title)
    if m:
        parts.append(m.group(1).lower())
    c = _CODEC_RE.search(title)
    if c:
        codec = c.group(1).lower().replace("h264", "h.264").replace("h265", "h.265")
        parts.append("x265" if codec == "hevc" else codec)
    else:
        ssrc = _SOURCE_RE.search(title)
        if ssrc:
            label = ssrc.group(1).upper().replace("WEBDL", "WEB-DL").replace("WEB-DL", "WEB-DL")
            parts.append(label)
    return " ".join(parts) if parts else "Unknown"


def _extract_encoder(title: str) -> str:
    """Best-effort release-group extraction. Returns '' if none found/usable."""
    base = re.sub(r"\.(mkv|mp4|avi|srt)\s*$", "", title, flags=re.IGNORECASE)
    m = _ENCODER_DASH_RE.search(base)
    if m:
        cand = m.group(1)
        if _normalize_encoder(cand) not in _SITE_TAGS and not cand.isdigit():
            return cand
    for b in _ENCODER_BRACKET_RE.findall(title):
        norm = _normalize_encoder(b)
        if norm and norm not in _SITE_TAGS and not norm.isdigit():
            return b.strip()
    return ""


def parse_release(title: str) -> dict[str, Any]:
    """Extract structured info from a release name.

    Returns dict with: series, season(int|None), episode(int|None),
    encoder(str), encoder_norm(str), quality(str), is_pack(bool), parsed(bool).
    `parsed` is False when no encoder+episode/pack could be reliably extracted,
    in which case the release belongs in the 'Other' bucket (never dropped).
    """
    title = title or ""
    se = _SE_RE.search(title)
    season = int(se.group(1)) if se else None
    episode = int(se.group(2)) if se else None

    is_pack = False
    if not se:
        pm = _PACK_RE.search(title)
        if pm:
            is_pack = True
            for g in pm.groups():
                if g:
                    season = int(g)
                    break

    encoder = _extract_encoder(title)
    encoder_norm = _normalize_encoder(encoder)
    quality = _extract_quality(title)

    series = title
    if se:
        series = title[: se.start()].strip(" .-_")
    elif is_pack:
        pm = _PACK_RE.search(title)
        if pm:
            series = title[: pm.start()].strip(" .-_")
    series = re.sub(r"^www\.[^ ]+\s*-\s*", "", series).strip(" .-_") or "Unknown"

    parsed = bool(encoder_norm) and (episode is not None or is_pack)
    return {
        "series": series,
        "season": season,
        "episode": episode,
        "encoder": encoder,
        "encoder_norm": encoder_norm,
        "quality": quality,
        "is_pack": is_pack,
        "parsed": parsed,
    }


def group_series_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Group normalized search rows into the Series Mode structure.

    Output:
      {
        "encoders": [ {name, encoder_norm, quality, episode_count,
                       seasons:[{season, episodes:[row,...]}, ...]}, ... ],
        "packs": [row, ...],
        "other": [row, ...],
        "stats": {raw, parsed, packs, other},
      }
    Each `row` is the original normalized row augmented with `se` (e.g. "S02E05").
    """
    raw = len(rows)
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    packs: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    parsed_count = 0

    for row in rows:
        info = parse_release(str(row.get("name", "")))
        enriched = dict(row)
        if info["season"] is not None and info["episode"] is not None:
            enriched["se"] = f"S{info['season']:02d}E{info['episode']:02d}"
        elif info["season"] is not None:
            enriched["se"] = f"S{info['season']:02d}"
        else:
            enriched["se"] = ""
        enriched["series"] = info["series"]

        if info["is_pack"]:
            packs.append(enriched)
            continue
        if not info["parsed"]:
            other.append(enriched)
            continue

        parsed_count += 1
        key = (info["encoder_norm"], info["quality"])
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "name": info["encoder"] or info["encoder_norm"],
                "encoder_norm": info["encoder_norm"],
                "quality": info["quality"],
                "_seasons": {},
            }
            buckets[key] = bucket
        season = info["season"] if info["season"] is not None else 0
        bucket["_seasons"].setdefault(season, []).append((info["episode"], enriched))

    encoders: list[dict[str, Any]] = []
    for bucket in buckets.values():
        seasons = []
        count = 0
        for season in sorted(bucket["_seasons"].keys()):
            eps = bucket["_seasons"][season]
            eps.sort(key=lambda t: (t[0] if t[0] is not None else 0))
            episodes = [r for _, r in eps]
            count += len(episodes)
            seasons.append({"season": season, "episodes": episodes})
        encoders.append({
            "name": bucket["name"],
            "encoder_norm": bucket["encoder_norm"],
            "quality": bucket["quality"],
            "episode_count": count,
            "seasons": seasons,
        })

    # Sort sections: encoder name A->Z, then quality desc (2160p before 1080p).
    encoders.sort(key=lambda e: (e["encoder_norm"], _quality_sort_key(e["quality"])))

    return {
        "encoders": encoders,
        "packs": packs,
        "other": other,
        "stats": {
            "raw": raw,
            "parsed": parsed_count,
            "packs": len(packs),
            "other": len(other),
        },
    }


def _quality_sort_key(quality: str) -> tuple[int, str]:
    """Higher resolution first (descending)."""
    order = {"2160p": 0, "1080p": 1, "720p": 2, "480p": 3}
    res = next((r for r in order if r in quality.lower()), None)
    return (order.get(res, 9), quality)


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

    def bitsearch(self, q: str, category: str, sort: str, order: str, page: int = 1, dedup: bool = True) -> dict[str, Any]:
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
        # Collapse same-infohash duplicates (keep highest-seeded). Applied to the
        # page's results only — pagination totals describe the upstream dataset
        # and are deliberately left untouched.
        if dedup:
            raw_results = _dedup_by_infohash(raw_results)
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
