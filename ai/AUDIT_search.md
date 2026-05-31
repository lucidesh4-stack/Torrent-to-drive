# 🔬 Search Page — Complete Audit & Workflow Map

> **STATUS (2026-05-31): ALL findings addressed.** F1–F4, F6 fixed (batch 1). F5, F7, F8, F9, F10, P3 fixed (batch 2). Remaining P3 items (in-process rate limiter across workers, global getaddrinfo serialization) are documented infra trade-offs in STATE.md Known Tech Debt. See "Decision Ledger" in STATE.md.

> Adversarial audit performed in Ultimate Engineer Mode under the Zero-Regression Protocol.
> Every claim below was **executed**, not assumed. Test harnesses: `/tmp/harness.py`,
> `/tmp/parse_test.py`, `/tmp/pack_test.py`, `/tmp/dedup_test.py` (live Flask test client +
> direct service calls). No source files were modified by this audit (app.js was rebuilt
> only to verify build-sync, then restored byte-for-byte).

---

## 1. Complete Search Workflow (end-to-end trace)

### 1.1 Files in the search data path
| Layer | File | Role |
|---|---|---|
| Template | `templates/index.html` | Search box, category `<select>`, Quality/Encoder multi-selects, `[Normal][Series]` toggle, `#results` (legacy table, hidden) + `#seriesResults` (active container), `#pagination` |
| JS core | `src/1-core.js` | Globals: `currentSort="size"`, `currentOrder="asc"`, `$()`, `parseResponse`, `postJson` |
| JS search | `src/5-search.js` | `search()` orchestration, `renderPagination`, `renderSearchTable` (legacy), `makeAddButton` |
| JS sort/suggest | `src/3-search-sort.js` | `getSuggestions` (IMDb autocomplete), `cycleSort` (client-side re-sort), `syncSortControls` |
| JS render | `src/3b-series.js` | `renderNormalGrouped`, `renderSeriesGrouped`, `sortRows`, `makeAccordion`, dropdown helpers |
| JS wiring | `src/6-main.js` | Event listeners, dropdown toggles, mode buttons, `init()` |
| Route | `routes/search.py` | `/api/search`, `/api/suggest` |
| Service | `search_service.py` | `SearchService.bitsearch/imdb_suggestions`, parsing, grouping, dedup, DoH DNS fallback |
| Security | `security.py` | `rate_limited`, `validate_*`, `json_error` |
| Config | `config.py` | allowed sorts/orders/categories, bitsearch URL, timeouts |

### 1.2 Runtime flow
1. **User types** → `searchQuery#input` → `getSuggestions()` (350 ms debounce, ≥3 chars) → `GET /api/suggest?q=` → `SearchService.imdb_suggestions` → IMDb suggestion JSON → poster dropdown. Clicking a suggestion only fills the box (no auto-search, to save quota).
2. **User clicks Search / Enter** → `search(false, 1)` in `5-search.js`.
   - If input matches `magnet:?xt=urn:btih:` → saves to history + `POST /api/add` (bypasses search entirely).
   - Else builds query params: `q, category, sort=currentSort, order=currentOrder, page=currentPage, dedup=1, quality=<checked>`; if `seriesMode` also `mode=series, encoders=<checked>`.
3. **Backend `/api/search`** (`@rate_limited(cost=1.0)`):
   - Validates `q, category, sort, order, page` (rejects on bad input → 400).
   - **Series branch** (`mode=series`): sanitizes qualities/encoders → computes `planned = 2*Q + N*Q` → **quota guard 400** if `> 12` → runs packs queries (`<q> x265` + `<q> hevc`, size-desc) + encoder queries (`<q> <ENC>`, seeders-desc) → dedup each set → `build_packs` (packs only, smallest-first, top-20) → `group_series_results` (encoder→uploader→quality→season→episode) → JSON `{mode:"series", packs, encoders, stats, requests_used, ...}`.
   - **Normal branch** (default): one `bitsearch` **per quality** (`<title> <q>`, **seeders-desc**), combine → dedup → `group_by_quality` (2160p→1080p→720p→Other, each **size-asc**) → JSON `{mode:"normal_grouped", quality_groups}`.
4. **`SearchService.bitsearch`**: GET `bitsearch.eu/api/v1/search` (limit 50). On DNS failure → Cloudflare DoH (`1.1.1.1`) resolve + scoped `socket.getaddrinfo` monkeypatch retry (thread-locked, 5-min IP cache). On any failure → empty results (never raises). Optional per-call dedup.
5. **Frontend render**: series → `renderSeriesGrouped`; normal → `renderNormalGrouped`. Both prepend a clickable `seriesHeaderRow` (Name|SE|Time|Size|Add), render **accordion** sections (all collapsed by default, one-open-at-a-time). `cycleSort` re-orders loaded rows client-side (no re-fetch).
6. **Add**: per-row Add = `POST /api/add {magnet,size}`. Series "Add all N" = 1 Seedr add (episode 1) + all N saved to History.

### 1.3 Verified-WORKING behaviors (executed)
- ✅ Normal single + multi-quality (`Show 2160p`/`1080p`/`720p` → 3 calls, grouped).
- ✅ Series 1080p×ELiTE → 3 calls (2 pack + 1 encoder), `requests_used=3`, grouped output.
- ✅ Quota guard: 3q×3enc = 15 > 12 → **HTTP 400 with ZERO upstream calls**. Correct.
- ✅ Dedup: same infohash (case-insensitive) keeps highest-seed; blank-hash rows pass through; order preserved.
- ✅ Validation: empty `q`→400, bad category→400, bad sort→400, page `-1/99999/abc`→400.
- ✅ IMDb suggest returns normalized list.
- ✅ `app.js` is **byte-identical** to a fresh `build_js.py` rebuild from `src/` (no build drift).
- ✅ App boots (`create_app()`), all accordion CSS classes exist, all JS-referenced element IDs exist in the template.

---

## 2. FINDINGS (prioritized)

### 🔴 P0 — Correctness bugs that silently lose results

> ✅ **F1 FIXED** — `_PACK_RE` now matches `Sxx COMPLETE` (space/underscore/dash); new `_SEASON_TOKEN_RE` detects bare `Sxx`/`Season N` packs. Verified 7/7 packs, 0 episode misclassifications.
> ✅ **F2 FIXED** — dead `page` param removed; bad values no longer 400.

**F1. Season-pack parser misses the two most common naming patterns → packs dropped.**
`_PACK_RE` requires `S05.COMPLETE` (dot/no space) or `SEASON 5`/`COMPLETE SEASON`. It does **NOT** match:
- `Breaking Bad S05 COMPLETE ...` (space before COMPLETE) → `is_pack=False` → **discarded**.
- `Loki S02 1080p x265-ELiTE` (bare `S02`, no "COMPLETE"/"SEASON" word) → `is_pack=False` → **discarded**.
Evidence (`/tmp/pack_test.py`): of 7 valid pack names, only 4 survived; bare-`Sxx` packs (the dominant real-world form) were 0% detected. Since Series Mode's pack queries are literally `<title> <q> x265|hevc`, most returned packs are named `Show.S02.1080p...x265-GROUP` with no "COMPLETE" word → **the Season Packs section is frequently empty even when packs exist.** This is the headline Series-Mode feature failing.

**F2. `page` parameter is validated but never used in either active mode.**
`routes/search.py` parses & range-checks `page` (rejects bad values with 400) then **never passes it to `bitsearch`** — both `run_normal()` and series `run()` hardcode page=1. So:
- Pagination is dead for the only two modes the UI uses (always 50 results max per quality).
- `renderPagination` in `5-search.js` is dead code for grouped modes (`quality_groups`/`series` responses carry no `pagination` object; the function is never called from the grouped paths).
- Net: users see at most 50 results/quality with no way to page deeper, and a validated-but-ignored param is a latent trap.

### 🟠 P1 — Latent crashes / fragility

> ✅ **F3 FIXED** — `validate_positive_int_local` deleted (was the only `Any` reference).
> ✅ **F4 FIXED** — `/api/search` + `/api/suggest` return 503 `search_unavailable` when `search` is None.
> ✅ **F6 FIXED** — template comment corrected (dropdowns visible in both modes).

**F3. `validate_positive_int_local(value: Any, ...)` references undefined `Any`.**
`routes/search.py` never imports `Any`. It does **not** crash today **only** because `from __future__ import annotations` (PEP 563) turns the annotation into a string that's never evaluated. This is a live landmine: any future tooling that calls `typing.get_type_hints()` on this module, or removal of the `__future__` import, → `NameError` at import/eval. Also, a duplicate of `security.validate_positive_int` already exists — this local copy is redundant.

**F4. No `None` guard on `current_app.search` → 500 on misconfiguration.**
Both `/api/search` and `/api/suggest` do `search = getattr(current_app, "search", None)` then immediately call `search.bitsearch(...)` / `search.imdb_suggestions(...)`. If `app.search` is ever unset, both endpoints raise `AttributeError: 'NoneType'` → generic 500 (verified). Defensive 503 ("search unavailable") would be correct.

### 🟡 P2 — Dead code / spec drift / minor UX

> ✅ **F5 FIXED** — validation kept (hygiene) + documented as intentionally not forwarded.
> ✅ **F7 FIXED** — empty quality shows "1080p (default)".
> ✅ **F8 FIXED** — `renderPagination`/`renderSearchTable` + `#results`/`#pagination` DOM deleted (app.js 1465→1352).
> ✅ **F9 FIXED** — dead `_CATEGORY_LABELS["1"]` removed.
> ✅ **F10 FIXED** — resolution/codec tokens rejected as encoders.
> ✅ **P3 (timeout) FIXED** — 45s series budget → partial results, no worker kill.

**F5. `sort`/`order` are validated then thrown away in grouped modes.** Normal hardcodes `seeders`, packs hardcode `size`. The client's `currentSort/currentOrder` only affect client-side re-sort. Validation of these params is pointless work and misleads future maintainers into thinking server sort is honored.

**F6. Template comment contradicts implemented behavior.** `index.html` labels the Quality/Encoder dropdowns *"Series-only multi-select dropdowns (hidden in Normal mode)"*, but STATE.md + `setSeriesMode` deliberately keep them visible in **both** modes. Stale comment → maintainer confusion.

**F7. Un-checking all qualities → "Quality: none" label but search still runs as 1080p.** Backend silently defaults `[]→["1080p"]`; the UI shows "none", so the user can't tell what was searched. Confusing but not a crash.

**F8. Legacy flat-table path is fully dead.** `renderSearchTable`, `#results`/`#torrentBody`/`#mobileResults`, and `renderPagination` are never reached (both modes route to `#seriesResults`). ~90 lines of unreachable JS + hidden DOM kept "just in case" — dead weight that node-check passes but humans must still reason about.

**F9. `_CATEGORY_LABELS["1"]` ("Other") is unreachable.** `allowed_categories` excludes `"1"` and the `<select>` has no `value="1"`, so that mapping can never fire. Harmless, but dead.

**F10. Anime/non-standard titles produce garbage encoder tokens before being discarded.** `Some Anime - 01 [1080p][HEVC][SubGroup]` parsed `encoder="1080p"` (bracket fallback grabbed the resolution). It's saved by `parsed=False` (discarded into Other), so no user-visible bug today — but the extractor is fragile and would surface garbage the moment `parsed` logic loosens.

### 🟢 P3 — Observations (not bugs)
- Rate limiter is **in-process** (per `security.py` TokenBucketRateLimiter) → on Render multi-worker it under-limits (each worker has its own bucket). Matches known tech-debt #3.
- `request_timeout_seconds=6` × up to 12 sequential series calls = up to ~72 s worst case, all **synchronous** in one request — risks Render/gunicorn worker timeout (default 30 s) under slow upstream. No per-request total budget.
- DoH fallback monkeypatches the **global** `socket.getaddrinfo` under a lock — correct but globally serializes all bitsearch DNS during the fallback window.

---

## 3. Recommended fix order (no code written yet — awaiting approval per RULES)
1. **F1** (pack regex) — highest user impact, isolated to `search_service._PACK_RE` + a bare-`Sxx`-pack heuristic. Needs careful ZRP (must not reclassify real episodes as packs).
2. **F2** (page wiring) — decide: either implement real pagination for grouped modes, or stop accepting/validating `page` and remove dead `renderPagination`.
3. **F3 + F4** (import `Any` / collapse into `validate_positive_int`; add `None`→503 guard) — trivial, high safety.
4. **F5/F6/F8/F9** cleanup — dead-code & comment removal (zero behavior change).
5. **F7** UX label, **F10** extractor hardening — optional polish.

> Each fix will get a full ZRP Impact Analysis (path trace, type check, dependency audit,
> side-effect map) + before/after before any code is written.
