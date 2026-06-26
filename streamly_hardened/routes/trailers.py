from __future__ import annotations

import json as _json
import logging
import os
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from flask import Blueprint, current_app, jsonify

from ..security import csrf_required, json_error, rate_limited

log = logging.getLogger(__name__)

trailers_bp = Blueprint("trailers", __name__)

_YOUTUBE_RSS_PROXY = "https://streamly-youtube-proxy.lucidesh.workers.dev"

_CRAWL_INTERVAL_SECONDS = 86400
_CRAWL_LOCK_TTL = 600
_FEED_TTL_SECONDS = 172800
_STALE_HOURS = 24  # Trigger auto-crawl if feed is older than 24 hours

_BLOCKED_KEYWORDS = {
    "gameplay", "story trailer", "game", "behind the scenes", "bts",
    "interview", "vlog", "reaction", "review", "unboxing", "lego",
    "funko", "toy", "merchandise", "comic con", "panel", "clip",
    "featurette", "tv spot", "tv spots", "making of", "b-roll",
    "exclusive look", "extended look", "first look", "announcement",
}

_TITLE_HEURISTICS = {
    "gameplay", "vlog", "reaction", "review", "unboxing", "lego", "funko",
    "toy", "merchandise", "comic con", "bts", "behind the scenes",
}

# Shorts detection patterns (title-based, case-insensitive)
_SHORTS_PATTERNS = [
    '#shorts', '#short', '#shortsfeed', '#ytshorts', '#youtubeshorts',
    '#reels', '#tiktok', '#vertical',
]

def _get_proxy_health(rs) -> str:
    if not rs:
        return "unknown"
    val = rs.get("streamly:trailers:proxy_health")
    return val.decode() if isinstance(val, bytes) else (val or "unknown")


def _update_trailer_window(rs, new_entries: list[dict]) -> None:
    """Updates the Redis sorted set trailer window: removes old ones, adds new ones."""
    now = datetime.now(timezone.utc)
    cutoff_time = now - timedelta(days=30)
    cutoff_ts = cutoff_time.timestamp()

    # Evict trailers older than 30 days
    rs._execute("ZREMRANGEBYSCORE", "streamly:trailers:window", "-inf", str(cutoff_ts))

    # Add new qualifying videos
    if new_entries:
        zadd_args = []
        for e in new_entries:
            try:
                # Calculate published timestamp as score
                pub_str = e["published"]
                pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                score = pub_dt.timestamp()

                if score < cutoff_ts:
                    continue

                member = _json.dumps(e)
                zadd_args.extend([str(score), member])
            except Exception as ex:
                log.warning("Skipping entry in window ZADD: %s", ex)

        if zadd_args:
            rs._execute("ZADD", "streamly:trailers:window", *zadd_args)


def get_current_trailer_window() -> list[dict]:
    """Returns the current list of trailers in the window (most recent first)."""
    rs = None
    try:
        from flask import current_app
        rs = getattr(current_app, "rs", None)
    except Exception:
        pass
    if not rs:
        return []

    # Get all members sorted by score descending (most recent first)
    members = rs._execute("ZREVRANGEBYSCORE", "streamly:trailers:window", "+inf", "-inf")

    results = []
    if members:
        for m in members:
            try:
                if isinstance(m, bytes):
                    m = m.decode("utf-8")
                results.append(_json.loads(m))
            except Exception as e:
                log.warning("Failed to parse member from trailer window: %s", e)
    return results


def _is_shorts_by_title(title: str) -> bool:
    t = title.lower()
    # Check for hashtag patterns
    if any(p in t for p in _SHORTS_PATTERNS):
        return True
    # Check for standalone word "shorts" (e.g., "funny shorts", "movie shorts")
    if re.search(r'(^|\s)#?shorts($|\s)', t):
        return True
    # Check for standalone word "short" at end (e.g., "a short")
    if re.search(r'\bshort\b', t) and 'trailer' not in t and 'teaser' not in t:
        return True
    return False

_CHANNEL_PRIORITY = {
    "Marvel Entertainment": 0, "Warner Bros": 0, "Sony Pictures": 0,
    "Universal Pictures": 0, "Paramount Pictures": 0, "Netflix": 0,
    "Disney": 0, "A24": 0, "Amazon MGM Studios": 0, "HBO": 0,
    "Searchlight Pictures": 0, "Lionsgate Movies": 0, "YRF": 0,
    "Dharma Productions": 0, "T-Series": 0, "Zee Studios": 0,
    "Red Chillies Entertainment": 0, "Excel Entertainment": 0,
    "Pen Movies": 0, "AA Films": 0, "Geetha Arts": 0,
    "Mythri Movie Makers": 0, "Sithara Entertainments": 0,
    "Sun Pictures": 0, "Lyca Productions": 0, "Hombale Films": 0,
    "Vyjayanthi Movies": 0, "Prithviraj Productions": 0,
    "ONE Media": 1, "Movieclips Trailers": 1, "Rotten Tomatoes Trailers": 1,
    "Bollywood Hungama": 1, "Telugu Filmnagar": 1, "123 Telugu": 1,
    "Goldmines Telefilms": 1,
}

TRAILER_CHANNELS = [
    ("UCjmJDM5pRKbUlVIzDYYWb6g", "Warner Bros"),
    ("UCvC4D8onUfXzvjTOM-dBfEA", "Marvel Entertainment"),
    ("UCz97F7dMxBNOfGYu3rx8aCw", "Sony Pictures"),
    ("UCq0OueAsdxH6b8nyAspwViw", "Universal Pictures"),
    ("UCF9imwPMSGz4Vq1NiTWCC7g", "Paramount Pictures"),
    ("UCWOA1ZGywLbqmigxE4Qlvuw", "Netflix"),
    ("UC_976xMxPgzIa290Hqtk-9g", "Disney"),
    ("UCwYzZs_hwA6NdaQp6Hjhe5w", "ONE Media"),
    ("UC3gNmTGu-TTbFPpfSs5kNkg", "Movieclips Trailers"),
    ("UCuPivVjnfNo4mb3Oog_frZg", "A24"),
    ("UCf5CjDJvsFvtVIhkfmKAwAA", "Amazon MGM Studios"),
    ("UCVTQuK2CaWaTgSsoNkn5AiQ", "HBO"),
    ("UCor9rW6PgxSQ9vUPWQdnaYQ", "Searchlight Pictures"),
    ("UCJ6nMHaJPZvsJ-HmUmj1SeA", "Lionsgate Movies"),
    ("UCbTLwN10NoCU4WDzLf1JMOA", "YRF"),
    ("UCGdHCtXEzkCB7-g_NXYZPcw", "Dharma Productions"),
    ("UCq-Fj5jknLsUf-MWSy4_brA", "T-Series"),
    ("UC3jMepkLKF8y4iiwWmAB3RA", "Zee Studios"),
    ("UCjJKg01HAP01xCLVhDmnLhw", "Red Chillies Entertainment"),
    ("UCn9BuiRZGR_tPM2GGT4jN-w", "Excel Entertainment"),
    ("UC3ar28GS6o1p0m_wabfk2zw", "Pen Movies"),
    ("UCdfXaARoko58ZraSegLA-4A", "AA Films"),
    ("UCiJfiEg1FImWsVuEu0L8X6Q", "Geetha Arts"),
    ("UCKZSn5C-RzrLjuWJF8wWiDw", "Mythri Movie Makers"),
    ("UC2woPAI_KMAR25R_oezEQqw", "Sithara Entertainments"),
    ("UCo2r1S9iXJshkeV9v_ZDicw", "Sun Pictures"),
    ("UCA7gwgLgmCZ8DSmdf2bhb8g", "Lyca Productions"),
    ("UCarJoVXH0T2pdtcHBu9J8Bw", "Hombale Films"),
    ("UCdj3E_o7ONZ9_6EEN3Mj6YQ", "Vyjayanthi Movies"),
    ("UCH1Gszpy-NmA6ZXZaxhnlwA", "Prithviraj Productions"),
]


def _rss_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.youtube.com/",
    }


def _fetch_rss(url: str, proxy_url: str | None = None, timeout: float = 15.0) -> requests.Response:
    """Fetch RSS. Prefer the dedicated YouTube proxy first, then direct, then fallback proxy."""
    rs = None
    try:
        from flask import current_app
        rs = getattr(current_app, "rs", None)
    except Exception:
        pass

    # 1. Try dedicated YouTube RSS proxy first (Proxy-First for YouTube)
    if _YOUTUBE_RSS_PROXY:
        proxied_yt = f"{_YOUTUBE_RSS_PROXY.rstrip('/')}/?url={urllib.parse.quote(url, safe='')}&referer={urllib.parse.quote('https://www.youtube.com/feed', safe='')}"
        try:
            r = requests.get(proxied_yt, timeout=timeout, headers=_rss_headers())
            if r.status_code == 200:
                if rs:
                    rs.set("streamly:trailers:proxy_health", "proxy_ok", ex=3600)
                log.info("RSS via YouTube proxy for %s -> HTTP %d", url, r.status_code)
                return r
            else:
                if rs:
                    rs.set("streamly:trailers:proxy_health", "proxy_bad", ex=1800)
        except Exception as e:
            log.warning("YouTube proxy RSS fetch failed for %s: %s", url, e)
            if rs:
                rs.set("streamly:trailers:proxy_health", "proxy_down", ex=1800)

    # 2. Fallback to direct fetch
    try:
        r = requests.get(url, timeout=timeout, headers=_rss_headers())
        if r.status_code == 200:
            if rs:
                rs.set("streamly:trailers:proxy_health", "direct_ok", ex=3600)
            return r
    except Exception as e:
        log.debug("Direct RSS fetch fallback failed for %s: %s", url, e)

    # 3. Fallback to general Cloudflare Worker proxy (the existing proxy_url)
    if proxy_url:
        proxied = f"{proxy_url.rstrip('/')}/?url={urllib.parse.quote(url, safe='')}&referer={urllib.parse.quote('https://www.youtube.com/feed', safe='')}"
        try:
            r = requests.get(proxied, timeout=timeout, headers=_rss_headers())
            if r.status_code == 200:
                if rs:
                    rs.set("streamly:trailers:proxy_health", "proxy_ok", ex=3600)
                log.info("RSS via general proxy fallback for %s -> HTTP %d", url, r.status_code)
                return r
            else:
                if rs:
                    rs.set("streamly:trailers:proxy_health", "proxy_bad", ex=1800)
        except Exception as e:
            log.warning("General proxy fallback RSS fetch failed for %s: %s", url, e)
            if rs:
                rs.set("streamly:trailers:proxy_health", "proxy_down", ex=1800)

    # 4. Final direct attempt
    try:
        return requests.get(url, timeout=timeout, headers=_rss_headers())
    except Exception as e:
        log.warning("RSS fetch failed completely for %s: %s", url, e)
        raise


def _normalize_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\b(part|chapter|volume|episode)\s+\d+\b", r"\1", t)
    return t.strip()


def _extract_trailer_info(title: str) -> tuple[str, str, int]:
    t = title.lower()
    media_type = "teaser" if "teaser" in t else "trailer"
    num_match = re.search(r"\b(?:trailer|teaser)\s*(\d+)\b", t)
    num = int(num_match.group(1)) if num_match else 0

    parts = re.split(
        r"(\||–|-)\s*(official|final|teaser|trailer|exclusive|extended|first|hindi|tamil|telugu|malayalam|kannada)",
        t,
    )
    name = parts[0] if parts else t
    name = re.sub(
        r"\b(hindi|tamil|telugu|malayalam|kannada|english)\s*(trailer|teaser|dubbed)?\b",
        "",
        name,
    )
    name = _normalize_title(name)
    return name, media_type, num


def _channel_priority(name: str) -> int:
    return _CHANNEL_PRIORITY.get(name, 99)


def _parse_rss_incremental(xml_text: str, channel_name: str, last_seen: str | None) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("RSS parse error for %s: %s", channel_name, e)
        return []

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "media": "http://search.yahoo.com/mrss/",
    }

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    results: list[dict] = []

    for entry in root.findall("atom:entry", ns):
        title_elem = entry.find("atom:title", ns)
        if title_elem is None or title_elem.text is None:
            continue
        title = title_elem.text.strip()
        title_lower = title.lower()

        if any(bw in title_lower for bw in _BLOCKED_KEYWORDS):
            continue
        if "trailer" not in title_lower and "teaser" not in title_lower:
            continue

        # Filter out YouTube Shorts by title
        if _is_shorts_by_title(title):
            continue

        if any(h in title_lower for h in _TITLE_HEURISTICS):
            continue
        if len(title) < 8 or len(title) > 120:
            continue

        published_elem = entry.find("atom:published", ns)
        if published_elem is None or published_elem.text is None:
            continue
        published = published_elem.text

        try:
            pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            continue

        if pub_dt < cutoff:
            continue

        if last_seen and published <= last_seen:
            continue

        id_elem = entry.find("atom:id", ns)
        if id_elem is None or id_elem.text is None:
            continue
        vid = id_elem.text.split(":")[-1]

        thumb = entry.find("media:group/media:thumbnail", ns)
        thumb_url = None
        if thumb is not None:
            thumb_url = thumb.get("url")
        if not thumb_url:
            thumb_url = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"

        norm_name, media_type, num = _extract_trailer_info(title)

        results.append(
            {
                "title": title,
                "normalized": norm_name,
                "type": media_type,
                "number": num,
                "id": vid,
                "channel": channel_name,
                "published": published,
                "thumbnail": thumb_url,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "priority": _channel_priority(channel_name),
            }
        )

    return results


def _build_digest(entries: list[dict]) -> dict:
    best: dict[tuple[str, str, str, int], dict] = {}
    for e in entries:
        date = e["published"][:10]
        key = (date, e["normalized"], e["type"], e["number"])
        if key in best:
            if e["priority"] < best[key]["priority"]:
                best[key] = e
            elif e["priority"] == best[key]["priority"] and e["published"] > best[key]["published"]:
                best[key] = e
        else:
            best[key] = e

    by_date: dict[str, dict[str, dict]] = {}
    for key, e in best.items():
        date, norm, _, _ = key
        if date not in by_date:
            by_date[date] = {}
        if norm not in by_date[date]:
            by_date[date][norm] = {
                "title": e["title"],
                "normalized": norm,
                "category": "movie",
                "videos": [],
            }
        by_date[date][norm]["videos"].append(
            {
                "type": e["type"],
                "number": e["number"],
                "id": e["id"],
                "channel": e["channel"],
                "published": e["published"],
                "thumbnail": e["thumbnail"],
                "url": e["url"],
            }
        )

    items = []
    for date in sorted(by_date.keys(), reverse=True):
        day_items = []
        for norm in sorted(by_date[date].keys()):
            item = by_date[date][norm]
            item["videos"].sort(
                key=lambda v: (
                    v["type"] != "teaser",
                    v["type"] != "trailer",
                    v["number"],
                )
            )
            day_items.append(item)
        items.append({"date": date, "items": day_items})

    return {"items": items}


def _crawl_trailers_incremental(app) -> None:
    rs = getattr(app, "rs", None)
    if not rs:
        log.warning("Trailer crawl skipped: Redis unavailable")
        return

    lock = rs._execute("SET", "streamly:trailers:crawl_lock", "1", "EX", str(_CRAWL_LOCK_TTL), "NX")
    if lock != "OK":
        return

    try:
        proxy_url = rs.get("streamly:cloudflare_worker_proxy")
        if proxy_url:
            if isinstance(proxy_url, bytes):
                proxy_url = proxy_url.decode("utf-8")
            proxy_url = proxy_url.strip()
        if not proxy_url:
            proxy_url = app.config.get("CLOUDFLARE_WORKER_PROXY") or os.getenv("CLOUDFLARE_WORKER_PROXY", "")
            if proxy_url:
                proxy_url = proxy_url.strip()
        if not proxy_url:
            proxy_url = "https://streamly-proxy.lucidesh.workers.dev"

        # Load existing window and populate known_keys
        with app.app_context():
            current_window = get_current_trailer_window()

        known_keys: set[tuple[str, str]] = set()
        for e in current_window:
            known_keys.add((e["id"], e["published"]))

        channels = [c for c in TRAILER_CHANNELS if c[0] and c[0] != "???"]
        all_new_entries: list[dict] = []
        total_channels_checked = 0
        total_channels_with_new = 0

        def fetch_one(channel_id: str, channel_name: str) -> tuple[list[dict] | None, str]:
            try:
                rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
                r = _fetch_rss(rss_url, proxy_url=proxy_url, timeout=15.0)
                if r.status_code != 200:
                    log.warning("RSS HTTP %d for %s", r.status_code, channel_name)
                    return None, channel_name

                last_seen = rs.get(f"streamly:trailers:last_seen:{channel_id}")
                entries = _parse_rss_incremental(r.text, channel_name, last_seen)
                return entries, channel_name
            except Exception as e:
                log.warning("RSS fetch failed for %s: %s", channel_name, e)
                return None, channel_name

        batch_size = 5
        for batch_start in range(0, len(channels), batch_size):
            batch = channels[batch_start:batch_start + batch_size]
            with ThreadPoolExecutor(max_workers=batch_size) as executor:
                futures = {
                    executor.submit(fetch_one, cid, name): (cid, name)
                    for cid, name in batch
                }
                for future in as_completed(futures):
                    entries, channel_name = future.result()
                    total_channels_checked += 1
                    if entries:
                        new_entries = [e for e in entries if (e["id"], e["published"]) not in known_keys]
                        if new_entries:
                            all_new_entries.extend(new_entries)
                            total_channels_with_new += 1
                        newest = max(entries, key=lambda e: e["published"])
                        rs.set(f"streamly:trailers:last_seen:{channel_name}", newest["published"])

            if batch_start + batch_size < len(channels):
                time.sleep(2)

        log.info(
            "Incremental crawl: %d new entries from %d/%d channels",
            len(all_new_entries), total_channels_with_new, len(channels),
        )

        # Update the trailer window: evicts older than 30 days and ZADDs new ones
        _update_trailer_window(rs, all_new_entries)

        # Fetch the updated full window (most recent first)
        with app.app_context():
            updated_window = get_current_trailer_window()

        # Build digest from the window
        digest = _build_digest(updated_window)
        rs.set(
            "streamly:trailers:feed",
            _json.dumps(digest),
            ex=_FEED_TTL_SECONDS,
        )
        log.info(
            "Incremental crawl complete: %d total entries in window -> %d day groups",
            len(updated_window), len(digest["items"]),
        )

    except Exception as e:
        log.exception("Incremental crawl error")
        # Serve stale feed if we have one (resilience against total proxy+direct failure)
        if rs:
            stale = rs.get("streamly:trailers:feed")
            if stale:
                log.warning("Crawl failed - serving stale feed")
    finally:
        try:
            rs._execute("DEL", "streamly:trailers:crawl_lock")
            rs.set("streamly:trailers:last_crawl_time", str(int(time.time())), ex=86400)
        except Exception:
            pass


def _start_crawl_thread(app, name: str = "TrailerDaemon-kick") -> None:
    def _run():
        with app.app_context():
            _crawl_trailers_incremental(app)
    threading.Thread(target=_run, name=name, daemon=True).start()


def trailer_daemon_loop(app) -> None:
    log.info("Trailer daemon started (interval=%ds)", _CRAWL_INTERVAL_SECONDS)
    while True:
        try:
            with app.app_context():
                _crawl_trailers_incremental(app)
        except Exception as e:
            log.exception("Trailer daemon loop error")
        time.sleep(_CRAWL_INTERVAL_SECONDS)


def start_trailer_daemon(app) -> None:
    t = threading.Thread(target=trailer_daemon_loop, args=(app,), name="TrailerDaemon", daemon=True)
    t.start()
    log.info("Trailer daemon thread spawned")


@trailers_bp.get("/api/trailers")
@rate_limited(cost=0.5)
def get_trailers():
    rs = getattr(current_app, "rs", None)
    if not rs:
        return jsonify({"items": []})

    raw_feed = rs.get("streamly:trailers:feed")
    if not raw_feed:
        _start_crawl_thread(current_app._get_current_object(), name="TrailerDaemon-kick")
        return jsonify({"items": []})

    try:
        data = _json.loads(raw_feed)
        # Trigger stale refresh if feed is older than _STALE_HOURS (2 hours)
        if isinstance(data, dict) and data.get("items"):
            newest_date = data["items"][0].get("date")
            if newest_date:
                newest_dt = datetime.strptime(newest_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - newest_dt) > timedelta(hours=_STALE_HOURS):
                    _start_crawl_thread(current_app._get_current_object(), name="TrailerDaemon-stale")
        return jsonify(data)
    except Exception:
        return jsonify({"items": []})


@trailers_bp.get("/api/trailers/status")
@rate_limited(cost=0.2)
def trailers_status():
    rs = getattr(current_app, "rs", None)
    if not rs:
        return jsonify({"status": "unknown", "last_crawl": None, "running": False, "channels": 0, "stale_hours": _STALE_HOURS})

    last_crawl = rs.get("streamly:trailers:last_crawl_time")
    lock = rs.get("streamly:trailers:crawl_lock")
    is_stale = False
    if last_crawl:
        try:
            lc = int(last_crawl)
            if (time.time() - lc) > (_STALE_HOURS * 3600):
                is_stale = True
        except Exception:
            pass
    return jsonify({
        "status": "ok",
        "last_crawl": int(last_crawl) if last_crawl and str(last_crawl).isdigit() else None,
        "running": bool(lock),
        "channels": len([c for c in TRAILER_CHANNELS if c[0] != "???"]),
        "stale_hours": _STALE_HOURS,
        "is_stale": is_stale,
    })


@trailers_bp.post("/api/trailers/refresh")
@rate_limited(cost=1.0)
@csrf_required
def refresh_trailers():
    rs = getattr(current_app, "rs", None)
    if not rs:
        return json_error(503, "redis_unavailable", "Redis is required")

    lock_raw = rs.get("streamly:trailers:crawl_lock")
    if lock_raw:
        return jsonify({"success": True, "message": "Refresh already in progress", "status": "running"})

    try:
        _start_crawl_thread(current_app._get_current_object(), name="TrailerDaemon-refresh")
        return jsonify({"success": True, "message": "Refresh started", "status": "started"})
    except Exception as e:
        log.warning("Failed to start refresh crawl: %s", e)
        return jsonify({"success": False, "message": "Failed to start refresh", "status": "error"})


@trailers_bp.get("/api/trailers/channels")
@rate_limited(cost=0.5)
def list_trailer_channels():
    return jsonify(
        {
            "channels": [
                {"id": cid, "name": name, "priority": _channel_priority(name)}
                for cid, name in TRAILER_CHANNELS
                if cid != "???"
            ]
        }
    )
