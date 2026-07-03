# removed future annotations

import re
import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from typing import Optional

from ..auth_utils import current_client
from ..security import validate_query, rate_limited
from ..search_service import (
    group_series_results,
    build_packs,
    _dedup_by_infohash,
    group_by_quality,
    parse_release,
    matches_query,
    _normalize_encoder,
    _quality_bucket,
)

log = logging.getLogger(__name__)
search_router = APIRouter()

ALLOWED_QUALITIES = ["2160p", "1080p", "720p"]
PRESET_ENCODERS = ["ELiTE", "PSA", "MeGusta"]
SERIES_MAX_REQUESTS = 12


def _csv(value: str | None) -> list[str]:
    out = []
    for part in str(value or "").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


@search_router.get("/api/suggest")
@rate_limited(cost=0.5)
async def suggest(request: Request, q: str):
    config = request.app.state.config
    try:
        query = validate_query(q, config)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    
    search = request.app.state.search
    if search is None:
        raise HTTPException(status_code=503, detail="Search service is not available")
    return await search.imdb_suggestions(query)


@search_router.get("/api/search")
@rate_limited(cost=1.0)
async def search_route(
    request: Request,
    q: str,
    mode: Optional[str] = "",
    quality: Optional[str] = "",
    encoders: Optional[str] = ""
):
    config = request.app.state.config
    try:
        query = validate_query(q, config)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    search = request.app.state.search
    if search is None:
        raise HTTPException(status_code=503, detail="Search service is not available")

    mode = mode.strip().lower()
    provider_order = ("apibay", "bitsearch", "torrents-csv") if mode == "series" else None

    locked = {"provider": None}
    provider_attempts: list[dict] = []
    provider_fallback = {"mode": None}

    def _relevant(r):
        info = parse_release(str(r.get("name", "")))
        return matches_query(query, info["series"], is_episode=info["episode"] is not None)

    def _tokens(value):
        tokens = [t for t in re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split() if t]
        if len(tokens) > 1 and tokens[-1] in {"us", "uk", "ca", "au", "nz"}:
            tokens.pop()
        return tokens

    def _without_articles(tokens):
        return [t for t in tokens if t not in {"the", "a", "an"}]

    query_core = _without_articles(_tokens(query))

    def _series_primary_relevant(row):
        info = parse_release(str(row.get("name", "")))
        if matches_query(query, info["series"], is_episode=info["episode"] is not None):
            return True
        series_core = _without_articles(_tokens(info["series"]))
        return bool(query_core) and series_core == query_core

    def _series_loose_relevant(row):
        info = parse_release(str(row.get("name", "")))
        series_core = _without_articles(_tokens(info["series"]))
        return bool(query_core) and all(t in series_core for t in query_core)

    # --- Series Mode v2: targeted queries (packs + per encoder×quality) ---
    if mode == "series":
        qualities_list = [x for x in _csv(quality or "1080p") if x in ALLOWED_QUALITIES]
        if not qualities_list:
            qualities_list = ["1080p"]
        encoders_list = [e for e in _csv(encoders) if e in PRESET_ENCODERS]

        planned = 1 + (2 * len(qualities_list)) + (len(encoders_list) * len(qualities_list))
        if planned > SERIES_MAX_REQUESTS:
            raise HTTPException(
                status_code=400,
                detail=f"This selection needs {planned} searches (limit {SERIES_MAX_REQUESTS}). Reduce the number of qualities or encoders."
            )

        series_less_relevant: list[dict] = []
        series_other: list[dict] = []

        def _remember_series_fallback(raw_rows, primary_rows):
            primary_hashes = {str(r.get("infohash", "")).lower() for r in primary_rows}
            for row in raw_rows:
                ih = str(row.get("infohash", "")).lower()
                if ih and ih in primary_hashes:
                    continue
                info = parse_release(str(row.get("name", "")))
                if _series_loose_relevant(row):
                    series_less_relevant.append(row)
                elif not info.get("parsed") or not _series_primary_relevant(row):
                    series_other.append(row)

        async def series_round_search(query_text):
            order = [locked["provider"]] if locked["provider"] else list(provider_order or search._provider_order())
            first_raw_provider = None
            first_raw_rows: list[dict] = []
            for provider in order:
                raw_rows = _dedup_by_infohash(await search._run_provider(provider, query_text))
                primary_rows = [r for r in raw_rows if _series_primary_relevant(r)]
                less_count = sum(1 for r in raw_rows if r not in primary_rows and _series_loose_relevant(r))
                other_count = max(0, len(raw_rows) - len(primary_rows) - less_count)
                provider_attempts.append({
                    "provider": provider,
                    "raw": len(raw_rows),
                    "filtered": len(primary_rows),
                    "less_relevant": less_count,
                    "other": other_count,
                })
                if raw_rows and first_raw_provider is None:
                    first_raw_provider = provider
                    first_raw_rows = raw_rows
                if primary_rows:
                    if locked["provider"] is None:
                        locked["provider"] = provider
                    _remember_series_fallback(raw_rows, primary_rows)
                    return primary_rows
            if locked["provider"] is None and first_raw_provider is not None:
                locked["provider"] = first_raw_provider
                if provider_fallback["mode"] is None:
                    provider_fallback["mode"] = "other"
                _remember_series_fallback(first_raw_rows, [])
            return []

        broad_rows = await series_round_search(query)

        pack_rows = list(broad_rows)
        for ql in qualities_list:
            pack_rows += await series_round_search(f"{query} {ql} x265")
            pack_rows += await series_round_search(f"{query} {ql} hevc")
        pack_rows = _dedup_by_infohash(pack_rows)
        packs = build_packs(pack_rows)

        enc_rows = list(broad_rows)
        for enc_name in encoders_list:
            for ql in qualities_list:
                enc_rows += await series_round_search(f"{query} {ql} {enc_name}")
        enc_rows = _dedup_by_infohash(enc_rows)

        selected_enc = {_normalize_encoder(e) for e in encoders_list}
        if selected_enc:
            enc_rows = [r for r in enc_rows if r.get("encoder_norm", "") in selected_enc]

        existing = {p.get("infohash") for p in packs}
        extra_packs = [p for p in build_packs(enc_rows, top_n=10_000) if p.get("infohash") not in existing]
        for ep in extra_packs:
            if len(packs) < 20:
                packs.append(ep)
            elif (ep.get("size_bytes", 0) or 0) < (packs[-1].get("size_bytes", 0) or 0):
                packs[-1] = ep
            packs.sort(key=lambda r: r.get("size_bytes", 0) or 0)
        packs = packs[:20]

        groups = group_series_results(enc_rows)
        main_hashes = {str(r.get("infohash", "")).lower() for r in packs + enc_rows if r.get("infohash")}
        series_less_relevant = [r for r in _dedup_by_infohash(series_less_relevant) if str(r.get("infohash", "")).lower() not in main_hashes]
        less_hashes = {str(r.get("infohash", "")).lower() for r in series_less_relevant if r.get("infohash")}
        series_other = [
            r for r in _dedup_by_infohash(series_other)
            if str(r.get("infohash", "")).lower() not in main_hashes and str(r.get("infohash", "")).lower() not in less_hashes
        ]
        return {
            "success": True,
            "mode": "series",
            "packs": packs,
            "encoders": groups["encoders"],
            "less_relevant": series_less_relevant,
            "other": series_other,
            "stats": groups["stats"],
            "requests_used": len(provider_attempts),
            "provider": locked["provider"],
            "provider_attempts": provider_attempts,
            "provider_fallback": provider_fallback["mode"],
            "qualities": qualities_list,
            "encoders_selected": encoders_list,
        }

    # --- Normal Mode: ONE broad query -> filter (quality + encoder) -> quality sections ---
    selected_encoders = {
        e for e in (_normalize_encoder(x) for x in _csv(encoders))
        if e
    }
    qualities_list = [x for x in _csv(quality) if x in ALLOWED_QUALITIES]
    wanted_qualities = set(qualities_list)

    def _normal_filter(row):
        if selected_encoders and row.get("encoder_norm", "") not in selected_encoders:
            return False
        if wanted_qualities and _quality_bucket(str(row.get("name", ""))) not in wanted_qualities:
            return False
        return True

    chosen_provider = None
    matched_rows: list[dict] = []
    less_relevant_rows: list[dict] = []
    fallback_mode = None
    first_less_provider = None
    first_less_rows: list[dict] = []

    for provider in search._provider_order():
        raw_rows = _dedup_by_infohash(await search._run_provider(provider, query))
        eligible = [r for r in raw_rows if _normal_filter(r)]
        relevant = [r for r in eligible if _relevant(r)]
        less = [r for r in eligible if not _relevant(r)]
        provider_attempts.append({
            "provider": provider,
            "raw": len(raw_rows),
            "eligible": len(eligible),
            "filtered": len(relevant),
            "less_relevant": len(less),
        })
        if eligible and first_less_provider is None:
            first_less_provider = provider
            first_less_rows = eligible
        if relevant:
            chosen_provider = provider
            matched_rows = relevant
            less_relevant_rows = less
            break

    if chosen_provider is None and first_less_provider is not None:
        chosen_provider = first_less_provider
        less_relevant_rows = first_less_rows
        fallback_mode = "less_relevant"

    locked["provider"] = chosen_provider
    quality_groups = group_by_quality(_dedup_by_infohash(matched_rows), only_qualities=qualities_list or None, cap=None)
    if less_relevant_rows:
        less_relevant_rows = _dedup_by_infohash(less_relevant_rows)
        less_relevant_rows.sort(key=lambda r: r.get("size_bytes", 0) or 0)
        quality_groups.append({
            "quality": "less_relevant",
            "label": "Less relevant",
            "count": len(less_relevant_rows),
            "rows": less_relevant_rows,
        })
    return {
        "success": True,
        "mode": "normal_grouped",
        "quality_groups": quality_groups,
        "provider": locked["provider"],
        "provider_attempts": provider_attempts,
        "provider_fallback": fallback_mode,
    }
