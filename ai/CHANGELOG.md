# Streamly — Changelog & Handoff Note

> Read this first when resuming in a new chat. It captures current state + key decisions
> so you don't have to re-read every file.

## Current state (as of this entry)
- **Desktop UI:** unchanged classic table layout (works well — don't touch).
- **Mobile UI (≤700px):** fully reworked, this was the bulk of recent work.
- **Assets are auto-cache-busted** now — no manual `?v=` bumping. `app.py` computes
  `asset_ver` from the newest static-file mtime and the template uses `?v={{ asset_ver }}`.

## File layout that matters
```
streamly_hardened/
├── app.py                      # Flask factory create_app(); index() injects asset_ver
├── templates/index.html        # loads css/base.css + css/responsive.css + js/app.js
└── static/
    ├── css/base.css            # core/desktop styles (load FIRST)
    ├── css/responsive.css      # media queries + mobile cloud/search/history (load SECOND)
    └── js/app.js               # single IIFE (see "Known tech debt")
check.py                        # run before committing: JS syntax + CSS braces + Flask 200
build_zip.sh                    # rebuilds /home/user/project.zip
```
⚠️ **CSS load order is load-bearing** — `base.css` then `responsive.css`. They were a single
`style.css` split byte-identically; keep that order or the cascade changes.

## Mobile UI decisions (don't re-litigate)
- **Search:** real desktop table scaled to fit width (no card view). Columns:
  NAME · SE · TIME · SIZE · ADD. Category column **removed** (dropdown filter kept).
  Leecher column **removed**. Non-name columns centered; name left.
- **Add button (search):** icon-only `+` on mobile (font-size:0 + `::before` glyph by
  `data-state` idle/adding/done). Full "Add" text on desktop. This was the only way to
  shrink the column past the word "Add"'s min-width.
- **Cloud Drive (mobile):** Seedr-style list (not the desktop table/side-panel).
  Tap = select, double-tap = open, ⋮ kebab = Download/Copy Link/Delete. Multi-select kept
  (bulk bar). Top toolbar: select-all + Up (left), Refresh (right). Bottom bar: account
  email + storage meter. Brand logo/"connected" hidden on mobile.
- **History:** magnet text + Time column removed. Actions are icons: 📋 Copy, ＋ Add, ✕ Delete.
- **Search box:** Paste (📋) and Clear (✕) are in-field icons. Clear = text-only
  (clears input + hides suggestions; keeps results).

## Bugs fixed (so they don't regress)
- **Mobile blank/hang:** caused by `background-attachment: fixed` + `backdrop-filter`.
  Removed `background-attachment: fixed`. Keep it gone.
- **Lingering suggestions:** `search()` now closes + cancels the suggestion dropdown/timer.
- **Horizontal overflow/crop:** `html/body { max-width:100%; overflow-x:hidden }` + brand
  allowed to shrink/hidden on mobile.

## JS is now split into editable fragments (edit these, not app.js directly)
`static/js/app.js` is a **generated bundle** — do not hand-edit it. Edit the fragments in
`static/js/src/` then rebuild:
```
cd streamly_hardened/static/js
python build_js.py        # concatenates src/*.js -> app.js (byte-identical bundling)
```
Fragments (numeric load order, all share one IIFE closure so state is global to them):
- `1-core.js`        state vars, $, status/toast, auth (silent relogin), postJson, showApp/showLogin, fmtDate
- `2-cloud.js`       selection, renderCloud + renderCloudMobile + context menu, storage, loadFolder, open/download/zip/delete
- `3-search-sort.js` syncSortControls, getSuggestions, cycleSort
- `4-history.js`     saveToHistory, renderHistory (+ its history button listeners)
- `5-search.js`      search(), renderPagination, renderSearchTable, makeAddButton
- `6-main.js`        setTab, all event wiring, init()
- `_wrap_open.txt` / `_wrap_close.txt` = the `(() => { ... })();` wrapper (don't edit)

Because they share one closure, a function in `5-search.js` can call one in `1-core.js`
directly — no imports needed. `check.py` validates the bundled app.js after rebuild.

## Known tech debt / next improvements
- Abandoned features already removed: Bridge badges CSS, magnet-paste CSS, Webtor branch,
  unused `updateSelected()`.

## Workflow — all dev tooling lives in `deploy/`
Windows one-click (portable WinPython 3.12.4 — path hardcoded in the deploy/*.bat files):
- **deploy/deploy.bat** → rebuild app.js + verify + git commit + push.  ← THE main one
- **deploy/check.bat**  → rebuild + verify only (no push).
- **deploy/build.bat**  → just rebuild app.js from src/ fragments.
- **deploy/check.py**   → the verifier (resolves repo root as its own parent dir).
- Move Python? edit the `PYEXE=` line atop the deploy/*.bat files.

Loop: I edit src/ fragments → you double-click **deploy/deploy.bat** → done.
App code + render.yaml + Dockerfile stay at repo root (that's what Render deploys).


## 2026-05-31 — Architecture Refactor

### Backend
- **Split `app.py` routes into Blueprints**: Moved route logic to `routes/` folder (auth, cloud, search, history). `app.py` is now a clean factory.
- **Split `services.py`**: Separated `CloudService` $\rightarrow$ `cloud_service.py` and `SearchService` $\rightarrow$ `search_service.py`.
- **Decoupled Rate Limiting**: Moved `TokenBucketRateLimiter` to `extensions.py`. The `@rate_limited` decorator now resolves the limiter from the global extension instead of requiring a local variable, allowing it to work in Blueprints.
- **Session/Client Helpers**: Extracted `current_client()` and `_try_restore_from_refresh()` to `auth_utils.py` to avoid circular imports between routes and app.

### Dev tooling
- **`check.py` update**: updated pre-flight checks to scan the new `routes/` directory and the split service files.

### Breaking changes
- None. API and behavior are identical.

---

## 2026-05-31 — Security and reliability fixes

### Backend
- **Storage check on `/api/add`**: before adding a magnet, checks if torrent size > available storage. Blocks with user-friendly "Not enough space" message. Falls back gracefully if storage check fails (network error) — proceeds with add rather than blocking.
- **Empty token guard**: `CloudService.serialize_token()` now returns `None` instead of `""` on failure. `RedisStore.set_refresh_token()` rejects `None`/empty values. Redis never gets corrupted with empty token strings.
- **Redis health check on startup**: after RedisStore init, tests connectivity. If Upstash unreachable, logs a clear warning. Previously silent failure — now you see it in Render logs immediately.
- **Specific exception handlers**: replaced 9 broad `except Exception as e` with `except (ConnectionError, TimeoutError)` in all provider-facing routes (add, delete, zip, bulk ops, list, history, get_url). Real bugs are no longer silently swallowed.
- **Safe error messages**: all `json_error` responses use generic messages. No `str(exc)` leaking file paths or internal variable names.

### Frontend
- **`/api/add` now sends `size_bytes`**: `5-search.js` sends `result.size_bytes` with the add request so the server can do the storage check. Backend exposes `size_bytes` in search response.

### Dev tooling
- **`deploy/deploy_all.py`**: single-command deployment script. Writes all files, rebuilds app.js, runs check.py (JS + CSS + Flask + pre-flight summary), creates project.zip. Replaces per-file tool calls with one Python script.
- **Extended `deploy/check.py`**: pre-flight summary now shows all fixes (done/pending/skipped) before commit.

### Breaking changes
- None. All changes are additive or defensive. No API contract changes.
