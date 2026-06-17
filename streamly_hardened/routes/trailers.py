from __future__ import annotations

import json as _json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

import requests
from flask import Blueprint, current_app, jsonify

from ..security import json_error, rate_limited

log = logging.getLogger(__name__)

trailers_bp = Blueprint("trailers", __name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CRAWL_INTERVAL_SECONDS = 600          # 10 minutes
_CRAWL_LOCK_TTL = 600                  # 10 minutes (same as interval)
_TMD_CACHE_TTL_SECONDS = 21600         # 6 hours
_FEED_TTL_SECONDS = 86400              # 24 hours
_STALE_FEED_THRESHOLD_SECONDS = 7200   # 2 hours; if feed is older, trigger immediate refresh

# Blocked keywords in YouTube titles (non-movie content)
_BLOCKED_KEYWORDS = {
    "gameplay", "story trailer", "game", "behind the scenes", "bts",
    "interview", "vlog", "reaction", "review", "unboxing", "lego",
    "funko", "toy", "merchandise", "comic con", "panel", "clip",
    "featurette", "tv spot", "tv spots", "making of", "b-roll",
    "exclusive look", "extended look", "first look", "announcement",
}

# TMDB-matching heuristic title blacklist (lowercased, partial match)
_TITLE_HEURISTICS = {
    "gameplay", "vlog", "reaction", "review", "unboxing", "lego", "funko",
    "toy", "merchandise", "comic con", "bts", "behind the scenes",
}

# Channel priority (lower = higher authority). Used when the same trailer
# appears from multiple channels.
_CHANNEL_PRIORITY = {
    "Marvel Entertainment": 0,
    "Warner Bros": 0,
    "Sony Pictures": 0,
    "Universal Pictures": 0,
    "Paramount Pictures": 0,
    "Netflix": 0,
    "A24": 0,
    "Amazon MGM Studios": 0,
    "HBO": 0,
    "Searchlight Pictures": 0,
    "Lionsgate Movies": 0,
    "YRF": 0,
    "Dharma Productions": 0,
    "T-Series": 0,
    "Zee Studios": 0,
    "Red Chillies Entertainment": 0,
    "Excel Entertainment": 0,
    "Pen Movies": 0,
    "AA Films": 0,
    "Geetha Arts": 0,
    "Mythri Movie Makers": 0,
    "Sithara Entertainments": 0,
    "Sun Pictures": 0,
    "Lyca Productions": 0,
    "Hombale Films": 0,
    "Vyjayanthi Movies": 0,
    "Prithviraj Productions": 0,
    "ONE Media": 1,
    "Movieclips Trailers": 1,
    "Rotten Tomatoes Trailers": 1,
    "Bollywood Hungama": 1,
    "Telugu Filmnagar": 1,
    "123 Telugu": 1,
    "Goldmines Telefilms": 1,
}

TRAILER_CHANNELS = [
    # Hollywood (minute 0)
    ("UCjmJDM5pRKbUlVIzDYYWbUw", "Warner Bros"),
    ("UCvC4D8onUfXzvjTOM-dBfEA", "Marvel Entertainment"),
    ("UCz97F7dMxBNOfGYu3rx8aCw", "Sony Pictures"),
    ("UCq0OueAsdxH6b8nyAspwViw", "Universal Pictures"),
    ("UCF9imwPMSGz4Vq1NiTWCC7g", "Paramount Pictures"),
    ("UCWOA1ZGywLbqmigxE4Qlvuw", "Netflix"),
    ("UCuaFvcY4MhZY3U43mMt1dYQ", "Disney"),
    ("UCwYzZs_hwA6NdaQp6Hjhe5w", "ONE Media"),
    ("UC3gNmTGu-TTbFPpfSs5kNkg", "Movieclips Trailers"),
    ("UCuPivVjnfNo4mb3Oog_frZg", "A24"),
    ("UCf5CjDJvsFvtVIhkfmKAwAA", "Amazon MGM Studios"),
    ("UCVTQuK2CaWaTgSsoNkn5AiQ", "HBO"),
    ("UCor9rW6PgxSQ9vUPWQdnaYQ", "Searchlight Pictures"),
    ("UCJ6nMHaJPZvsJ-HmUmj1SeA", "Lionsgate Movies"),
    # Bollywood (minute 3)
    ("UCbTLwN10NoCU4WDzLf1JMOA", "YRF"),
    ("UCGdHCtXEzkCB7-g_NXYZPcw", "Dharma Productions"),
    ("UCq-Fj5jknLsUf-MWSy4_brA", "T-Series"),
    ("UC3jMepkLKF8y4iiwWmAB3RA", "Zee Studios"),
    ("UCjJKg01HAP01xCLVhDmnLhw", "Red Chillies Entertainment"),
    ("UCn9BuiRZGR_tPM2GGT4jN-w", "Excel Entertainment"),
    ("UC3ar28GS6o1p0m_wabfk2zw", "Pen Movies"),
    ("UCdfXaARoko58ZraSegLA-4A", "AA Films"),
    # South Indian (minute 6)
    ("UCiJfiEg1FImWsVuEu0L8X6Q", "Geetha Arts"),
    ("UCKZSn5C-RzrLjuWJF8wWiDw", "Mythri Movie Makers"),
    ("UC2woPAI_KMAR25R_oezEQqw", "Sithara Entertainments"),
    ("UCo2r1S9iXJshkeV9v_ZDicw", "Sun Pictures"),
    ("UCA7gwgLgmCZ8DSmdf2bhb8g", "Lyca Productions"),
    ("UCarJoVXH0T2pdtcHBu9J8Bw", "Hombale Films"),
    ("UCdj3E_o7ONZ9_6EEN3Mj6YQ", "Vyjayanthi Movies"),
    ("UCH1Gszpy-NmA6ZXZaxhnlwA", "Prithviraj Productions"),
]

# ---------------------------------------------------------------------------
# TMDB cache helpers
# ---------------------------------------------------------------------------

def _tmdb_api_key() -> str | None:
    return current_app.config.get("TMDB_API_KEY") or os.getenv("TMDB_API_KEY", "")


def _fetch_tmdb_titles() -> set[str]:
    """Fetch upcoming + now-playing movies and on-air TV from TMDB.
    Returns a set of normalized (lowercase, stripped) titles."""
    key = _tmdb_api_key()
    if not key:
        return set()

    titles: set[str] = set()
    endpoints = [
        ("https://api.themoviedb.org/3/movie/upcoming", "results"),
        ("https://api.themoviedb.org/3/movie/now_playing", "results"),
        ("https://api.themoviedb.org/3/tv/on_the_air", "results"),
    ]

    for url, key_name in endpoints:
        try:
            r = requests.get(url, params={"api_key": key, "page": "1"}, timeout=15)
            r.raise_for_status()
            data = r.json()
            for item in data.get(key_name, []):
                title = item.get("title") or item.get("name") or ""
                if title:
                    titles.add(_normalize_title(title))
                # Also include original title / alternative names
                original = item.get("original_title") or item.get("original_name")
                if original:
                    titles.add(_normalize_title(original))
        except Exception as e:
            log.warning("TMDB fetch failed for %s: %s", url, e)

    return titles


def _load_tmdb_cache(rs) -> set[str]:
    """Return cached TMDB title set. Refresh if stale or missing."""
    raw = rs.get("streamly:trailers:tmdb_cache")
    if raw:
        try:
            data = _json.loads(raw)
            if isinstance(data, dict) and isinstance(data.get("titles"), list):
                fetched_at = data.get("fetched_at", 0)
                if (time.time() - fetched_at) < _TMD_CACHE_TTL_SECONDS:
                    return set(data["titles"])
        except Exception:
            pass

    titles = _fetch_tmdb_titles()
    if titles:
        try:
            rs.set(
                "streamly:trailers:tmdb_cache",
                _json.dumps({"titles": list(titles), "fetched_at": time.time()}),
                ex=_TMD_CACHE_TTL_SECONDS,
            )
        except Exception as e:
            log.warning("Failed to cache TMDB titles: %s", e)
    return titles


# ---------------------------------------------------------------------------
# Text / title helpers
# ---------------------------------------------------------------------------

def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    t = title.lower()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    # Strip common suffixes like "part 1", "chapter 2"
    t = re.sub(r"\b(part|chapter|volume|episode)\s+\d+\b", r"\1", t)
    return t.strip()


def _extract_trailer_info(title: str) -> tuple[str, str, int]:
    """Return (normalized_name, media_type, number).

    media_type is 'teaser' or 'trailer'.
    number is the integer suffix (Trailer 2 -> 2, default 0).
    """
    t = title.lower()

    # Determine type
    if "teaser" in t:
        media_type = "teaser"
    else:
        media_type = "trailer"

    # Extract number
    num_match = re.search(r"\b(?:trailer|teaser)\s*(\d+)\b", t)
    num = int(num_match.group(1)) if num_match else 0

    # Strip everything after the first pipe/dash/en-dash followed by trailer/teaser keywords
    parts = re.split(r"(\||–|-)\s*(official|final|teaser|trailer|exclusive|extended|first|hindi|tamil|telugu|malayalam|kannada)", t)
    name = parts[0] if parts else t

    # Also strip language suffixes that appear before the separator
    name = re.sub(r"\b(hindi|tamil|telugu|malayalam|kannada|english)\s*(trailer|teaser|dubbed)?\b", "", name)

    name = _normalize_title(name)
    return name, media_type, num


def _fuzzy_match(query: str, title_set: set[str], threshold: float = 0.85) -> bool:
    """Quick fuzzy match using difflib.SequenceMatcher."""
    if not title_set:
        return False
    if query in title_set:
        return True
    # Fast substring check first
    for t in title_set:
        if query in t or t in query:
            if len(query) >= 5 and len(t) >= 5:
                return True
    # Full fuzzy scan
    for t in title_set:
        if SequenceMatcher(None, query, t).ratio() >= threshold:
            return True
    return False


def _channel_priority(name: str) -> int:
    return _CHANNEL_PRIORITY.get(name, 99)


# ---------------------------------------------------------------------------
# RSS crawl helpers
# ---------------------------------------------------------------------------

def _parse_rss(xml_text: str, channel_name: str, tmdb_titles: set[str]) -> list[dict]:
    """Parse a YouTube RSS feed and return validated trailer entries."""
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

        # Gate 1: blocked keywords
        if any(bw in title_lower for bw in _BLOCKED_KEYWORDS):
            continue

        # Gate 2: must contain trailer or teaser
        if "trailer" not in title_lower and "teaser" not in title_lower:
            continue

        # Gate 3: heuristic blacklist (fallback when TMDB is off)
        if not tmdb_titles:
            if any(h in title_lower for h in _TITLE_HEURISTICS):
                continue
            # Reject extremely short or extremely long titles
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

        id_elem = entry.find("atom:id", ns)
        if id_elem is None or id_elem.text is None:
            continue
        vid = id_elem.text.split(":")[-1]

        thumb = entry.find("media:group/media:thumbnail", ns)
        thumb_url = (
            thumb.get("url")
            if thumb is not None
            else f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
        )

        norm_name, media_type, num = _extract_trailer_info(title)

        # Gate 4: TMDB fuzzy match (if available)
        if tmdb_titles and not _fuzzy_match(norm_name, tmdb_titles):
            continue

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
    """Merge entries into a date-grouped deduplicated digest.

    Returns: {"items": [{"date": "YYYY-MM-DD", "items": [...]}]}
    """
    # Deduplicate by (date, normalized, type, number) keeping highest priority
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

    # Group by date -> normalized title
    by_date: dict[str, dict[str, dict]] = {}
    for key, e in best.items():
        date, norm, _, _ = key
        if date not in by_date:
            by_date[date] = {}
        if norm not in by_date[date]:
            by_date[date][norm] = {
                "title": e["title"],
                "normalized": norm,
                "category": "movie",  # TMDB could enrich this later
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

    # Sort and build output
    items = []
    for date in sorted(by_date.keys(), reverse=True):
        day_items = []
        for norm in sorted(by_date[date].keys()):
            item = by_date[date][norm]
            # Sort videos: teaser first, then trailer, then by number
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


# ---------------------------------------------------------------------------
# Background daemon
# ---------------------------------------------------------------------------

def _crawl_trailers(app) -> None:
    """Single crawl cycle. Must be called inside app_context."""
    rs = getattr(app, "rs", None)
    if not rs:
        log.warning("Trailer crawl skipped: Redis unavailable")
        return

    lock = rs._execute("SET", "streamly:trailers:crawl_lock", "1", "EX", str(_CRAWL_LOCK_TTL), "NX")
    if lock != "OK":
        return

    try:
        tmdb_titles = _load_tmdb_cache(rs)
        if not tmdb_titles:
            log.info("TMDB cache empty; running with keyword-only fallback")

        all_entries: list[dict] = []
        channels = [c for c in TRAILER_CHANNELS if c[0] and c[0] != "???"]

        for idx, (channel_id, channel_name) in enumerate(channels):
            # Stagger: Hollywood (0), Bollywood (3), South Indian (6)
            if idx == 5:
                time.sleep(180)
            elif idx == 11:
                time.sleep(180)
            elif idx > 0:
                time.sleep(2)  # Small delay between same-batch channels

            try:
                r = requests.get(
                    f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
                    timeout=15,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                )
                if r.status_code != 200:
                    log.warning("RSS HTTP %d for %s", r.status_code, channel_name)
                    continue

                entries = _parse_rss(r.text, channel_name, tmdb_titles)
                all_entries.extend(entries)

                # Update last_seen to the newest entry from this channel
                if entries:
                    newest = max(entries, key=lambda e: e["published"])
                    rs.set(
                        f"streamly:trailers:last_seen:{channel_id}",
                        newest["published"],
                    )
            except Exception as e:
                log.warning("RSS fetch failed for %s: %s", channel_name, e)
                continue

        # Load existing digest and merge
        existing = None
        raw_feed = rs.get("streamly:trailers:feed")
        if raw_feed:
            try:
                existing = _json.loads(raw_feed)
            except Exception:
                pass

        if existing and isinstance(existing, dict) and "items" in existing:
            # Flatten existing entries so we can re-merge with fresh ones
            existing_entries = []
            for day_group in existing["items"]:
                for item in day_group.get("items", []):
                    for vid in item.get("videos", []):
                        existing_entries.append(
                            {
                                "title": item["title"],
                                "normalized": item["normalized"],
                                "type": vid["type"],
                                "number": vid["number"],
                                "id": vid["id"],
                                "channel": vid["channel"],
                                "published": vid["published"],
                                "thumbnail": vid["thumbnail"],
                                "url": vid["url"],
                                "priority": _channel_priority(vid["channel"]),
                            }
                        )
            all_entries.extend(existing_entries)

        digest = _build_digest(all_entries)
        rs.set(
            "streamly:trailers:feed",
            _json.dumps(digest),
            ex=_FEED_TTL_SECONDS,
        )
        log.info(
            "Trailer crawl complete: %d total entries -> %d day groups",
            len(all_entries),
            len(digest["items"]),
        )
    except Exception as e:
        log.exception("Trailer crawl error")
    finally:
        try:
            rs._execute("DEL", "streamly:trailers:crawl_lock")
        except Exception:
            pass


def trailer_daemon_loop(app) -> None:
    """Background thread target. Catches all exceptions and sleeps."""
    log.info("Trailer daemon started (interval=%ds)", _CRAWL_INTERVAL_SECONDS)
    while True:
        try:
            with app.app_context():
                _crawl_trailers(app)
        except Exception as e:
            log.exception("Trailer daemon loop error")
        time.sleep(_CRAWL_INTERVAL_SECONDS)


def start_trailer_daemon(app) -> None:
    """Start the background trailer polling thread. Idempotent (lock guarded)."""
    t = threading.Thread(target=trailer_daemon_loop, args=(app,), name="TrailerDaemon", daemon=True)
    t.start()
    log.info("Trailer daemon thread spawned")


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@trailers_bp.get("/api/trailers")
@rate_limited(cost=0.5)
def get_trailers():
    """Serve the cached trailer digest. Triggers a background crawl if the
    feed is completely missing or older than the stale threshold."""
    rs = getattr(current_app, "rs", None)
    if not rs:
        return jsonify({"items": []})

    # Trigger background refresh if missing or stale
    raw_feed = rs.get("streamly:trailers:feed")
    if not raw_feed:
        # Kick off an immediate background crawl
        threading.Thread(
            target=lambda: _crawl_trailers(current_app._get_current_object()),
            name="TrailerDaemon-kick",
            daemon=True,
        ).start()
        return jsonify({"items": []})

    try:
        data = _json.loads(raw_feed)
        # Check staleness
        if isinstance(data, dict) and data.get("items"):
            # Peek at the newest item to estimate freshness
            newest_date = data["items"][0].get("date")
            if newest_date:
                newest_dt = datetime.strptime(newest_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - newest_dt).days > 2:
                    threading.Thread(
                        target=lambda: _crawl_trailers(current_app._get_current_object()),
                        name="TrailerDaemon-stale",
                        daemon=True,
                    ).start()
        return jsonify(data)
    except Exception:
        return jsonify({"items": []})


@trailers_bp.get("/api/trailers/channels")
@rate_limited(cost=0.5)
def list_trailer_channels():
    """Return the configured channel list (for debugging / admin)."""
    return jsonify(
        {
            "channels": [
                {"id": cid, "name": name, "priority": _channel_priority(name)}
                for cid, name in TRAILER_CHANNELS
                if cid != "???"
            ]
        }
    )
