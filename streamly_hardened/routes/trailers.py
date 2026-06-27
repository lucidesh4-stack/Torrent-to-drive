from __future__ import annotations

import html
import json as _json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    before_log,
    after_log,
    retry_if_exception,
)
from flask import Blueprint, current_app, jsonify

from ..security import csrf_required, json_error, rate_limited

log = logging.getLogger(__name__)

trailers_bp = Blueprint("trailers", __name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_YOUTUBE_API_URL = "https://www.googleapis.com/youtube/v3/search"

# Tuning knobs — extracted from magic numbers
MAX_RESULTS_PER_PAGE = 10
MAX_PAGES = 3                     # pagination depth per channel
API_TIMEOUT_SECONDS = 15.0
WINDOW_DAYS = 30                  # rolling trailer window
TITLE_MIN_LEN = 8
TITLE_MAX_LEN = 120
CHANNEL_DELAY_SECONDS = 1        # polite pause between channels

# Circuit breaker
CB_FAILURE_THRESHOLD = 3          # consecutive failures to trip
CB_COOLDOWN_SECONDS = 1800        # 30 minutes

# Retry
MAX_RETRY_ATTEMPTS = 8
RETRY_MIN_WAIT = 2
RETRY_MAX_WAIT = 30

# Quota tracking — YouTube Data API v3 default is 10 000 units/day.
# A search.list call costs 100 units.
QUOTA_COST_PER_SEARCH = 100
QUOTA_DAILY_LIMIT = 10_000
QUOTA_WARNING_THRESHOLD = 0.80    # warn at 80 %

# Crawl timing
_CRAWL_INTERVAL_SECONDS = 86400
_CRAWL_LOCK_TTL = 600
_FEED_TTL_SECONDS = 172800
_STALE_HOURS = 24

# ---------------------------------------------------------------------------
# Centralized Redis keys
# ---------------------------------------------------------------------------

REDIS_KEYS: dict[str, str] = {
    "feed":            "streamly:trailers:feed",
    "window":          "streamly:trailers:window",
    "crawl_lock":      "streamly:trailers:crawl_lock",
    "last_crawl_time": "streamly:trailers:last_crawl_time",
    "proxy_health":    "streamly:trailers:proxy_health",
    "cb_failures":     "streamly:trailers:cb_failures",
    "cb_open_until":   "streamly:trailers:cb_open_until",
    "metric_api_calls":    "streamly:trailers:metric:api_calls",
    "metric_videos":       "streamly:trailers:metric:videos_fetched",
    "metric_quota_used":   "streamly:trailers:metric:quota_used",
}


def _rkey(name: str) -> str:
    """Return the Redis key for *name*, raising KeyError on typos."""
    return REDIS_KEYS[name]


def _last_seen_key(channel_id: str) -> str:
    return f"streamly:trailers:last_seen:{channel_id}"


# ---------------------------------------------------------------------------
# Content filters
# ---------------------------------------------------------------------------

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

_SHORTS_PATTERNS = [
    '#shorts', '#short', '#shortsfeed', '#ytshorts', '#youtubeshorts',
    '#reels', '#tiktok', '#vertical',
]


def _is_shorts_by_title(title: str) -> bool:
    """Return True if *title* looks like a YouTube Short."""
    t = title.lower()
    if any(p in t for p in _SHORTS_PATTERNS):
        return True
    if re.search(r'(^|\s)#?shorts($|\s)', t):
        return True
    if re.search(r'\bshort\b', t) and 'trailer' not in t and 'teaser' not in t:
        return True
    return False


def _passes_title_filter(title: str) -> bool:
    """Return True if *title* passes all content-quality filters."""
    tl = title.lower()
    if any(bw in tl for bw in _BLOCKED_KEYWORDS):
        return False
    if "trailer" not in tl and "teaser" not in tl:
        return False
    if _is_shorts_by_title(title):
        return False
    if any(h in tl for h in _TITLE_HEURISTICS):
        return False
    if len(title) < TITLE_MIN_LEN or len(title) > TITLE_MAX_LEN:
        return False
    return True


# ---------------------------------------------------------------------------
# Channel metadata
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Title helpers
# ---------------------------------------------------------------------------

def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation/extra whitespace, collapse series numbering."""
    t = title.lower()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\b(part|chapter|volume|episode)\s+\d+\b", r"\1", t)
    return t.strip()


def _extract_trailer_info(title: str) -> tuple[str, str, int]:
    """Parse *title* into (normalized_movie_name, media_type, trailer_number)."""
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


def _parse_iso(dt_str: str) -> datetime | None:
    """Safely parse an ISO-8601 datetime string into a timezone-aware datetime."""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

def _redis_get_str(rs, key: str) -> str | None:
    """Get a Redis value and decode bytes to str."""
    val = rs.get(key)
    if val is None:
        return None
    return val.decode("utf-8") if isinstance(val, bytes) else str(val)


def _redis_incr(rs, key: str, amount: int = 1) -> None:
    """Increment a Redis counter, ignoring errors."""
    try:
        rs._execute("INCRBY", key, str(amount))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Circuit breaker (lightweight, Redis-backed)
# ---------------------------------------------------------------------------

class _CircuitBreaker:
    """Simple circuit breaker that trips after *threshold* consecutive failures
    and stays open for *cooldown* seconds.  State lives in Redis so it
    survives process restarts and is shared across workers."""

    def __init__(self, threshold: int = CB_FAILURE_THRESHOLD,
                 cooldown: int = CB_COOLDOWN_SECONDS):
        self.threshold = threshold
        self.cooldown = cooldown

    def is_open(self, rs) -> bool:
        """Return True if the breaker is open (calls should be skipped)."""
        open_until = _redis_get_str(rs, _rkey("cb_open_until"))
        if open_until:
            try:
                if time.time() < float(open_until):
                    return True
                # Cooldown expired — close the breaker
                self._close(rs)
            except (ValueError, TypeError):
                pass
        return False

    def record_success(self, rs) -> None:
        """Reset the failure counter on success."""
        try:
            rs.set(_rkey("cb_failures"), "0", ex=3600)
        except Exception:
            pass

    def record_failure(self, rs) -> None:
        """Increment the failure counter; trip the breaker if threshold reached."""
        try:
            failures = int(_redis_get_str(rs, _rkey("cb_failures")) or "0") + 1
            rs.set(_rkey("cb_failures"), str(failures), ex=3600)
            if failures >= self.threshold:
                self._open(rs)
        except Exception:
            pass

    def _open(self, rs) -> None:
        reopen_at = time.time() + self.cooldown
        rs.set(_rkey("cb_open_until"), str(reopen_at), ex=self.cooldown + 60)
        log.warning(
            "Circuit breaker OPEN — pausing API calls for %d s after %d consecutive failures",
            self.cooldown, self.threshold,
        )

    def _close(self, rs) -> None:
        rs._execute("DEL", _rkey("cb_open_until"))
        rs.set(_rkey("cb_failures"), "0", ex=3600)
        log.info("Circuit breaker CLOSED — resuming API calls")


_circuit_breaker = _CircuitBreaker()


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

class YouTubeQuotaExceeded(Exception):
    """Raised when the API returns 403 due to quota exhaustion."""


class YouTubeAuthError(Exception):
    """Raised on authentication / key errors (401, 403 with auth reason)."""


class YouTubeTransientError(Exception):
    """Raised on transient network / server errors worth retrying."""


def _classify_api_error(exc: Exception) -> Exception:
    """Wrap an httpx error into a classified exception."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        body = ""
        try:
            body = exc.response.text
        except Exception:
            pass

        if status == 403:
            if "quotaExceeded" in body or "dailyLimitExceeded" in body:
                return YouTubeQuotaExceeded(f"YouTube quota exceeded: {body[:200]}")
            return YouTubeAuthError(f"YouTube 403 (auth/key issue): {body[:200]}")
        if status == 401:
            return YouTubeAuthError(f"YouTube 401: {body[:200]}")
        if status >= 500:
            return YouTubeTransientError(f"YouTube {status} server error")
        # 4xx other — not retryable
        return exc

    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout,
                        httpx.PoolTimeout, httpx.ConnectTimeout)):
        return YouTubeTransientError(str(exc))

    return exc


def _is_retryable(exc: Exception) -> bool:
    """Return True if *exc* is worth retrying (transient)."""
    return isinstance(exc, (YouTubeTransientError, httpx.ConnectError,
                            httpx.ReadTimeout, httpx.WriteTimeout,
                            httpx.PoolTimeout, httpx.ConnectTimeout))


# ---------------------------------------------------------------------------
# YouTube Data API v3 fetch — with pagination + retry
# ---------------------------------------------------------------------------

@retry(
    stop=stop_after_attempt(MAX_RETRY_ATTEMPTS),
    wait=wait_exponential(multiplier=2, min=RETRY_MIN_WAIT, max=RETRY_MAX_WAIT),
    retry=retry_if_exception(_is_retryable),
    before=before_log(log, logging.WARNING),
    after=after_log(log, logging.INFO),
    reraise=True,
)
def _fetch_channel_api(
    channel_id: str,
    channel_name: str,
    last_seen: str | None = None,
    api_key: str = "",
) -> list[dict]:
    """Fetch recent trailer videos for *channel_id* via YouTube Data API v3.

    Paginates up to ``MAX_PAGES`` pages of ``MAX_RESULTS_PER_PAGE`` results.
    Applies all content-quality filters and returns a list of trailer dicts.
    """
    if not api_key:
        log.error(
            "YOUTUBE_API_KEY is empty — cannot fetch channel %s (%s)",
            channel_name, channel_id,
        )
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    published_after = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    results: list[dict] = []
    page_token: str | None = None
    pages_fetched = 0
    t0 = time.monotonic()

    with httpx.Client(http2=True, timeout=API_TIMEOUT_SECONDS) as client:
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {
                "part": "snippet",
                "channelId": channel_id,
                "type": "video",
                "order": "date",
                "publishedAfter": published_after,
                "maxResults": MAX_RESULTS_PER_PAGE,
                "key": api_key,
            }
            if page_token:
                params["pageToken"] = page_token

            try:
                r = client.get(_YOUTUBE_API_URL, params=params)
                r.raise_for_status()
            except Exception as exc:
                raise _classify_api_error(exc) from exc

            data = r.json()
            pages_fetched += 1

            for item in data.get("items", []):
                entry = _parse_api_item(item, channel_name, cutoff, last_seen)
                if entry:
                    results.append(entry)

            page_token = data.get("nextPageToken")
            if not page_token:
                break

    elapsed = time.monotonic() - t0
    log.info(
        "YouTube API: channel=%s pages=%d results=%d elapsed=%.2fs",
        channel_name, pages_fetched, len(results), elapsed,
    )
    return results


def _parse_api_item(
    item: dict,
    channel_name: str,
    cutoff: datetime,
    last_seen: str | None,
) -> dict | None:
    """Parse a single YouTube API search result into a trailer dict, or None."""
    snippet = item.get("snippet", {})
    title = html.unescape(snippet.get("title", "")).strip()

    if not _passes_title_filter(title):
        return None

    published = snippet.get("publishedAt", "")
    if not published:
        return None

    pub_dt = _parse_iso(published)
    if pub_dt is None or pub_dt < cutoff:
        return None
    if last_seen and published <= last_seen:
        return None

    vid = item.get("id", {}).get("videoId")
    if not vid:
        return None

    thumbnails = snippet.get("thumbnails", {})
    thumb_url = (
        thumbnails.get("medium", {}).get("url")
        or thumbnails.get("default", {}).get("url")
        or f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
    )

    norm_name, media_type, num = _extract_trailer_info(title)

    return {
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


# ---------------------------------------------------------------------------
# Redis trailer-window management
# ---------------------------------------------------------------------------

def _update_trailer_window(rs, new_entries: list[dict]) -> None:
    """Evict trailers older than WINDOW_DAYS and ZADD new ones."""
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).timestamp()

    # Evict old entries
    try:
        rs._execute("ZREMRANGEBYSCORE", _rkey("window"), "-inf", str(cutoff_ts))
    except Exception as exc:
        log.warning("Failed to evict old trailers from window: %s", exc)

    if not new_entries:
        return

    # Build {member: score} mapping for zadd
    mapping: dict[str, float] = {}
    for e in new_entries:
        pub_dt = _parse_iso(e["published"])
        if pub_dt is None:
            continue
        score = pub_dt.timestamp()
        if score < cutoff_ts:
            continue
        mapping[_json.dumps(e)] = score

    if mapping:
        try:
            zadd_args: list[str] = []
            for member, score in mapping.items():
                zadd_args.extend([str(score), member])
            rs._execute("ZADD", _rkey("window"), *zadd_args)
        except Exception as exc:
            log.warning("Failed to ZADD new trailers to window: %s", exc)


def get_current_trailer_window() -> list[dict]:
    """Return current trailers in the rolling window, most recent first."""
    rs = None
    try:
        from flask import current_app
        rs = getattr(current_app, "rs", None)
    except Exception:
        pass
    if not rs:
        return []

    try:
        members = rs._execute("ZREVRANGEBYSCORE", _rkey("window"), "+inf", "-inf")
    except Exception as exc:
        log.warning("Failed to read trailer window: %s", exc)
        return []

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


# ---------------------------------------------------------------------------
# Digest builder
# ---------------------------------------------------------------------------

def _build_digest(entries: list[dict]) -> dict:
    """Deduplicate and group trailer entries by date, returning a feed dict."""
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


# ---------------------------------------------------------------------------
# Quota tracking helpers
# ---------------------------------------------------------------------------

def _track_quota(rs, pages_fetched: int) -> None:
    """Increment API-call and quota-usage counters in Redis."""
    _redis_incr(rs, _rkey("metric_api_calls"), pages_fetched)
    units = pages_fetched * QUOTA_COST_PER_SEARCH
    _redis_incr(rs, _rkey("metric_quota_used"), units)

    # Check if approaching daily limit
    raw = _redis_get_str(rs, _rkey("metric_quota_used"))
    if raw:
        try:
            used = int(raw)
            if used >= int(QUOTA_DAILY_LIMIT * QUOTA_WARNING_THRESHOLD):
                log.warning(
                    "YouTube API quota usage HIGH: %d / %d units (%.0f%%)",
                    used, QUOTA_DAILY_LIMIT, used / QUOTA_DAILY_LIMIT * 100,
                )
        except (ValueError, TypeError):
            pass


# ---------------------------------------------------------------------------
# Main crawl loop
# ---------------------------------------------------------------------------

def _crawl_trailers_incremental(app) -> None:
    """Crawl all TRAILER_CHANNELS sequentially, updating the rolling window."""
    rs = getattr(app, "rs", None)
    if not rs:
        log.warning("Trailer crawl skipped: Redis unavailable")
        return

    # Acquire distributed lock (SET key value EX seconds NX)
    lock_result = rs._execute("SET", _rkey("crawl_lock"), "1", "EX", str(_CRAWL_LOCK_TTL), "NX")
    if lock_result != "OK":
        return

    try:
        # Re-read API key on every crawl (allows runtime rotation)
        api_key = os.environ.get("YOUTUBE_API_KEY", "")
        if not api_key:
            log.error("YOUTUBE_API_KEY is not set — trailer crawl cannot proceed")
            return

        # Load existing window to detect duplicates
        with app.app_context():
            current_window = get_current_trailer_window()

        known_keys: set[tuple[str, str]] = set()
        for e in current_window:
            known_keys.add((e["id"], e["published"]))

        channels = [c for c in TRAILER_CHANNELS if c[0] and c[0] != "???"]
        all_new_entries: list[dict] = []
        total_checked = 0
        total_with_new = 0

        for channel_id, channel_name in channels:
            # Circuit breaker check
            if _circuit_breaker.is_open(rs):
                log.warning(
                    "Circuit breaker OPEN — skipping remaining channels (at %s)",
                    channel_name,
                )
                break

            log.info("Fetching channel: %s (%s)", channel_name, channel_id)
            t0 = time.monotonic()
            try:
                last_seen = _redis_get_str(rs, _last_seen_key(channel_id))
                entries = _fetch_channel_api(
                    channel_id, channel_name,
                    last_seen=last_seen,
                    api_key=api_key,
                )
                total_checked += 1
                _circuit_breaker.record_success(rs)

                # Track quota (each call is 1 page minimum)
                _track_quota(rs, max(1, len(entries) // MAX_RESULTS_PER_PAGE or 1))

                if entries:
                    new_entries = [
                        e for e in entries
                        if (e["id"], e["published"]) not in known_keys
                    ]
                    if new_entries:
                        all_new_entries.extend(new_entries)
                        total_with_new += 1
                        _redis_incr(rs, _rkey("metric_videos"), len(new_entries))
                    newest = max(entries, key=lambda e: e["published"])
                    rs.set(_last_seen_key(channel_id), newest["published"])

                elapsed = time.monotonic() - t0
                log.info(
                    "✓ %s completed — entries=%d new=%d elapsed=%.2fs",
                    channel_name,
                    len(entries),
                    len([e for e in entries if (e["id"], e["published"]) not in known_keys]),
                    elapsed,
                )

            except YouTubeQuotaExceeded:
                log.error("YouTube quota exceeded — stopping crawl at %s", channel_name)
                _circuit_breaker.record_failure(rs)
                break  # no point continuing

            except YouTubeAuthError as e:
                log.error("YouTube auth error at %s: %s", channel_name, e)
                _circuit_breaker.record_failure(rs)
                break  # key is bad, stop

            except Exception as e:
                elapsed = time.monotonic() - t0
                log.error(
                    "✗ %s failed — error_type=%s error=%s elapsed=%.2fs",
                    channel_name, type(e).__name__, e, elapsed,
                )
                _circuit_breaker.record_failure(rs)

            time.sleep(CHANNEL_DELAY_SECONDS)

        log.info(
            "Crawl summary: new_entries=%d channels_with_new=%d/%d checked",
            len(all_new_entries), total_with_new, total_checked,
        )

        # Update the 30-day rolling window
        _update_trailer_window(rs, all_new_entries)

        # Rebuild the digest from the full window
        with app.app_context():
            updated_window = get_current_trailer_window()

        digest = _build_digest(updated_window)
        rs.set(
            _rkey("feed"),
            _json.dumps(digest),
            ex=_FEED_TTL_SECONDS,
        )
        log.info(
            "Crawl complete: window_size=%d day_groups=%d",
            len(updated_window), len(digest["items"]),
        )

    except Exception:
        log.exception("Incremental crawl error")
        if rs:
            stale = rs.get(_rkey("feed"))
            if stale:
                log.warning("Crawl failed — serving stale feed")
    finally:
        try:
            rs._execute("DEL", _rkey("crawl_lock"))
            rs.set(_rkey("last_crawl_time"), str(int(time.time())), ex=86400)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Daemon / thread helpers
# ---------------------------------------------------------------------------

def _start_crawl_thread(app, name: str = "TrailerDaemon-kick") -> None:
    """Spawn a background thread to run one crawl cycle."""
    def _run():
        with app.app_context():
            _crawl_trailers_incremental(app)
    threading.Thread(target=_run, name=name, daemon=True).start()


def trailer_daemon_loop(app) -> None:
    """Long-running daemon that crawls on a fixed interval."""
    log.info("Trailer daemon started (interval=%ds)", _CRAWL_INTERVAL_SECONDS)
    while True:
        try:
            with app.app_context():
                _crawl_trailers_incremental(app)
        except Exception:
            log.exception("Trailer daemon loop error")
        time.sleep(_CRAWL_INTERVAL_SECONDS)


def start_trailer_daemon(app) -> None:
    """Spawn the trailer daemon thread."""
    t = threading.Thread(target=trailer_daemon_loop, args=(app,), name="TrailerDaemon", daemon=True)
    t.start()
    log.info("Trailer daemon thread spawned")


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@trailers_bp.get("/api/trailers")
@rate_limited(cost=0.5)
def get_trailers():
    """Return the cached trailer feed, kicking a crawl if empty/stale."""
    rs = getattr(current_app, "rs", None)
    if not rs:
        return jsonify({"items": []})

    raw_feed = rs.get(_rkey("feed"))
    if not raw_feed:
        _start_crawl_thread(current_app._get_current_object(), name="TrailerDaemon-kick")
        return jsonify({"items": []})

    try:
        data = _json.loads(raw_feed)
        # Trigger stale refresh if feed is older than _STALE_HOURS
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
    """Return crawl status, staleness, and circuit-breaker state."""
    rs = getattr(current_app, "rs", None)
    if not rs:
        return jsonify({"status": "unknown", "last_crawl": None, "running": False, "channels": 0, "stale_hours": _STALE_HOURS})

    last_crawl = rs.get(_rkey("last_crawl_time"))
    lock = rs.get(_rkey("crawl_lock"))
    is_stale = False
    if last_crawl:
        try:
            lc = int(last_crawl)
            if (time.time() - lc) > (_STALE_HOURS * 3600):
                is_stale = True
        except Exception:
            pass

    cb_open = _circuit_breaker.is_open(rs)

    return jsonify({
        "status": "ok",
        "last_crawl": int(last_crawl) if last_crawl and str(last_crawl).isdigit() else None,
        "running": bool(lock),
        "channels": len([c for c in TRAILER_CHANNELS if c[0] != "???"]),
        "stale_hours": _STALE_HOURS,
        "is_stale": is_stale,
        "circuit_breaker_open": cb_open,
    })


@trailers_bp.post("/api/trailers/refresh")
@rate_limited(cost=1.0)
@csrf_required
def refresh_trailers():
    """Manually trigger a trailer crawl."""
    rs = getattr(current_app, "rs", None)
    if not rs:
        return json_error(503, "redis_unavailable", "Redis is required")

    lock_raw = rs.get(_rkey("crawl_lock"))
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
    """List all configured trailer channels."""
    return jsonify(
        {
            "channels": [
                {"id": cid, "name": name, "priority": _channel_priority(name)}
                for cid, name in TRAILER_CHANNELS
                if cid != "???"
            ]
        }
    )
