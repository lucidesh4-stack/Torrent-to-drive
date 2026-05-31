# Streamly Project State

> This is the Single Source of Truth (SOT) for the agent. Read this first in every new chat.

---

## ⚡ Quick Reference
- **What**: Flask app — Seedr client + torrent search + custom video player + history.
- **Deploy**: Docker on Render (free tier).
- **Persistence**: Upstash Redis.
- **ZRP**: Zero-Regression Protocol is **ACTIVE** and mandatory.
- **SOT**: This file (`STATE.md`) replaces QUICK, CONTEXT, CHANGELOG, and ACTIVITY_LOG.

---

## 📖 Project Context

### User Journey
1. Login with Seedr account (or Guest mode).
2. **Search**: Title $\rightarrow$ bitsearch.eu $\rightarrow$ Add $\rightarrow$ Seedr Cloud.
3. **Cloud Drive**: Browse, stream, download/zip, delete.
4. **History**: Global magnet history stored in Upstash Redis.

### Architecture
- **Backend**: Flask Blueprints (`auth`, `cloud`, `search`, `history`) + decoupled services (`cloud_service`, `search_service`).
- **Frontend**: Generated `app.js` from `src/` fragments.
- **Session**: In-process `TTLStore`.

### User Preferences
- **Workflow**: Describe $\rightarrow$ Plan $\rightarrow$ Approve $\rightarrow$ Implement.
- **ZRP**: Path trace, Type check, Dependency audit, Side-effect mapping.
- **Format**: Summarized answers, no code in chat, update workspace files only.

---

## 🛠️ Current State

### Active Work
- **Status**: Architecture refactor and stability hardening completed.
- **Pending**:
    1. Logging system (Proposed).
    2. JS Namespacing (Proposed).
    3. Typed API responses (Proposed).
    4. Redis Session Store (Proposed).

### Known Tech Debt
1. Duplicate `init()` in `1-core.js` and `6-main.js`.
2. Dead code `updateSelected()` in `2-cloud.js`.
3. In-process session store (logout on multi-worker).
4. Bitsearch rate limits.

---

## 📜 Decision Ledger

### 2026-05-31 — Fix: size-desc default sort wasn't applied
- **Bug**: Backend `group_by_quality` pre-sorted size-desc, but the frontend `renderNormalGrouped` always re-sorted via `sortRows` using defaults `currentSort="seeders"`, so the first render showed seeds-order, not size-desc.
- **Fix**: Changed JS defaults to `currentSort="size"`, `currentOrder="desc"` (1-core.js) so the initial view matches the spec. Clicking SE/Time/Size still re-sorts client-side.
- **Files**: static/js/src/1-core.js, app.js (rebuilt).
- **Verified**: default render now 9 GB→2 GB→500 MB; gunicorn boots.

### 2026-05-31 — Mobile UI fix for series/quality sections
- **Bug**: New section rows used a fixed 5-col grid (`1fr 80px 110px 100px 90px`); on phones the Name column collapsed and the Add button + full size were clipped (showed "2.18" with no GB, no name, no Add).
- **Fix**: Added `@media (max-width:768px)` overrides in responsive.css — `.episode-row` becomes a wrapping flex card (Name full-width on top; seeds·size·time·Add below with Add right-aligned); section/uploader headers wrap; `#seriesResults` padding tightened; Quality/Encoder dropdown buttons sized to compact mobile controls with on-screen panels.
- **Files**: static/css/responsive.css (CSS-only). app.js rebuilt.
- **Verified**: CSS brace-balanced; node --check; gunicorn boots. Desktop layout unchanged (overrides are mobile-only).

### 2026-05-31 — Normal mode = quality-grouped + identical control row; meter removed
- **Identical row**: Quality + Encoder dropdowns now always visible in BOTH modes. The Normal/Series toggle ONLY changes backend processing (no show/hide).
- **Normal mode redesign**: one bitsearch per selected quality (default 1080p) → `<title> <q>` → dedup → grouped into **quality sections (4K → 1080p → 720p → Other)**, plain torrent rows, **size-descending** by default. Multi-quality = multi-query. Encoders ignored in Normal.
- **Client-side sort**: clicking SE/Time/Size now re-orders the already-loaded rows **within each section** (no re-fetch, no quota). Previously it re-ran the search.
- **Removed the daily meter entirely** (UI strip + RedisStore incr/get_request_count + config bitsearch_daily_limit + app.config export + CSS). Series request badge text retained in resultCount line only.
- **Files**: search_service.py (+`group_by_quality`,`_quality_bucket`), routes/search.py (Normal multi-query branch; meter code removed), redis_store.py (counter removed), config.py (limit removed), app.py (export removed), static/js/src/3b-series.js (rewrite: normal grouped render, sortRows, no meter), 5-search.js (normal_grouped handling, always send quality), 3-search-sort.js (cycleSort client-side), 6-main.js (dropdowns always visible), templates/index.html (meter removed, dropdowns un-hidden), static/css/base.css (meter CSS removed).
- **Verified**: Normal → normal_grouped sections 4K→1080p→720p, size-desc; multi-quality multi-query; Series intact (no daily_used); no stale meter refs; py_compile + node --check; gunicorn boots.

### 2026-05-31 — 413 add fix + Series/Normal row parity
- **413 fix**: `cloud_service.add_magnet` now catches `seedrcc.APIError` / HTTP 413 ("too large") and re-raises as `ConnectionError` with a clear message; route returns **502 "too large for your available space"** instead of an uncaught 500. Unrelated errors still propagate.
- **UI parity**: `.ms-dd-btn` (Quality/Encoder dropdowns) now match the Category `<select>` metrics exactly (font 16px, padding 12px 13px) so the Series Mode control row is the same height/look as Normal.
- **Files**: cloud_service.py, routes/cloud.py, static/css/base.css, app.js (rebuilt).
- **Verified**: APIError/413 → ConnectionError (clear msg); unrelated error re-raised; py_compile + node --check; gunicorn boots.

### 2026-05-31 — UI tweaks + Bitsearch daily meter
- **UI**: Quality & Encoder moved into the search row as custom multi-select dropdowns (button + checkbox panel). Removed the "Remove duplicates" checkbox → dedup is now **hard-wired ON** (Normal + Series).
- **Daily meter**: every bitsearch call increments an Upstash daily key `streamly:bitsearch_count:<UTC-date>` (48h TTL). Series response returns `daily_used` + `daily_limit`. UI shows a traffic-light "Bitsearch: X / 200 today" (green <70%, yellow ≥70%, red ≥90%) as an early-warning before the limit. Limit configurable via `BITSEARCH_DAILY_LIMIT` (default 200).
- **Files**: redis_store.py (incr/get_request_count), config.py (bitsearch_daily_limit), app.py (export to app.config), routes/search.py (count per call + return meter), static/js/src/3b-series.js, 5-search.js, 6-main.js, templates/index.html, static/css/base.css, app.js.
- **Verified**: meter increments across searches (3→6), limit exposed; dropdowns build clean; dedup hard-wired ON (regression); no stale dedup/seriesControls refs; py_compile + node --check pass; gunicorn boots.
- **Note**: 413→500 add bug explained (not fixed); size filter dropped per request.

### 2026-05-31 — Feature ③ v2: Series Mode redesign (targeted queries)
- **What**: Backend-driven, quota-bounded. Quality multi-select (4K/1080p/720p, 1080p default) + encoder multi-select (presets: ELiTE, PSA, MeGusta).
- **Packs**: per quality → `<title> <q> x265` + `<title> <q> hevc` (sort size desc, 1 page) → dedup → packs-only → smallest-first → top 20. Non-packs discarded. Qualifying packs found in encoder results replace the largest in top-20.
- **Encoders**: per encoder×quality → `<title> <q> <ENCODER>` (1 page) → dedup → group **encoder→uploader→quality→season→episode**. Unparseable discarded.
- **Quota guard**: requests = (2×Q) + (N×Q); hard cap 12 (route returns 400 before any call); UI badge "uses N request(s)".
- **Uploader**: bracket/site tag (eztv.re/TGx/…), else "Unknown".
- **Files**: search_service.py (added `_extract_uploader`, `build_packs`, `_pack_label`; restructured `group_series_results` with uploader level), routes/search.py (mode=series multi-query orchestration + guard), static/js/src/3b-series.js (rewrite), 5-search.js, 6-main.js, templates/index.html, static/css/base.css, app.js.
- **Verified**: route tests (correct queries, requests_used=4 for 1080p×2enc, packs smallest-first + migration, uploader split eztv.re/TGx, guard 400 with 0 calls); Normal regression intact; py_compile + node --check pass; gunicorn boots.

### 2026-05-31 — Feature ③: Series Mode (grouped view)
- **What**: Toggle [Normal][Series Mode] above results. Series Mode fetches 3 pages (~150), dedups, parses each title, groups Encoder→Quality→Season→Episode. Season Packs + Other in separate sections (never drop unparseable). Films/Normal unchanged.
- **Add semantics**: per-episode Add = normal single add. "+ Add all N" = ONE Seedr add (episode 1) + ALL N saved to History individually (avoids 413 quota storm). Packs/Other = per-row Add only.
- **Parsing**: loose encoder normalize (uppercase + strip non-alphanumeric, no fuzzy); site tags (EZTV/TGx/etc.) excluded as encoders; quality = resolution + codec/source. Non-standard (1x01, Ep01, anime) → Other.
- **Backend**: pure `parse_release`, `_normalize_encoder`, `group_series_results` in search_service; `/api/search?mode=series` branch in routes/search (3-page loop → dedup → group). Reuses Feature ① dedup (fixed seeders/seeds key mismatch).
- **Files**: search_service.py, routes/search.py, static/js/src/3b-series.js (new), 5-search.js, 6-main.js, templates/index.html, static/css/base.css, app.js (rebuilt, 7 fragments).
- **Verified**: parse/group unit tests (S/E, packs, encoders, quality, Loki cases); route tests (3 pages fetched, dedup applied, Normal regression intact); JS `node --check` passes; gunicorn boots.

### 2026-05-31 — Feature ①: Search dedup (infohash, highest-seeds)
- **What**: Collapse same-infohash duplicates in search results, keeping the highest-seeded copy. ON by default with a "Remove duplicates" checkbox; toggling re-runs search only if results already shown (no wasted quota).
- **Design**: Pure `_dedup_by_infohash()` in search_service; `bitsearch(dedup=True)` applies to results only — pagination totals (upstream dataset) left untouched. `/api/search?dedup=0` disables; absent ⇒ ON.
- **Files**: search_service.py, routes/search.py, static/js/src/5-search.js, static/js/src/6-main.js, templates/index.html, static/css/base.css, static/js/app.js (rebuilt).
- **Verified**: unit tests (highest-seed rep, case-insensitive hash, blank/missing-hash passthrough, order preserved); route tests (dedup 1/0/absent, pagination intact); gunicorn boots.
- **Part of**: Dedup + Series Mode + Size filter (3-change rollout). Next: ② size filter, ③ series mode.

### 2026-05-31 — Logs to Upstash Redis (reliable /api/logs)
- **Why**: Render disk is ephemeral — the old `RotatingFileHandler` + `/api/logs` file download was unreliable (404 / partial after restarts).
- **What**: Added `RedisLogHandler` (capped Redis list `streamly:logs`, last 2000 lines via LPUSH+LTRIM). `/api/logs` POST now serves logs from Redis. Removed disk file handler.
- **Safety**: Handler never raises; skips `redis_store` records + re-entrancy guard (no infinite logging loop). Redis init moved before logging setup. No-Redis → 503.
- **Files**: streamly_hardened/redis_store.py, streamly_hardened/app.py
- **Verified**: gunicorn boot OK (Dockerfile module path); write→download flow returns recent lines chronologically; loop-guard confirmed; wrong creds 403, no-redis 503.

### 2026-05-31 — Deploy Crash Fix: RequestIDFilter app-context safety
- **Bug**: `RuntimeError: Working outside of application context` at boot → gunicorn "Worker failed to boot" → Render deploy exit 1.
- **Cause**: `RequestIDFilter.filter` read `g` (request-only); boot-time Redis health-check log fired with no app context.
- **Fix**: Wrap `g.get("request_id", ...)` in try/except RuntimeError, fallback to "system".
- **Files**: streamly_hardened/app.py
- **Verified**: gunicorn boot with Upstash env vars set (prev. crash condition) now succeeds; in-request logging unchanged.

### 2026-05-31 — 2026-05-31 — Secure Logging System Implementation
- Files: streamly_hardened/app.py, ai/deploy/check.py


### 2026-05-31 — 2026-05-31 — Secure Logging System Implementation
- Files: streamly_hardened/app.py, ai/deploy/check.py


### 2026-05-31 — 2026-05-31 — Secure Logging System Implementation
- Files: streamly_hardened/app.py, ai/deploy/check.py


### 2026-05-31 — 2026-05-31 — Secure Logging System Implementation
- Files: streamly_hardened/app.py, ai/deploy/check.py


### 2026-05-31 — Protocol Adoption & Workspace Hardening
- **Deterministic Development**: Adopted Zero-Regression Protocol (ZRP).
- **AI Optimization**: Consolidated fragmented docs into `STATE.md`.
- **Workspace**: Purged `__pycache__` and temp files.

### 2026-05-31 — Security and Reliability Fixes
- **Storage Guard**: Mandatory check in `/api/add` to prevent over-filling Seedr.
- **Token Guard**: `RedisStore` rejects empty refresh tokens.
- **Redis Health**: Boot-time connectivity check for Upstash.
- **Exceptions**: Replaced broad `except Exception` with `(ConnectionError, TimeoutError)`.
- **Error Safety**: Generic messages to prevent internal leak.

### 2026-05-31 — Architecture Refactor
- **Blueprints**: Split `app.py` into `routes/` blueprints.
- **Services**: Split `services.py` into `cloud_service.py` and `search_service.py`.
- **Polyfill**: `_get_cfg` in `security.py` handles diverse config objects.

---

## 🚀 Deployment Activity
[2026-05-31] Protocol Adoption & Workspace Cleanup — ai/QUICK.md
[2026-05-31] Code quality: magic numbers extracted, route docstrings added, no behavior change
[2026-05-31] Security and reliability fixes (initial batch)
[2026-05-31] Security and reliability fixes (initial batch)
[2026-05-31] Initial fix batch: storage check, empty token guard, Redis health check, specific exception handlers, safe error messages.

[2026-05-31] 2026-05-31 — Secure Logging System Implementation — streamly_hardened/app.py, ai/deploy/check.py


## 🔄 Recent Changes
- **2026-05-31** — Mobile UI fix: series/quality section rows reflow to a 2-line card (Name on top; seeds·size·Add below) so nothing is clipped on phones; dropdowns sized for mobile. Changed: static/css/responsive.css, app.js.
- **2026-05-31** — Normal mode now quality-grouped (4K/1080p/720p, size-desc, default 1080p, multi-query); control row identical in both modes; sorting is client-side (no re-fetch); removed daily meter entirely. Changed: search_service.py, routes/search.py, redis_store.py, config.py, app.py, 3b-series.js, 5-search.js, 3-search-sort.js, 6-main.js, index.html, base.css, app.js.
- **2026-05-31** — Fixed 413 add_torrent → was 500, now clean 502 "too large"; made Series dropdown buttons match Category select so Series/Normal rows look identical. Changed: cloud_service.py, routes/cloud.py, base.css, app.js.
- **2026-05-31** — UI: quality/encoder multi-select dropdowns in search row; removed dedup checkbox (dedup always ON); added Upstash daily bitsearch meter with green/yellow/red early-warning (X/200 today, configurable). Changed: redis_store.py, config.py, app.py, routes/search.py, 3b-series.js, 5-search.js, 6-main.js, index.html, base.css, app.js.
- **2026-05-31** — Feature ③ v2 Series Mode redesign: quality+encoder multiselect, targeted queries (packs x265/hevc + per encoder×quality), encoder→uploader→quality→season→episode, packs smallest-first top-20, quota guard (cap 12 + badge). Changed: search_service.py, routes/search.py, static/js/src/3b-series.js, 5-search.js, 6-main.js, templates/index.html, static/css/base.css, app.js.
- **2026-05-31** — Feature ③ Series Mode: [Normal][Series Mode] toggle; grouped Encoder→Quality→Season→Episode (3-page fetch); "Add all"=1 Seedr add+N history; Packs/Other sections. Changed: search_service.py, routes/search.py, static/js/src/3b-series.js (new), 5-search.js, 6-main.js, templates/index.html, static/css/base.css, app.js.
- **2026-05-31** — Feature ① Search dedup (same-infohash → keep highest-seeded); default-on + "Remove duplicates" checkbox. Changed: search_service.py, routes/search.py, static/js/src/5-search.js, 6-main.js, templates/index.html, static/css/base.css, app.js.
- **2026-05-31** — Logging now persists to Upstash Redis (capped 2000 lines); `/api/logs` serves logs from Redis; disk file handler removed. Changed: streamly_hardened/redis_store.py, streamly_hardened/app.py.
- **2026-05-31** — Deploy crash fix: made `RequestIDFilter` context-safe (no more boot-time `RuntimeError: working outside of application context`). Changed: streamly_hardened/app.py.
- **2026-05-31** — 2026-05-31 — Secure Logging System Implementation. Changed: streamly_hardened/app.py, ai/deploy/check.py.
