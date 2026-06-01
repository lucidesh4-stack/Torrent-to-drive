# CloudFlow Project State

> This is the Single Source of Truth (SOT) for the agent. Read this first in every new chat.

---

## ⚡ Quick Reference
- **What**: CloudFlow — Flask app — Seedr client + torrent search + custom video player + history.
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

### 2026-06-01 — UI/docs rename: CloudFlow
- **What**: Renamed visible app branding from Streamly/Streamly Hardened to **CloudFlow** in UI and docs only.
- **Scope**: Browser title, login brand/copy, log access page title, DEPLOY.md, and STATE.md. Internal Python package/folder names remain `streamly_hardened` to avoid import/deploy risk.
- **Files**: templates/index.html, app.py, DEPLOY.md, render.yaml, ai/STATE.md, project.zip.
- **Verified**: py_compile, app.js unchanged/valid, Flask test client served index/static assets.

### 2026-06-01 — High-confidence dead-weight cleanup
- **What**: Removed obsolete flat search table/pagination path and stale Add-all code after grouped search/mobile redesign. Deleted `addAllEpisodes`, `renderPagination`, `renderSearchTable`, old `#results/#torrentBody/#mobileResults/#pagination` DOM, old pagination/mobile-card/table CSS, and stale `.section-add` styles.
- **Backend cleanup**: Removed obsolete category/sort/order validators and config allowlists, unused `bitsearch_api_key`, unused `seedr_archive_url`, unused `max_bulk_items`, and unused imports (`dataclass` in cloud_service, `CloudService` in routes/cloud, `session`/`require_json_body` in app).
- **Preserved**: Provider failover/Bitsearch code, compatibility `userPill`, storage null guards, and other non-high-confidence items were left intact.
- **Files**: templates/index.html, static/js/src/3b-series.js, static/js/src/5-search.js, static/js/app.js, static/css/base.css, static/css/responsive.css, app.py, config.py, security.py, cloud_service.py, routes/cloud.py.
- **Verified**: removed-symbol grep clean; app.js rebuilt; node --check; py_compile; CSS brace balance; Flask test client served index/static assets.

### 2026-06-01 — Clipboard magnet re-check on focus/visibility/Search tab
- **What**: Improved magnet clipboard detection so newly copied magnets are picked up after returning to Streamly. The frontend now schedules debounced clipboard checks on Search tab click, window focus, page visibility restore, and pointer interaction while Search is visible.
- **Safety**: Checks are Search-tab gated and debounced (1.2s). Existing same-page and 24h localStorage duplicate guards still prevent repeat auto-adds.
- **Files**: static/js/src/1-core.js, static/js/src/5-search.js, static/js/src/6-main.js, static/js/app.js.
- **Verified**: app.js rebuilt; node --check; py_compile; CSS brace balance; Flask test client served index/static assets.

### 2026-06-01 — Persistent duplicate guard for magnet auto-add
- **What**: Hardened clipboard/paste/URL magnet auto-add with a browser-local duplicate guard. The frontend extracts the `btih` infohash and stores `streamly:autoAddedMagnet:<hash>` in `localStorage` for 24 hours.
- **Behavior**: If the same infohash is detected again within 24 hours, Streamly fills/keeps the magnet in the search bar but does not auto-add. It shows `Magnet already auto-added recently. Tap Add to force add again.` Manual Add still works and refreshes the timestamp.
- **Scope**: Frontend-only; no backend/API changes. Same-page in-memory guard remains, localStorage adds refresh/new-tab protection.
- **Files**: static/js/src/1-core.js, static/js/src/5-search.js, static/js/app.js.
- **Verified**: app.js rebuilt; node --check; py_compile; CSS brace balance; Flask test client served index/static assets.

### 2026-06-01 — Frontend magnet auto-ingest: clipboard, paste, URL
- **What**: Added frontend-only magnet ingest. Search page detects magnet links from clipboard, paste/input, `?magnet=`, hash param `#magnet=`, and direct hash text. Detected magnets fill the search box and trigger the existing add flow automatically.
- **Clipboard/Paste**: On Search tab init and after authenticated status, Streamly attempts clipboard read; if clipboard contains a valid magnet, it auto-pastes and auto-adds. Paste button and manual paste also auto-add. Duplicate guard prevents repeatedly adding the same magnet in the same page session.
- **URL cleanup**: `?magnet=`/hash magnet URLs are cleaned with `history.replaceState()` after detection to avoid repeat-add on refresh.
- **Files**: static/js/src/1-core.js, static/js/src/5-search.js, static/js/src/6-main.js, static/js/app.js.
- **Verified**: app.js rebuilt; node --check; py_compile; CSS brace balance; Flask test client served index/static assets.

### 2026-06-01 — Mobile Search V2 polish: overlay, stable sections, denser rows
- **What**: Fixed regressions from Mobile Search V2. Suggestion overlay is no longer clipped on mobile. Sorting header clicks and quality/season navigation preserve open sections instead of collapsing everything.
- **UI**: Restyled global quality navigation to match season chips (small pill style instead of segmented bar). Reduced mobile result vertical space via tighter section headers, rows, metadata, season chips, and smaller add buttons.
- **State preservation**: Mobile/desktop renderers now capture open accordion keys before re-rendering and restore matching sections after sort/quality/season changes.
- **Files**: static/js/src/3b-series.js, static/css/responsive.css, static/js/app.js.
- **Verified**: app.js rebuilt; node --check; py_compile; CSS brace balance; Flask test client served index/static assets.

### 2026-06-01 — Cancel active Seedr transfers from Cloud Drive
- **What**: Added transfer cancellation for active Seedr torrents. Backend endpoint `POST /api/transfer/cancel` validates the transfer id and calls `CloudService.delete_transfer()` -> `client.delete_torrent(str(id))`.
- **UI**: Active transfer rows/cards now show a `Cancel` button on desktop and mobile. Cancel confirms, calls the endpoint, refreshes current Cloud Drive, and shows status/toast. Completed files/folders still use existing delete flow.
- **Files**: cloud_service.py, routes/cloud.py, static/js/src/2-cloud.js, base.css, responsive.css, app.js.
- **Verified**: service harness (`delete_transfer` string id); app.js rebuilt; node --check; py_compile; CSS brace balance; Flask test client served index/static assets.

### 2026-06-01 — Cloud Drive shows active Seedr transfers with progress
- **What**: `/fs/folder/<id>/items` now includes active Seedr torrents from `contents.torrents` as `transfers[]`, with name, size, progress %, status, download rate, seeders, and formatted size/rate fields. If `progress_url` is available, the backend enriches progress via `client.get_torrent_progress()`.
- **UI**: Desktop Cloud Drive renders transfer rows at the top with a progress bar. Mobile Cloud Drive renders loading transfer cards with progress bar/meta. Transfers are not selectable/deletable as normal files.
- **Refresh**: Cloud Drive auto-refreshes every 5s while active transfers are visible; polling stops when leaving Cloud Drive. Add-to-Seedr success refreshes Cloud Drive if visible, otherwise refreshes storage snapshot only.
- **Files**: cloud_service.py, routes/cloud.py, static/js/src/1-core.js, static/js/src/2-cloud.js, static/js/src/5-search.js, static/js/src/6-main.js, base.css, responsive.css, app.js.
- **Verified**: fake Seedr transfer harness; app.js rebuilt; node --check; py_compile; CSS brace balance; Flask test client served index/static assets.

### 2026-06-01 — Mobile Search V2 implementation: compact controls + global quality nav
- **What**: Implemented the approved mobile search layout. Mobile now hides the Torrent Search title/subtitle, keeps the search input text visible, uses a compact `[input]+[search]` row, then `Filters` + `Normal|Series` segmented control, then result count.
- **Global quality navigation**: Added mobile-only `4K | 1080p | 720p` segmented navigation under the `Name | SE | Time | Size | +` header. It navigates one returned quality at a time for both Normal/movie and Series results. Quality selection remains in Filters and controls which qualities are searched.
- **Series browsing**: Series mode removes per-encoder quality accordions on mobile; selected global quality is applied across packs/encoders. Open encoder shows encoder title, season chips (`S1..S8`), and only the active season episodes. Season labels are hidden on mobile.
- **No Add all**: Removed generated section/quality Add-all buttons completely; only per-row `+` remains.
- **Filters**: Added mobile bottom-sheet filters with quality + encoder checkboxes, synced to existing desktop dropdown checkboxes before search.
- **Files**: templates/index.html, static/js/src/3b-series.js, static/js/src/5-search.js, static/js/src/6-main.js, static/css/responsive.css, static/js/app.js.
- **Verified**: app.js rebuilt; node --check; py_compile; CSS brace balance; Flask test client served index/static assets.

### 2026-06-01 — Compact desktop cloud UI + eager storage snapshot
- **What**: Tightened desktop topbar storage into a compact single-line header with a thinner meter; reduced desktop cloud side-card width/padding, metric spacing, and action button size so Open/Download/Copy Link/Delete/Clear Selection no longer appear oversized.
- **Storage load fix**: Added `refreshStorageSnapshot()` to fetch `/fs/folder/0/items` after authenticated startup on the Search tab, updating only storage usage without switching tabs or rendering Cloud Drive. Existing Cloud Drive loads still use `loadFolder()`.
- **Safety**: Snapshot fetch is auth-gated, deduped by `storageSnapshotLoading/storageSnapshotLoaded`, silent on failure, and does not mutate `items/currentFolder/selectedKeys`.
- **Files**: static/css/base.css, static/js/src/2-cloud.js, static/js/src/6-main.js, static/js/app.js, ai/STATE.md, project.zip.
- **Verified**: app.js rebuilt; node --check; py_compile; CSS brace balance; Flask test client served index/static assets after installing deps in excluded .venv.

### 2026-06-01 — Desktop cloud UI: storage topbar + copy-link action
- **What**: Desktop topbar no longer shows the Streamly Hardened title or right-side email pill. The left brand area now shows Storage usage with a compact meter and account text (`Connected to <email>`). The desktop cloud side-card no longer contains the Storage block.
- **Copy Link**: Added desktop `Copy Link` action. File selection copies `/api/url`; single folder copies `/api/zip`; multi-select copies `/api/zip/bulk`. Existing mobile context-menu Copy Link remains unchanged.
- **Safety**: Removed desktop storage DOM references are now null-guarded (`pathLabel`, `storageMeter`, `storageText`). `userPill` remains hidden for compatibility with silent-login fallback.
- **Files**: templates/index.html, static/css/base.css, static/js/src/1-core.js, static/js/src/2-cloud.js, static/js/src/6-main.js, static/js/app.js.
- **Verified**: app.js rebuilt; node --check; py_compile; CSS brace balance; DOM id contract spot-check; Flask test client served index/static assets after installing deps in excluded .venv.

### 2026-05-31 — Series mode: add broad <title> query, merge into packs + episodes
- **What**: Series search now runs a single broad `<title>`-only query FIRST, then the existing packs (`<title> <q> x265|hevc`) and encoder (`<title> <q> <ENCODER>`) queries. Broad results are MERGED into both the packs set and the episode set, then deduped and grouped as before. Helps catch episodes/packs the narrow queries miss.
- **Quota**: broad query is counted -> planned = 1 + 2*Q + N*Q (cap 12). (e.g. 3q×3enc = 16 -> blocked.)
- **Encoder filter**: the broad query returns ALL release groups; if the user ticked encoders, the merged episode set is filtered to those `encoder_norm` (none ticked => keep all encoders found).
- **Unchanged**: grouping (packs + encoder->quality->season->episode), relevance filter (episodes exact -> spin-offs dropped), dedup, original pack names, response shape, frontend.
- **Files**: routes/search.py (series branch only).
- **Verified**: route harness (broad fires first + counted; requests_used=4 for 1080p×ELiTE; broad-only episode E05 appears; broad pack merged; encoder filter drops PSA; quota 16>12 -> 400; spin-offs filtered); py_compile; gunicorn boots; app.js untouched.


### 2026-05-31 — Normal mode = one broad query -> quality+encoder FILTER -> quality sections
- **What**: simplified Normal mode per user. ONE broad apibay query for the title (was: one query per ticked quality). Then filter the result set:
  - **Quality** = which sections appear (ticked => only those; none ticked => all incl. Other).
  - **Encoder** = filter release groups (ticked => only those, case-insensitive; none => all).
  - Each section sorted **size-ascending**, **no cap** (keep all matching).
- **Rows now carry `encoder`/`encoder_norm`** (added to `_make_row`) so encoder filtering needs no re-parse.
- **`group_by_quality(rows, only_qualities, cap)`** gained filter params (default-compatible).
- **Frontend**: Normal mode now also sends `encoders=`; renderer unchanged.
- **BONUS BUGFIX (important)**: the exact-match relevance filter was rejecting ALL movies — for a movie (no SxxExx) the parser returns the whole filename as "series" (title+year+quality+codec+group), so exact match never hit -> Normal mode would show nothing for movies. Fixed `matches_query(query, series, is_episode=...)`:
  - episodes -> EXACT match (still drops spin-offs: The Boys Presents Diabolical, Daredevil Born Again),
  - movies/packs -> PREFIX match (tolerant of trailing year/quality/group junk).
- **Files**: search_service.py (_make_row encoder fields, group_by_quality params, matches_query is_episode), routes/search.py (Normal branch rewrite: single query + encoder filter + quality-section filter; round_search passes is_episode), static/js/src/5-search.js (send encoders in normal), app.js rebuilt.
- **Verified**: unit (movie prefix vs episode exact, 10 cases; encoder field; quality-filter sections; size-asc; no cap); route harness (one call; all/filtered sections; encoder filter; different-movie dropped); py_compile; node --check; gunicorn boots; app.js in sync.


### 2026-05-31 — Fix: exact-match filter broke when query contained quality/noise
- **Bug**: after switching to exact title match, typing "the boys 1080p" compared ['the','boys','1080p'] to series ['the','boys'] -> no match -> ZERO results. Any quality/codec/season word in the query nuked all results.
- **Fix**: `matches_query` now strips release-metadata from the QUERY first (`_clean_query_tokens` + `_QUERY_META`): resolutions, codecs, sources, season/complete/pack, encoder tags, SxxExx, and years. Articles ("the") are kept. So "the boys 1080p", "the boys 1080p x265", "the boys s02" all reduce to ['the','boys'] and match "The.Boys". Spin-offs still dropped; all-noise query (e.g. just "1080p") disables filtering instead of returning nothing.
- **Files**: search_service.py (matches_query + _clean_query_tokens + _QUERY_META).
- **Verified**: unit (quality/codec/season noise in query still matches; spin-offs dropped; all-noise disables filter); route harness ("the boys 1080p" returns The Boys, not empty); py_compile; gunicorn boots; app.js untouched.


### 2026-05-31 — Relevance filter tightened to EXACT title match
- **Why**: the prior "all query tokens appear anywhere" rule let spin-offs / look-alikes through — searching "The Boys" returned "The Boys Presents Diabolical" and "My Life With the Walter Boys" (both contain the words "the"+"boys" scattered).
- **What**: `matches_query` now keeps a result only if its parsed series tokens EQUAL the query tokens (separator/case-insensitive). "The Boys" -> only "The Boys".
- **Trade-off (accepted by user)**: this also drops spin-offs like "Daredevil Born Again" / "Marvels Daredevil" for a plain "Daredevil" search. To find a spin-off, search its full title (IMDb autocomplete assists). Supersedes the earlier all-tokens choice.
- **Files**: search_service.py (matches_query only). Route wiring unchanged.
- **Verified**: unit (The Boys keeps only The Boys; Diabolical/Walter Boys dropped; spaced variant matches; Daredevil spin-offs dropped; empty query disables filter); route harness (series + normal return only exact match); py_compile; gunicorn boots; app.js untouched.


### 2026-05-31 — Search providers: FAILOVER instead of merge (single-source per search)
- **Why**: merging 3 sources multiplied duplicates (different infohashes / naming variants slip past dedup) and added unrelated junk. Fix = use ONE good source per search.
- **What**: `multi_search(q, prefer=None)` now tries providers in PRIORITY ORDER and returns the FIRST that yields results (failover), instead of querying all concurrently and merging. Returns `(rows, winning_provider)`. Same-source infohash dedup still applied.
- **Provider lock**: `routes.search.round_search` pins the winning provider for the whole request via `prefer=`, so multi-round Series searches stay on ONE source (consistent, no cross-source dups).
- **Priority order**: `apibay -> torrents-csv -> bitsearch` (apibay freshest/cleanest; bitsearch last, currently flaky). Override via `SEARCH_PROVIDERS` env (comma-separated, priority order). Set `SEARCH_PROVIDERS=apibay` for strict single-source.
- **Removed**: ThreadPoolExecutor merge path (no longer needed). Relevance filter (matches_query) + series-key episode dedup + pack dedup all retained.
- **Files**: search_service.py (multi_search failover + _run_provider; dropped concurrent import), config.py (priority-order default + comment), routes/search.py (provider lock in round_search).
- **Verified**: unit (first-non-empty wins, others not called; empty->fallback; all-empty->[]/None; prefer locks); route harness (series locked to apibay across rounds, junk filtered, Normal intact, quota guard 400); py_compile; gunicorn boots; app.js untouched & in sync.
- **WatchSoMuch note**: investigated — no public JSON API (probes: /api/torrents 404, search 302 to login), Cloudflare + shifting domains + VIP gating; not a viable programmatic backend (HTML scrape only). Documented, not integrated.


### 2026-05-31 — Series fixes: relevance filter + separator-insensitive dedup + pack dedup
- **#1 Duplicate episodes**: the episode dedup key now uses `series_key()` (token-normalized series, separators collapsed) so `Daredevil.Born.Again` and `Daredevil Born Again` count as the SAME series → the dup `S01E09 ELiTE 1080p` rows now collapse to the highest-seeded one. (Was: raw `series.lower()`, so dots vs spaces produced different keys.)
- **#2 / #3 Unrelated results**: added `matches_query(query, series)` — keep a result only if EVERY query word appears in the parsed series tokens. Applied centrally in `routes.search.round_search` (both Normal + Series). Drops provider junk like "Bones" / "The Red Green Show" when searching "Daredevil". Per user choice (all-tokens match): "Daredevil Born Again" and "Marvels Daredevil" are kept (they contain "daredevil").
- **#5 Season packs**: `build_packs` now dedups by (normalized series, season, quality bucket) keeping highest-seeded, and still shows the ORIGINAL torrent name (already removed pack_label previously). Fixes the duplicate "Marvels Daredevil · Season 2" entries.
- **#4 "Unknown" quality groups**: already fixed by the prior encoder→quality redesign (no uploader level). Was only visible because the running server had the OLD build deployed.
- **New helpers**: `_norm_tokens`, `series_key`, `matches_query` in search_service.py.
- **Files**: search_service.py (helpers, group_series_results dedup key, build_packs dedup), routes/search.py (relevance filter in round_search).
- **Verified**: unit (dotted/spaced dup collapse keep-47-drop-0; Bones/RedGreen dropped, Daredevil/BornAgain/Marvels kept; pack dedup + original name); route harness (series junk dropped + dup collapsed, normal junk dropped); py_compile; gunicorn boots; app.js unchanged & in sync.
- **NOTE TO USER**: the screenshots showed the OLD deployed build (uploader sub-groups, "Unknown · 1080p x265", synthesized pack labels). Deploy the rebuilt project.zip to get #4/#5 + these fixes live.


### 2026-05-31 — Series Mode redesign: encoder→quality→season→episode, per-encoder dedup, original pack names
- **Structure**: removed the **uploader** level. Series is now **encoder → quality → season → episode**. (`group_series_results` returns `encoders[].qualities[]` instead of `encoders[].uploaders[]`.)
- **Encoder merge**: case variants collapse via `encoder_norm` (ELiTE/elite/ELITE → one encoder); display uses the first nicely-cased original name.
- **Quality**: coarse buckets only — **4K / 1080p / 720p / Other** (via `_quality_bucket`), not fine "1080p x265" labels. Ordered 4K→1080p→720p→Other.
- **Dedup**: within each (encoder, quality) the same `<series>+SxxExx` collapses to the **highest-seeded** copy (one row per episode per encoder).
- **Episode order**: season ascending, then episode ascending (true sequence).
- **Season packs**: now display the **original torrent name** (`row.name`); dropped the synthesized `pack_label` and pack uploader tag.
- **Dead code removed**: `_extract_uploader`, `_pack_label`, `_quality_sort_key` (no remaining callers).
- **Frontend**: `renderSeriesGrouped` rewritten — packs show original name; encoder body iterates quality groups (reusing the existing collapsible group CSS) → season label → episodes; "Add all" at encoder and quality-group level. app.js rebuilt.
- **Files**: search_service.py (group_series_results rewrite, build_packs, dead-code removal), static/js/src/3b-series.js, app.js. Route/template/CSS unchanged. Normal mode unaffected.
- **Verified**: unit (encoder case-merge, per-encoder highest-seed dedup, 4K/1080p/720p buckets, episode sequence, packs original name, PSA stays separate, no `uploaders` key); route harness (series new shape, Normal intact, quota guard 400); py_compile + node --check; gunicorn boots; app.js in sync.


### 2026-05-31 — Normal mode: top-30 by seeders per quality, shown size-ascending
- **What**: `group_by_quality` now keeps only the **30 most-seeded** releases **per quality section** (4K/1080p/720p/Other), then displays each section **size-ascending**. Quality sections retained; per-quality cap (selecting multiple qualities yields up to 30 in EACH section). Cap = `NORMAL_TOP_PER_QUALITY = 30` (module constant). Applied after cross-source dedup.
- **Scope**: single function in search_service.py; response shape (`normal_grouped`) and all frontend unchanged. Series mode unaffected (does not use group_by_quality).
- **Files**: search_service.py.
- **Verified**: unit (50→30 kept = highest seeds, size-asc); route harness (1080p capped 30, size-asc; series intact); py_compile; gunicorn boots; app.js untouched & in sync.


### 2026-05-31 — Multi-source search (concurrent merge + dedup) + category removed
- **Why**: bitsearch.eu's /api/v1/search was returning 500/502 (recurring outages), taking down all search. Single-source = single point of failure.
- **What**: Added `SearchService.multi_search(q)` — queries **bitsearch + apibay (The Pirate Bay JSON API) + torrents-csv CONCURRENTLY** (ThreadPoolExecutor), merges results, dedups by infohash (highest-seed kept). **Fault-tolerant**: a provider that 500s/times-out/throws contributes 0 rows and is logged; results still return from working providers. Latency ≈ slowest single provider, not the sum (proven: full merged search 1.12s while bitsearch alone hangs 6s to timeout).
- **Providers**: each returns the canonical UI row shape via `_make_row` (name, infohash, seeds, leeches, size/size_bytes, date, magnet, source). New helpers `_fetch_apibay`, `_fetch_torrents_csv`, `_bitsearch_rows`, `_to_int`, `_unix_to_date`, `_format_bytes`. No new pip deps (stdlib concurrent.futures + existing requests).
- **Category REMOVED**: providers use different/poor category schemes, so the category `<select>`, its param, validation, and `_CATEGORY_LABELS` mapping were dropped. Rows carry `category:"Other"` for UI compatibility. `routes/search.py` no longer imports/validates category/sort/order/page (page was already dead); fetches go through `multi_search`.
- **Config**: `search_providers` tuple (env `SEARCH_PROVIDERS`, comma-separated; default all three) to enable/disable any source without code.
- **Files**: search_service.py (providers + multi_search), routes/search.py (rewrite: multi_search, no category), config.py (search_providers), templates/index.html (category select removed), static/js/src/5-search.js + 6-main.js (category send/listener removed), app.js (rebuilt).
- **Verified LIVE**: with bitsearch DOWN (500/timeout), multi_search returned 115 merged rows from apibay(94)+torrents-csv(21) in 1.12s; 0 cross-source duplicate infohashes; concurrency confirmed; route harness (normal grouped, multi-quality 3 rounds, series ELiTE, quota guard 400/0-calls, empty→400, stray category ignored, suggest); py_compile + node --check all pass; gunicorn boots; app.js in sync; SEARCH_PROVIDERS override works.


### 2026-05-31 — Accordion sections (collapsed default, one-open) both modes
- **Why**: 50+ results all expanded = overwhelming scroll on mobile.
- **What**: All sections start **collapsed** on every search. **One section open at a time** (opening one closes its siblings) via a shared `makeAccordion(section, header, container, groupSel)` helper. Applies to Normal quality sections, Series Season Packs + encoder sections, AND uploader sub-groups within an open encoder (uploader is now its own collapsible accordion — one uploader open at a time). Desktop + mobile.
- **Files**: static/js/src/3b-series.js (makeAccordion; default-collapsed; uploader-group/uploader-body wrappers), static/css/base.css (.uploader-group/.uploader-body collapse + chevron), app.js.
- **Verified**: no stale toggle handlers; node --check; CSS balanced; gunicorn boots. Add-all buttons still work (accordion ignores button clicks).

### 2026-05-31 — Normal: fetch by seeders, display size-ASCENDING + clickable header
- **Fetch vs display split**: Normal mode now fetches bitsearch by **seeders** (most-seeded/relevant 50 per quality) but **displays** each quality section **size-ascending** (low→high) by default.
- **Header row**: added one clickable header (Name | SE | Time | Size | Add) at the top of the sectioned views (Normal + Series), wired to `cycleSort` (client-side re-sort).
- **Series**: structure unchanged; gains the header row. Episodes stay in native S/E order until the user clicks a header (`userSorted` flag), then re-sort within each uploader/season group.
- **Files**: routes/search.py (run_normal fetch=seeders), search_service.py (group_by_quality size-asc), 1-core.js (default size/asc), 3b-series.js (seriesHeaderRow, userSorted, series re-sort), 3-search-sort.js (cycleSort re-renders active view), base.css + responsive.css (.sec-head), app.js.
- **Verified**: fetch sends sort=seeders; sections display 476MB→1.86GB→8.38GB (size-asc); header clicks re-sort; gunicorn boots; node --check + py_compile pass.

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
[2026-06-01] High-confidence dead-weight cleanup — removed old flat search/pagination/Add-all/config leftovers
[2026-06-01] Clipboard magnet re-check on focus/visibility/Search tab — 1-core.js, 5-search.js, 6-main.js, app.js
[2026-06-01] Persistent duplicate guard for magnet auto-add — 1-core.js, 5-search.js, app.js
[2026-06-01] Frontend magnet auto-ingest from clipboard/paste/URL — 1-core.js, 5-search.js, 6-main.js, app.js
[2026-06-01] Mobile Search V2 polish: unclip suggestions, preserve open sections, compact rows — 3b-series.js, responsive.css, app.js
[2026-06-01] Cancel active Seedr transfers — cloud_service.py, routes/cloud.py, 2-cloud.js, base.css, responsive.css, app.js
[2026-06-01] Active Seedr transfer progress in Cloud Drive — cloud_service.py, routes/cloud.py, 1-core.js, 2-cloud.js, 5-search.js, 6-main.js, base.css, responsive.css, app.js
[2026-06-01] Mobile Search V2 implementation — index.html, 3b-series.js, 5-search.js, 6-main.js, responsive.css, app.js
[2026-06-01] Compact desktop UI + eager storage snapshot — base.css, 2-cloud.js, 6-main.js, app.js
[2026-06-01] Desktop cloud UI: storage moved to topbar; desktop Copy Link action added — templates/index.html, base.css, 1-core.js, 2-cloud.js, 6-main.js, app.js
[2026-05-31] Protocol Adoption & Workspace Cleanup — ai/QUICK.md
[2026-05-31] Code quality: magic numbers extracted, route docstrings added, no behavior change
[2026-05-31] Security and reliability fixes (initial batch)
[2026-05-31] Security and reliability fixes (initial batch)
[2026-05-31] Initial fix batch: storage check, empty token guard, Redis health check, specific exception handlers, safe error messages.

[2026-05-31] 2026-05-31 — Secure Logging System Implementation — streamly_hardened/app.py, ai/deploy/check.py


## 🔄 Recent Changes
- **2026-06-01** — Renamed visible app branding/docs to **CloudFlow** while preserving internal `streamly_hardened` package names. Changed: index.html, app.py, DEPLOY.md, render.yaml, STATE.md.
- **2026-06-01** — Removed high-confidence dead weight: old flat search table/pagination renderer and DOM/CSS, stale Add-all code/styles, obsolete category/sort/order validators/config, unused config fields, and unused imports. Changed: index.html, 3b-series.js, 5-search.js, app.js, base.css, responsive.css, app.py, config.py, security.py, cloud_service.py, routes/cloud.py.
- **2026-06-01** — Clipboard magnet detection now re-checks on Search tab click, focus, visibility restore, and pointer interaction so newly copied magnets are detected. Changed: 1-core.js, 5-search.js, 6-main.js, app.js.
- **2026-06-01** — Added 24h localStorage duplicate guard for magnet auto-add keyed by BTIH infohash; duplicate magnets fill the search box but do not auto-add unless user taps Add. Changed: 1-core.js, 5-search.js, app.js.
- **2026-06-01** — Added frontend magnet auto-ingest: clipboard auto-detect/autopaste/auto-add, paste auto-add, and URL/hash `magnet` ingest with URL cleanup. Changed: 1-core.js, 5-search.js, 6-main.js, app.js.
- **2026-06-01** — Polished Mobile Search V2: suggestions no longer clipped, sort/quality/season clicks preserve open sections, quality nav now looks like season chips, and result rows are denser. Changed: 3b-series.js, responsive.css, app.js.
- **2026-06-01** — Added Cancel button for active Seedr transfers in Cloud Drive; endpoint `/api/transfer/cancel` calls Seedr `delete_torrent`. Changed: cloud_service.py, routes/cloud.py, 2-cloud.js, base.css, responsive.css, app.js.
- **2026-06-01** — Cloud Drive now shows active Seedr transfers before completion, with desktop/mobile progress bars and auto-refresh while transfers are loading. Changed: cloud_service.py, routes/cloud.py, 1-core.js, 2-cloud.js, 5-search.js, 6-main.js, base.css, responsive.css, app.js.
- **2026-06-01** — Implemented Mobile Search V2: no mobile Torrent Search header, compact visible search bar, Filters bottom sheet, Normal|Series segmented control, mobile global quality navigation, season chips per encoder, and removed Add-all buttons. Changed: index.html, 3b-series.js, 5-search.js, 6-main.js, responsive.css, app.js.
- **2026-06-01** — Made desktop storage/topbar and selected-item side panel compact; added eager authenticated storage snapshot so storage usage loads on Search without clicking Cloud Drive. Changed: base.css, 2-cloud.js, 6-main.js, app.js.
- **2026-06-01** — Desktop cloud UI updated: left topbar now shows Storage + compact meter + `Connected to <email>`, right email pill hidden, side-card Storage block removed, and desktop Copy Link action added for files/folders/multi-select zip links. Changed: index.html, base.css, 1-core.js, 2-cloud.js, 6-main.js, app.js.
- **2026-05-31** — Series mode now also runs a broad <title> query first and merges it into packs + episodes (catches releases the narrow queries miss); broad query counts toward the 12-query quota; episodes filtered to ticked encoders. Changed: routes/search.py.
- **2026-05-31** — Normal mode simplified: one broad query -> filter by quality (sections) + encoder -> size-asc quality sections (no cap). Also fixed relevance filter silently dropping ALL movies (episodes=exact match, movies=prefix match). Changed: search_service.py, routes/search.py, 5-search.js, app.js.
- **2026-05-31** — Fixed exact-match filter eating all results when the query included quality/codec/season words (e.g. "the boys 1080p" returned nothing). Query is now stripped of release-metadata before matching. Changed: search_service.py.
- **2026-05-31** — Relevance filter tightened to EXACT title match (was: all-tokens-present). Fixes spin-offs leaking in (e.g. "The Boys" no longer returns "The Boys Presents Diabolical" / "My Life With the Walter Boys"). Trade-off: spin-offs need their full title to appear. Changed: search_service.py.
- **2026-05-31** — Search switched from merge-all to FAILOVER: first provider (priority apibay->torrents-csv->bitsearch) that returns results wins; provider locked per request so each search is single-source (kills cross-source duplicates). Set SEARCH_PROVIDERS=apibay for strict single-source. Changed: search_service.py, config.py, routes/search.py.
- **2026-05-31** — Series fixes: separator-insensitive episode dedup (Daredevil.Born.Again == Daredevil Born Again), query-relevance filter dropping unrelated results (Bones/Red Green Show), pack dedup by (series,season,quality) keeping highest-seed. Changed: search_service.py, routes/search.py.
- **2026-05-31** — Series Mode redesign: encoder→quality(4K/1080p/720p)→season→episode (uploader level removed); encoders merged case-insensitively; per-encoder dedup of <series>+SxxExx keeping highest seeder; episodes in sequence; season packs show original torrent name. Changed: search_service.py, 3b-series.js, app.js.
- **2026-05-31** — Normal mode now keeps the 30 most-seeded per quality section and displays them size-ascending (quality sections retained, per-quality cap). Changed: search_service.py (group_by_quality + NORMAL_TOP_PER_QUALITY).
- **2026-05-31** — Multi-source search: bitsearch + apibay + torrents-csv queried concurrently, merged & deduped; survives any provider outage (verified live with bitsearch down → 115 results in 1.12s). Category filter removed entirely. Changed: search_service.py, routes/search.py, config.py, templates/index.html, 5-search.js, 6-main.js, app.js.
- **2026-05-31** — Accordion: all sections collapsed by default, one open at a time (both modes, both desktop+mobile); uploader sub-groups also collapsible. Changed: static/js/src/3b-series.js, static/css/base.css, app.js.
- **2026-05-31** — Normal: fetch by seeders, display size-ascending; added clickable Name/SE/Time/Size/Add header row to sectioned views (both modes); Series episodes re-sort within groups on header click. Changed: routes/search.py, search_service.py, 1-core.js, 3b-series.js, 3-search-sort.js, base.css, responsive.css, app.js.
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
