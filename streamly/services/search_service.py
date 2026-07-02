from __future__ import annotations
import asyncio
import ipaddress
import logging
import re
import socket
import time
from datetime import datetime, timezone
from typing import Any, Tuple, Optional
from urllib.parse import quote

import httpx
from ..config import settings
from ..core.http_client import http_client

log = logging.getLogger(__name__)

def _dedup_by_infohash(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def seeders_of(item: dict[str, Any]) -> int:
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

# --- Regexes (kept from original) ---
_SE_RE = re.compile(r"\bS(\d{1,2})E(\d{1,3})\b", re.IGNORECASE)
_PACK_RE = re.compile(r"\b(?:S(\d{1,2})\.?COMPLETE|SEASON[\s._-]?(\d{1,2})|COMPLETE[\s._-]?SEASON)\b", re.IGNORECASE)
_RES_RE = re.compile(r"\b(2160p|1080p|720p|480p)\b", re.IGNORECASE)
_CODEC_RE = re.compile(r"\b(x265|x264|h\.?265|h\.?264|hevc|av1)\b", re.IGNORECASE)
_SOURCE_RE = re.compile(r"\b(web-?dl|web|bluray|bdrip|brrip|hdtv|dvdrip)\b", re.IGNORECASE)
_KNOWN_ENCODERS = {"PSA", "ELITE", "MEGUSTA", "TGX", "QXR", "TIGOLE", "SILENCE", "VYTO", "UTR", "GALAXYRG", "GZR", "YIFY", "PAHE", "NTB", "FLUX", "DON", "CTRLHD", "WIKI", "ECLIPSE", "SARTRE", "CAKES", "BOB", "KINGS", "TOMBDOC", "ION10", "MINX", "VENGEANCE", "NOGRP", "TEPES", "TAAP", "TELLY", "JYK", "FUM", "MSD", "RMTEAM", "AFG", "RBG", "SUNSCREEN", "IFT", "POED", "BETA", "SWTYBLZ", "ANONYMOUS", "BATMAN", "KARTZ", "SMURF", "GETI", "MKVCAGE", "MAXIMUS", "TERA", "TKO", "SHASHA", "BONE", "HEVCBAY", "SHIT2BIT", "RARBG", "YTS", "EZTV"}
_METADATA_EXCLUSIONS = {"2160P", "1080P", "720P", "480P", "4K", "2K", "UHD", "HD", "FHD", "SD", "10BIT", "8BIT", "HDR", "HDR10", "DV", "HDR10PLUS", "X265", "X264", "H265", "H264", "HEVC", "AV1", "XVID", "DIVX", "MP4", "MKV", "AVI", "TO", "RE", "AG", "COM", "ETHD", "WEBDL", "WEB", "WEBRIP", "BLURAY", "BDRIP", "BRRIP", "HDTV", "DVDRIP", "DVD", "REMUX", "HD", "SATRIP", "TVRIP", "DSNP", "NF", "AMZN", "HMAX", "NFLX", "AAC", "AC3", "DTS", "ATMOS", "TRUEHD", "EAC3", "DD51", "DD20", "DDP51", "DDP20", "MP3", "DDP5", "DD", "DDP", "SUB", "SUBS", "SUBBED", "DUB", "DUBBED", "MULTISUBS", "ENG", "ITA", "FRE", "SPA", "GER", "FRA", "JPN", "SEASON", "COMPLETE", "PACK", "EPISODE", "REPACK", "PROPER", "INTERNAL", "UNCUT", "EXTENDED", "RERIP", "BATCH", "TEMP", "EZTV", "EZTVRE", "EZTVX", "TGX", "RARBG", "YTS", "YIFY", "ETTV", "MKV", "MP4", "AVI", "TO", "RE", "AG", "COM", "ETHD", "GLODLS"}

def _normalize_encoder(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()

def _extract_quality(title: str) -> str:
    parts: list[str] = []
    m = _RES_RE.search(title)
    if m: parts.append(m.group(1).lower())
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
    if not title: return ""
    base = re.sub(r"\.(mkv|mp4|avi|srt|ts)\s*$", "", title, flags=re.IGNORECASE).strip(" .-_")
    bracket_match = re.search(r"[\[\(]([A-Za-z0-9 ._-]{2,15})[\]\)]\s*$", base)
    if bracket_match:
        cand = bracket_match.group(1).strip()
        norm = _normalize_encoder(cand)
        if norm and norm not in _METADATA_EXCLUSIONS and not norm.isdigit(): return cand
    tokens = [t for t in re.split(r"[\s._\-–—\[\]\(\)]+", base) if t]
    last_marker_idx = -1
    for i, t in enumerate(tokens):
        t_norm = _normalize_encoder(t)
        if re.match(r"^S\d{1,2}(E\d{1,3})?$", t_norm) or re.match(r"^E\d{1,3}$", t_norm) or t_norm in ("SEASON", "EPISODE", "COMPLETE"):
            last_marker_idx = i
    for t in reversed(tokens):
        norm = _normalize_encoder(t)
        if norm in _KNOWN_ENCODERS: return t
    dash_match = re.search(r"-([A-Za-z0-9]{2,15})\s*$", base)
    if dash_match:
        cand = dash_match.group(1)
        norm = _normalize_encoder(cand)
        if norm and norm not in _METADATA_EXCLUSIONS and not norm.isdigit(): return cand
    start_idx = last_marker_idx + 1 if last_marker_idx != -1 else 0
    candidate_tokens = tokens[start_idx:]
    for t in reversed(candidate_tokens):
        norm = _normalize_encoder(t)
        if not norm or norm in _METADATA_EXCLUSIONS or norm.isdigit() or re.match(r"^S\d{1,2}(E\d{1,3})?$", norm) or re.match(r"^E\d{1,3}$", norm):
            continue
        return t
    return ""

def _norm_tokens(s: str) -> list[str]:
    return [t for t in re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split() if t]

def series_key(series: str) -> str:
    return " ".join(_norm_tokens(series))

def matches_query(query: str, series: str, *, is_episode: bool = False) -> bool:
    q = _clean_query_tokens(query)
    if not q: return True
    s = _clean_query_tokens(series)
    if is_episode: return s == q
    return s[:len(q)] == q

_QUERY_META = {"2160p", "1080p", "720p", "480p", "4k", "uhd", "hd", "x265", "x264", "h265", "h264", "hevc", "av1", "xvid", "divx", "10bit", "8bit", "hdr", "hdr10", "dv", "web", "webdl", "webrip", "bluray", "bdrip", "brrip", "hdtv", "dvdrip", "dvd", "remux", "aac", "ac3", "dts", "atmos", "truehd", "eac3", "season", "complete", "pack", "episode", "repack", "proper", "internal", "elite", "psa", "megusta", "rarbg", "yts", "yify", "eztv", "tgx", "ettv"}

def _clean_query_tokens(query: str) -> list[str]:
    out: list[str] = []
    for t in _norm_tokens(query):
        if t in _QUERY_META or re.fullmatch(r"s\d{1,2}(e\d{1,3})?", t) or re.fullmatch(r"e\d{1,3}", t) or re.fullmatch(r"(19|20)\d{2}", t):
            continue
        out.append(t)
    if len(out) > 1 and out[-1] in {"us", "uk", "ca", "au", "nz"}:
        out.pop()
    return out

def parse_release(title: str) -> dict[str, Any]:
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
    if not encoder_norm:
        encoder = "Other Encoders"
        encoder_norm = "OTHERENCODERS"
    quality = _extract_quality(title)
    series = title
    if se: series = title[: se.start()].strip(" .-_")
    elif is_pack:
        pm = _PACK_RE.search(title)
        if pm: series = title[: pm.start()].strip(" .-_")
    series = re.sub(r"^www\.[^ ]+\s*-\s*", "", series).strip(" .-_") or "Unknown"
    parsed = (episode is not None or is_pack)
    return {"series": series, "season": season, "episode": episode, "encoder": encoder, "encoder_norm": encoder_norm, "quality": quality, "is_pack": is_pack, "parsed": parsed}

def build_packs(rows: list[dict[str, Any]], top_n: int = 20) -> list[dict[str, Any]]:
    def seeds_of(r: dict[str, Any]) -> int:
        try: return int(r.get("seeds", 0) or 0)
        except (TypeError, ValueError): return 0
    best: dict[tuple[str, Any, str], dict[str, Any]] = {}
    for row in rows:
        name = str(row.get("name", ""))
        info = parse_release(name)
        if not info["is_pack"]: continue
        enriched = dict(row)
        enriched["se"] = f"S{info['season']:02d}" if info["season"] is not None else "Season Pack"
        enriched["series"] = info["series"]
        dkey = (series_key(info["series"]), info["season"], _quality_bucket(name))
        prev = best.get(dkey)
        if prev is None or seeds_of(enriched) > seeds_of(prev):
            best[dkey] = enriched
    packs = list(best.values())
    packs.sort(key=lambda r: r.get("size_bytes", 0) or 0)
    return packs[:top_n]

def _quality_bucket(title: str) -> str:
    m = _RES_RE.search(title or "")
    if m:
        res = m.group(1).lower()
        if res in ("2160p", "1080p", "720p"): return res
    return "Other"

def group_by_quality(rows: list[dict[str, Any]], only_qualities: list[str] | None = None, cap: int | None = None) -> list[dict[str, Any]]:
    order = ["2160p", "1080p", "720p", "Other"]
    label = {"2160p": "4K", "1080p": "1080p", "720p": "720p", "Other": "Other"}
    wanted = {x for x in (only_qualities or []) if x in order}
    emit = [k for k in order if k in wanted] if wanted else order
    buckets: dict[str, list[dict[str, Any]]] = {k: [] for k in order}
    for row in rows: buckets[_quality_bucket(str(row.get("name", " a")))].append(row)
    sections = []
    for key in emit:
        items = buckets[key]
        if not items: continue
        if cap is not None:
            items.sort(key=lambda r: r.get("seeds", 0) or 0, reverse=True)
            items = items[:cap]
        items.sort(key=lambda r: r.get("size_bytes", 0) or 0)
        sections.append({"quality": key, "label": label[key], "count": len(items), "rows": items})
    return sections

def group_series_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    _Q_ORDER = ["2160p", "1080p", "720p", "Other"]
    _Q_LABEL = {"2160p": "4K", "1080p": "1080p", "720p": "720p", "Other": "Other"}
    raw = len(rows)
    encs: dict[str, dict[str, Any]] = {}
    parsed_count, other_discarded = 0, 0
    def seeds_of(r: dict[str, Any]) -> int:
        try: return int(r.get("seeds", 0) or 0)
        except (TypeError, ValueError): return 0
    for row in rows:
        info = parse_release(str(row.get("name", "")))
        if info["is_pack"] or not info["parsed"]:
            other_discarded += 1
            continue
        enriched = dict(row)
        if info["season"] is not None and info["episode"] is not None: enriched["se"] = f"S{info['season']:02d}E{info['episode']:02d}"
        elif info["season"] is not None: enriched["se"] = f"S{info['season']:02d}"
        else: enriched["se"] = ""
        enriched["series"] = info["series"]
        qbucket = _quality_bucket(str(row.get("name", "")))
        season = info["season"] if info["season"] is not None else 0
        episode = info["episode"]
        enc = encs.setdefault(info["encoder_norm"], {"name": info["encoder"] or info["encoder_norm"], "encoder_norm": info["encoder_norm"], "_qualities": {}})
        if info["encoder"] and enc["name"] == enc["encoder_norm"]: enc["name"] = info["encoder"]
        quality = enc["_qualities"].setdefault(qbucket, {})
        dkey = (series_key(info["series"]), enriched["se"] or str(id(enriched)))
        prev = quality.get(dkey)
        if prev is None or seeds_of(enriched) > seeds_of(prev[2]):
            quality[dkey] = (season, episode, enriched)
        parsed_count += 1
    encoders: list[dict[str, Any]] = []
    for enc in encs.values():
        qualities = []
        enc_count = 0
        for qkey in _Q_ORDER:
            qmap = enc["_qualities"].get(qkey)
            if not qmap: continue
            seasons_map: dict[int, list[tuple[Any, dict[str, Any]]]] = {}
            for (season, episode, r) in qmap.values():
                seasons_map.setdefault(season, []).append((episode, r))
            seasons = []
            q_count = 0
            for season in sorted(seasons_map.keys()):
                eps = seasons_map[season]
                eps.sort(key=lambda t: (t[0] if t[0] is not None else 0))
                seasons.append({"season": season, "episodes": [r for _, r in eps]})
                q_count += len(eps)
            qualities.append({"quality": qkey, "label": _Q_LABEL[qkey], "episode_count": q_count, "seasons": seasons})
            enc_count += q_count
        encoders.append({"name": enc["name"], "encoder_norm": enc["encoder_norm"], "episode_count": enc_count, "qualities": qualities})
    encoders.sort(key=lambda e: e["encoder_norm"])
    return {"encoders": encoders, "stats": {"raw": raw, "parsed": parsed_count, "other_discarded": other_discarded}}

_BITSEARCH_DNS_LOCK = asyncio.Lock()
_BITSEARCH_IP_CACHE: tuple[str, float] | None = None

async def _resolve_bitsearch_via_doh(timeout: float) -> str | None:
    global _BITSEARCH_IP_CACHE
    now = time.monotonic()
    if _BITSEARCH_IP_CACHE and _BITSEARCH_IP_CACHE[1] > now:
        return _BITSEARCH_IP_CACHE[0]
    try:
        resp = await http_client.get("https://1.1.1.1/dns-query", params={"name": "bitsearch.eu", "type": "A"}, headers={"accept": "application/dns-json"})
        data = resp.json()
        for answer in data.get("Answer", []) if isinstance(data, dict) else []:
            candidate = answer.get("data")
            try:
                ip = ipaddress.ip_address(candidate)
                if ip.version == 4 and not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast):
                    _BITSEARCH_IP_CACHE = (str(ip), now + 300)
                    return str(ip)
            except ValueError: continue
    except Exception as exc:
        log.warning("Cloudflare DoH fallback for bitsearch.eu failed: %s", exc)
    return None

async def _temporary_bitsearch_resolution(ip: str):
    old_getaddrinfo = socket.getaddrinfo
    def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host == "bitsearch.eu": return old_getaddrinfo(ip, port, family or socket.AF_INET, type, proto, flags)
        return old_getaddrinfo(host, port, family, type, proto, flags)
    async with _BITSEARCH_DNS_LOCK:
        socket.getaddrinfo = patched_getaddrinfo
        try: yield
        finally: socket.getaddrinfo = old_getaddrinfo

def _to_int(*values: Any, default: int = 0) -> int:
    for value in values:
        try:
            if value is not None and value != "": return int(value)
        except (TypeError, ValueError): continue
    return default

def _unix_to_date(value: Any) -> str:
    ts = _to_int(value, default=0)
    if ts <= 0: return ""
    try: return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError): return ""

def _make_row(*, name: str, infohash: str, seeds: Any, leeches: Any, size_bytes: Any, date: str, source: str) -> dict[str, Any] | None:
    infohash = str(infohash or "").strip()[:128]
    name = str(name or "").strip()[:512]
    if not infohash or not name: return None
    size_b = max(0, _to_int(size_bytes, default=0))
    enc = _extract_encoder(name)
    return {"name": name, "size": _format_bytes(size_b), "size_bytes": size_b, "seeds": _to_int(seeds, default=0), "leeches": _to_int(leeches, default=0), "date": str(date or "")[:32], "category": "Other", "encoder": enc, "encoder_norm": _normalize_encoder(enc), "magnet": f"magnet:?xt=urn:btih:{infohash}&dn={name}", "infohash": infohash, "source": source}

def _format_bytes(num: int) -> str:
    n = float(max(0, num))
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB": return (f"{int(n)} {unit}" if unit == "B" else f"{n:.2f} {unit}")
        n /= 1024
    return f"{n:.2f} PB"

async def _fetch_apibay(q: str, timeout: float) -> list[dict[str, Any]]:
    try:
        resp = await http_client.get("https://apibay.org/q.php", params={"q": q, "cat": "0"}, headers={"User-Agent": "Streamly/1.0"})
        data = resp.json()
        if not isinstance(data, list): return []
        rows: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict): continue
            ih = item.get("info_hash", "")
            if str(ih).strip("0") == "" or str(item.get("name", "")) == "No results returned": continue
            row = _make_row(name=item.get("name", ""), infohash=ih, seeds=item.get("seeders"), leeches=item.get("leechers"), size_bytes=item.get("size"), date=_unix_to_date(item.get("added")), source="apibay")
            if row: rows.append(row)
        return rows
    except Exception: return []

async def _fetch_torrents_csv(q: str, timeout: float) -> list[dict[str, Any]]:
    try:
        resp = await http_client.get("https://torrents-csv.com/service/search", params={"q": q, "size": 50}, headers={"User-Agent": "Streamly/1.0"})
        data = resp.json()
        items = data.get("torrents", []) if isinstance(data, dict) else []
        rows: list[dict[str, Any]] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict): continue
            row = _make_row(name=item.get("name", ""), infohash=item.get("infohash", ""), seeds=item.get("seeders"), leeches=item.get("leechers"), size_bytes=item.get("size_bytes"), date=_unix_to_date(item.get("created_unix")), source="torrents-csv")
            if row: rows.append(row)
        return rows
    except Exception: return []

class SearchService:
    def __init__(self, config):
        self.config = config

    async def imdb_suggestions(self, q: str) -> list[dict[str, Any]]:
        url = self.config.imdb_suggest_template.format(query=quote(q.lower(), safe=""))
        try:
            resp = await http_client.get(url)
            data = resp.json()
            suggestions: list[dict[str, Any]] = []
            for item in data.get("d", []) if isinstance(data, dict) else []:
                imdb_id = item.get("id")
                if not isinstance(imdb_id, str) or not imdb_id.startswith("tt"): continue
                image = item.get("i", {}) if isinstance(item.get("i"), dict) else {}
                suggestions.append({"title": _safe_name_local(item.get("l", "")), "year": item.get("y", "N/A"), "poster": image.get("imageUrl", "") if isinstance(image.get("imageUrl", ""), str) else "", "id": imdb_id})
                if len(suggestions) >= 5: break
            return suggestions
        except Exception: return []

    async def bitsearch(self, q: str, category: str, sort: str, order: str, page: int = 1, dedup: bool = True) -> dict[str, Any]:
        page = max(1, int(page or 1))
        params = {"q": q, "sort": sort, "order": order, "page": page, "limit": 50}
        if category: params["category"] = category
        try:
            payload = await http_client.get(self.config.bitsearch_url, params=params, headers={"User-Agent": "Streamly/1.0"}).json()
        except Exception as exc:
            ip = await _resolve_bitsearch_via_doh(self.config.request_timeout_seconds)
            if not ip: return {"results": [], "pagination": {"page": page, "perPage": 50, "total": 0, "totalPages": 1, "hasNext": False, "hasPrev": page > 1}, "took": None}
            try:
                async with _temporary_bitsearch_resolution(ip):
                    payload = await http_client.get(self.config.bitsearch_url, params=params, headers={"User-Agent": "Streamly/1.0"}).json()
            except Exception:
                return {"results": [], "pagination": {"page": page, "perPage": 50, "total": 0, "totalPages": 1, "hasNext": False, "hasPrev": page > 1}, "took": None}
        
        raw_results = payload.get("results", []) if isinstance(payload, dict) else []
        if dedup: raw_results = _dedup_by_infohash(raw_results)
        pagination = payload.get("pagination", {}) if isinstance(payload, dict) else {}
        per_page = _to_int(pagination.get("perPage"), payload.get("perPage"), default=50)
        total = _to_int(pagination.get("total"), payload.get("totalResults"), payload.get("count"), default=len(raw_results))
        total_pages = _to_int(pagination.get("totalPages"), payload.get("pages"), default=max(1, (total + per_page - 1) // per_page))
        return {"results": raw_results, "pagination": {"page": _to_int(pagination.get("page"), payload.get("page"), default=page), "perPage": per_page, "total": total, "totalPages": total_pages, "hasNext": bool(pagination.get("hasNext", page < total_pages)), "hasPrev": bool(pagination.get("hasPrev", page > 1))}, "took": payload.get("took")}

    async def _bitsearch_rows(self, q: str) -> list[dict[str, Any]]:
        payload = await self.bitsearch(q, "", "relevance", "desc", 1, dedup=False)
        results = payload.get("results", []) if isinstance(payload, dict) else []
        rows: list[dict[str, Any]] = []
        for item in results if isinstance(results, list) else []:
            if not isinstance(item, dict): continue
            row = _make_row(name=item.get("title", ""), infohash=item.get("infohash", ""), seeds=item.get("seeders"), leeches=item.get("leecher"), size_bytes=item.get("size"), date=str(item.get("createdAt", "")).split("T")[0], source="bitsearch")
            if row: rows.append(row)
        return rows

    async def _run_provider(self, name: str, q: str) -> list[dict[str, Any]]:
        try:
            if name == "apibay": return await _fetch_apibay(q, self.config.request_timeout_seconds)
            if name == "torrents-csv": return await _fetch_torrents_csv(q, self.config.request_timeout_seconds)
            if name == "bitsearch": return await self._bitsearch_rows(q)
        except Exception: pass
        return []

    def _provider_order(self, prefer: str | None = None, *, strict_prefer: bool = False, order_override: tuple[str, ...] | list[str] | None = None) -> list[str]:
        configured = tuple(order_override or self.config.search_providers)
        if prefer and prefer in configured and strict_prefer: return [prefer]
        order: list[str] = []
        if prefer and prefer in configured: order.append(prefer)
        for name in configured:
            if name not in order: order.append(name)
        return order

    async def multi_search_filtered(self, q: str, filter_fn, prefer: str | None = None, *, strict_prefer: bool = False, allow_raw_fallback: bool = True, order_override: tuple[str, ...] | list[str] | None = None) -> tuple[list[dict[str, Any]], str | None, list[dict[str, Any]], str | None]:
        attempts: list[dict[str, Any]] = []
        raw_fallback_rows, raw_fallback_provider = [], None
        for name in self._provider_order(prefer, strict_prefer=strict_prefer, order_override=order_override):
            raw_rows = _dedup_by_infohash(await self._run_provider(name, q))
            filtered_rows = [r for r in raw_rows if filter_fn(r)]
            attempts.append({"provider": name, "raw": len(raw_rows), "filtered": len(filtered_rows)})
            if raw_rows and raw_fallback_provider is None:
                raw_fallback_rows, raw_fallback_provider = raw_rows, name
            if filtered_rows: return filtered_rows, name, attempts, None
        if allow_raw_fallback and raw_fallback_provider is not None:
            return raw_fallback_rows, raw_fallback_provider, attempts, "unfiltered"
        return [], None, attempts, None

def _safe_name_local(value: Any) -> str:
    if not isinstance(value, str): value = str(value or "")
    return "".join(ch for ch in value if ch >= " " and ch != "\x7f")[:512]
