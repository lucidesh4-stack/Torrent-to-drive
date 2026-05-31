# Streamly — Context for Chat Continuations

> **Read this first in every new chat.** Your job is to pick up exactly where the last chat left off —
> as if you're the same AI with full project memory. Read everything. Then answer or ask.

---

## 🔄 HOW TO USE THIS FILE

1. Read the whole thing before responding
2. Check **"Active work"** — what was in progress when this chat ended
3. Check **"Open questions"** — things not yet decided
4. Check **"User preferences"** — how this user works, what they reject
5. If the active work has a plan in **"Pending plans"**, follow it
6. If an open question is blocking you, ask the user before doing anything
7. If context is insufficient to answer, say "I need more info on X before proceeding"

---

## WHAT THIS PROJECT IS

**Streamly Hardened** = Flask web app acting as a local Seedr client with integrated torrent search.

**User journey:**
1. Login with Seedr account (or close dialog → Guest mode for search-only)
2. **Search tab**: type a title → bitsearch.eu results → click **Add** → torrent sent to Seedr cloud
3. **Cloud Drive tab**: browse Seedr files, stream video, download/zip, delete
4. **History modal**: view/copy/re-add/delete magnet links (global, device-wide, stored in Upstash Redis)

**Deployment:** Docker on Render (free tier) + UptimeRobot every 5 min to prevent sleep.
**Persistence:** Upstash Redis (HTTP REST, no TCP) stores: SECRET_KEY, refresh tokens, magnet history.
**Sessions:** In-process TTLStore (not Redis) — Seedr client objects per session ID.

---

## FULL FILE LAYOUT

```
workspace/
├── ai/                       ← all AI-maintained files
│   ├── QUICK.md              ← 1-page reference (read first, every chat)
│   ├── CHANGES.md            ← template + examples for changes.json
│   ├── CONTEXT.md            ← full project state (read on resume)
│   ├── RULES.md              ← user's working rules
│   ├── CHANGELOG.md          ← decision history
│   ├── ACTIVITY_LOG.md       ← deployment log
│   ├── changes.json          ← current session changes (edit this)
│   └── deploy/               ← deployment tooling
│       ├── deploy_all.py     ← single-command: write + verify + docs + zip
│       ├── check.py          ← JS + CSS + Flask smoke test
│       ├── deploy.bat        ← git commit + push (Render auto-deploys)
│       ├── check.bat
│       └── build.bat
├── DEPLOY.md                 ← Render + Upstash setup guide (root level)
├── render.yaml               ← Render deployment config (root level)
├── project.zip               ← auto-generated zip
└── streamly_hardened/        ← source code (Seedr client)
    ├── __init__.py
    ├── app.py
    ├── config.py
    ├── security.py
    ├── cloud_service.py      ← (split from services.py)
    ├── search_service.py     ← (split from services.py)
    ├── auth_utils.py         ← (session/client helpers)
    ├── store.py
    ├── redis_store.py
    ├── requirements.txt
    ├── Dockerfile
    ├── .dockerignore
    ├── routes/               ← route blueprints
    │   ├── __init__.py
    │   ├── auth.py
    │   ├── cloud.py
    │   ├── search.py
    │   └── history.py
    ├── static/
    │   ├── css/base.css         ← MUST LOAD FIRST
    │   ├── css/responsive.css   ← MUST LOAD SECOND
    │   └── js/
    │       ├── app.js           ← GENERATED (don't touch)
    │       └── src/             ← edit here
    │           1-core.js
    │           2-cloud.js
    │           3-search-sort.js
    │           4-history.js
    │           5-search.js
    │           6-main.js
    │           _wrap_open.txt
    │           _wrap_close.txt
    │           build_js.py
    └── templates/index.html
```


## USER PREFERENCES (CRITICAL)

This user works in a specific way. **Follow these always:**

1. **Describe intent → I plan → User approves → I implement**
   - They describe what they want
   - I break it into steps BEFORE writing any code
   - They ask questions or approve the plan
   - Only then do I implement
   - I update workspace files only — no code in the chat

2. **Never assume — ask questions** when requirements are ambiguous or missing info that would affect correctness.

3. **TXT files that are actually zip archives** — extract to workspace first, don't try to read as text.

4. **CSS load order is sacred** — `base.css` before `responsive.css`. Never change.

5. **Never hand-edit `app.js`** — it's generated from `src/` fragments. Edit fragments → run check.py.

6. **Don't re-litigate decided things** — the CHANGELOG documents decisions that are done. Check CHANGELOG first.

7. **Summarized answers only** — not verbose explanations unless asked.

8. **Only perform actions that won't time out the chat.**

---

## ACTIVE WORK

> Fill this in at the end of every chat. Describes what was being done and what's pending.

### Last session ended with these results:

**What was being worked on:** Refactoring `app.py` routes into Blueprints and splitting `services.py` to improve maintainability.

**Status:** Completed and verified.
- `services.py` $\rightarrow$ `cloud_service.py` + `search_service.py`.
- `app.py` routes $\rightarrow$ `routes/` (auth, cloud, search, history).
- Rate limiting decoupled from `create_app` via `extensions.py`.
- `auth_utils.py` created for session/client management.
- All checks pass via `check.py`.

**Pending fixes (from login audit):**
1. Storage check before add (implemented, but needs verification in the new structure)
2. Empty token guard (implemented)
3. Logout endpoint (still pending)
4. Redis health check on startup (implemented)
5. Replace broad except Exception handlers (partially implemented, 1 broad remaining)
6. Fix error messages leaking internal details (partially implemented)

---

## PENDING PLANS

> Any plans that were discussed but not yet implemented. Follow these if the user asks to continue.

*(None currently)*

---

## OPEN QUESTIONS

> Things not yet decided or ambiguous. Ask the user before proceeding if any of these become relevant.

*(None currently)*

---

## RECENT CHANGES LOG
- **2026-05-31** — 2026-05-31 — Code quality: magic numbers extracted, route docstrings added, no behavior change. Changed: streamly_hardened/app.py, streamly_hardened/services.py, streamly_hardened/redis_store.py, streamly_hardened/static/js/src/5-search.js, ai/deploy/check.py, ai/deploy/deploy_all.py.

- **2026-05-31** — 2026-05-31 — Security and reliability fixes (initial batch). Changed: streamly_hardened/app.py, streamly_hardened/services.py, streamly_hardened/redis_store.py, streamly_hardened/static/js/src/5-search.js, ai/deploy/check.py, ai/deploy/deploy_all.py.

- **2026-05-31** — 2026-05-31 — Security and reliability fixes (initial batch). Changed: streamly_hardened/app.py, streamly_hardened/services.py, streamly_hardened/redis_store.py, streamly_hardened/static/js/src/5-search.js, deploy/check.py, deploy/deploy_all.py.



- **2026-05-31** — Initial fix batch: storage check, empty token guard, Redis health check, specific exception handlers, safe error messages. Workspace optimized: deploy_all.py now reads from changes.json, QUICK.md + CHANGES.md created. All 5 fixes verified via check.py.






> Append entries here when something significant happens. Format: `[YYYY-MM-DD] What changed and why`


---

## KEY DESIGN DECISIONS

1. **Refresh token = full Token (not just refresh)** — stored as base64. seedrcc's `from_refresh_token()` crashes if the response omits a new refresh token. Storing the full token sidesteps that bug.

2. **Silent re-login** — On 401 from non-login endpoints, JS calls `/api/login/silent` (8s debounce). `init()` uses double-try: `/api/status` first → silent relogin fallback → forces Guest mode. Login popup can be dismissed (continue as guest) via × or Escape.

3. **Bitsearch DNS fallback via DoH** — scoped monkey-patch to `socket.getaddrinfo` via context manager + lock. Only active during the request. Cached 5 min. Only used after normal DNS fails.

4. **Upstash for persistence only** — TTLStore is in-process. RedisStore wraps Upstash HTTP API. Used for: SECRET_KEY, refresh token, magnet history. NOT for session storage.

5. **Asset cache-busting** — `asset_ver` computed as max mtime of all static files in `index()`. Template injects `?v={{ asset_ver }}`.

6. **URL hash routing** — `setTab()` writes `window.history.replaceState(null, null, #${name})`. Refreshing restores the correct tab.

7. **History is global** — Redis key `streamly:history:global_history`. Deduplicated by magnet. Capped at 50.

8. **Video player with StreamlyPlayer deep link** — `openItem()` detects video extensions (mp4, webm, mov, m4v, mkv, avi). Inline player + "External Player" button uses `streamlyplayer://play?url=...`.

9. **Paste → auto-detect magnet** — `pasteBtn` reads clipboard, fills input, and if it detects a magnet link, automatically calls `search()` which saves to history and adds to Seedr.

10. **Defensive attribute access** — ALL service attribute reads use `getattr(obj, attr, default)`. Never crashes on missing attributes.

11. **Suggest debounce** — 350ms wait + race guard: skips results if user kept typing and input no longer matches.

---

## MOBILE-SPECIFIC DECISIONS (don't regress)

- **Cloud Drive**: Seedr-style list. Tap=select, double-tap=open, ⋮ kebab=Download/Copy Link/Delete. Multi-select with bulk action bar above storage meter.
- **Search**: Desktop table kept, scaled to fit. Columns: NAME · SE · TIME · SIZE · ADD. Category + Leecher columns removed. Add button is icon-only via `font-size:0` + `::before` glyph (data-state: +/…/✓).
- **Search box**: Paste (📋) and Clear (✕) are in-field. Clear keeps results on screen.
- **Bug fixed**: `background-attachment: fixed` + `backdrop-filter` caused mobile blank/hang — removed.
- **Breakpoints**: ≤980px (tablet), ≤700px (mobile cloud/search changes), ≤420px (compact pagination).

---

## KNOWN TECH DEBT

1. **Duplicate `init()`** — both `1-core.js` and `6-main.js` define `init()`. Last one in bundle order wins. `6-main.js` runs last so its `init()` is active. Needs cleanup.

2. **`updateSelected()` mentioned in CHANGELOG as removed** — still in `2-cloud.js`. Verify if dead code.

3. **No frontend framework** — single IIFE closure with no module isolation. Fragment naming is the only namespacing.

4. **In-process session store** — users logged out on multi-worker Render deploys.

5. **Bitsearch rate limits** — shared IP on free tier. Cloudflare Worker proxy recommended (DEPLOY.md step 5).

6. **`deploy.bat` force-pushes** — `--force` on git push. Hardcoded WinPython path. Auto-tags with `good-YYYYMMDD-HHMMSS`. Rollback via `rollback.bat` uses git tags.

7. **`/api/suggest` has no CSRF** — read-only, rate-limited. Intentionally unauthenticated.

---

## BACKEND API SUMMARY

| Endpoint | Auth | CSRF | Rate cost | Notes |
|---|---|---|---|---|
| `GET /` | — | — | — | Serves index.html with asset_ver |
| `GET /healthz` | — | — | — | UptimeRobot target |
| `GET /api/csrf` | — | — | 0.2 | Returns new csrfToken |
| `GET /api/status` | — | — | 0.2 | Checks auth state |
| `POST /api/login` | — | ✅ | 5.0 | Returns username |
| `POST /api/login/silent` | — | — | 1.0 | Tries restore from refresh token |
| `GET /fs/folder/<id>/items` | ✅ | — | 1.0 | Folders + files + storage |
| `POST /api/add` | ✅ | ✅ | 2.0 | Add magnet to Seedr |
| `POST /api/delete` | ✅ | ✅ | 2.0 | Delete single file/folder |
| `POST /api/delete/bulk` | ✅ | ✅ | 3.0 | Delete up to 100 items |
| `POST /api/zip` | ✅ | ✅ | 2.0 | Zip URL for 1 item |
| `POST /api/zip/bulk` | ✅ | ✅ | 3.0 | Zip URL for up to 100 items |
| `GET /api/url?file_id=` | ✅ | — | 1.0 | Stream URL for file |
| `GET /api/suggest?q=` | — | — | 0.5 | IMDb title suggestions (no auth) |
| `GET /api/search` | — | — | 1.0 | Bitsearch results |
| `GET /api/history` | — | — | 1.0 | Global magnet history (Redis) |
| `POST /api/history/add` | ✅ | ✅ | — | Add magnet to history |
| `POST /api/history/delete` | ✅ | ✅ | — | Remove magnet from history |
| `POST /api/history/clear` | ✅ | ✅ | — | Clear entire history |

---

## ENVIRONMENT VARIABLES

| Var | Required | Default | Purpose |
|---|---|---|---|
| `SECRET_KEY` | Auto | Ephemeral | Flask session key. Auto-stored in Upstash. |
| `APP_ENV` | No | `production` | `production` or `development` |
| `PORT` | Render | `10000` | Container port |
| `UPSTASH_REDIS_REST_URL` | Rec. | — | Upstash database URL |
| `UPSTASH_REDIS_REST_TOKEN` | Rec. | — | Upstash REST token |
| `SEEDR_EMAIL` / `SEEDR_PASSWORD` | No | — | Headless single-account auto-login |
| `BITSEARCH_URL` | No | bitsearch.eu | Override (e.g. Cloudflare Worker) |
| `SESSION_TTL_SECONDS` | No | `43200` | Session expiry (12h) |
| `REQUEST_TIMEOUT_SECONDS` | No | `6.0` | HTTP request timeout |
| `ARCHIVE_TIMEOUT_SECONDS` | No | `10.0` | Zip archive fetch timeout |

---

## OPTIMIZED WORKFLOW (use this)

### Files at a glance
```
QUICK.md       ← 1-page reference (read first, every chat)
CHANGES.md     ← template + examples for changes.json
changes.json   ← current session's changes (edit this to deploy)
deploy_all.py  ← run this: writes files + rebuilds + verifies + docs + zip
check.py       ← smoke tests: JS brackets + CSS braces + Flask boots
deploy.bat     ← commit + push → Render auto-deploys
ACTIVITY_LOG.md ← deployment history
```

### How to make changes (3 steps)

**Step 1 — You:** Describe the changes fully in chat.

**Step 2 — Me:**
1. Read `QUICK.md` (1 page, instant)
2. Read the relevant code end-to-end
3. Write `changes.json` with the updated file contents
4. Run `python3 deploy/deploy_all.py`
5. Check.py runs automatically → all verified
6. Docs auto-updated (CHANGELOG, CONTEXT, ACTIVITY_LOG)
7. `project.zip` created

**Step 3 — You:**
1. Double-click `deploy/deploy.bat` → commit + push
2. Test on Render
3. Report bugs with exact error messages

### Git snapshot (safety net)
`deploy_all.py` auto-commits a `before-{timestamp}` git tag before applying changes. To revert:
```bash
git reset --hard before-20260531-143022
```

### Writing changes.json
See `CHANGES.md` for full template. Basic format:
```json
{
  "session": "YYYY-MM-DD — what changed",
  "changes": [{"file": "path", "content": "..."}]
}
```

### Checklist before writing changes
- [ ] Read current file from workspace
- [ ] Trace full data path (caller → callee)
- [ ] Check CHANGELOG for related decisions
- [ ] Handle null/empty/max edge cases
- [ ] CSS load order unchanged
- [ ] Never edit app.js directly
- [ ] Note judgment calls in comments

---

## WORKFLOW FOR MAKING CHANGES (legacy reference)

The optimized workflow above replaces this. kept for reference:
- Edit src/ fragments → check.py → deploy.bat (old manual way)
- New way: write changes.json → deploy_all.py (automated)

---

## CHANGELOG SOURCE NOTES

The original `CHANGELOG.md` (in workspace root) contains additional handoff details. Key things from it not captured above:

- **Abandoned/dead features removed from codebase:** Bridge badges CSS, magnet-paste CSS, Webtor branch, unused `updateSelected()` function. Do NOT re-add these.
- **`updateSelected()` is dead code** — NOT the same as `updateSelection()` which IS used. Do not confuse them.
- **`build_zip.sh`** — referenced in CHANGELOG as a script that rebuilds `/home/user/project.zip`. Not present in current workspace. May have been a local utility.
- **`check.py`** — CHANGELOG says "run before committing" at repo root. The actual working copy is `deploy/check.py`. Do not look for it at the root.
- **Windows deploy tooling** — uses portable WinPython 3.12.4 at a hardcoded path. Change `PYEXE=` in deploy/*.bat files to move Python.
- **Loop:** User edits src/ fragments → double-click `deploy/deploy.bat` → done.

---

## USER'S WORKING RULES (from RULES.md)

The user sets these. Always follow them:

1. **User describes what they want.** You do not ask for clarification unless critical.
2. **You break it into steps before writing code — no code yet.**
3. **User asks follow-up questions or approves the plan.**
4. **You provide before/after comparison** so only the requested modifications are made.
5. **Summarized answers only** — not verbose explanations unless asked.
6. **Code in the background, not in the conversation** — update workspace files only.
7. **Workspace contains a `project.zip`** that auto-updates when any file changes.
8. **Only perform actions that won't time out the chat.**
9. **When user uploads a `.txt` that is actually an archive (Arena.ai workaround), extract it into the workspace first.**
10. **Never assume — ask questions** when requirements are ambiguous or missing info that would affect correctness.

---

## USER'S USE CASE (personal context — not shared publicly)

**What this app is used for:** Pirating movies via Seedr — convenience layer over Seedr.
- User has been using Seedr for 7 years
- Pain point: too many steps to add torrents to Seedr
- Custom video player that plays files directly (Seedr doesn't allow this)
- History as a permanent wishlist — survives accidental Seedr deletion
- Shared Seedr account — anyone with the link has access, no credentials for end users

**Previous chat's topic:** Making changes quickly and accurately without requiring debugging.

---

## GLOBAL LOGIN ARCHITECTURE

**Status:** Already implemented. Shared Seedr account via `SEEDR_EMAIL` + `SEEDR_PASSWORD` env vars + Redis token persistence.

**Key flows:**
1. Manual login → token stored in Redis (survives server restarts)
2. Auto-login on server restart → env vars used as fallback
3. Seedr password change → login form pops up (fail-safe)
4. No logout endpoint (not needed for single-user/shared-account use)
5. 3-4 concurrent users handled fine

---

## PENDING FIXES (from login audit)

These were decided and are waiting to be implemented:

1. **Storage check before add** — before `/api/add` fires, query storage via `/fs/folder/0/items`. Block if torrent size > available space. Prevents wasting storage on files that won't fit.

2. **Empty token guard** — `serialize_token()` can return `""` silently. Add validation to reject empty strings from being stored in Redis.

3. **Logout endpoint** — clears Redis token + TTLStore + session. Not critical for use case but keeps things clean.

4. **Redis health check on startup** — if Upstash is unreachable on boot, log a clear warning so the user knows before errors cascade.

5. **Fix broad `except Exception`** handlers — 13 places catch all exceptions. Replace with specific exception types.

6. **Fix error messages** — `str(exc)` and `str(e)` leak internal details to users. Use generic messages.

---

## USER PREFERENCES FOR WORK

- **Plan first, code later** — describe what you want, I break it into steps, you approve, then I implement
- **Summarized answers only** — not verbose
- **No debugging sessions** — changes should be accurate enough to not need debugging. If something might not work, I say so before writing code
- **Workspace files only** — no code in the chat
- **Will return to this** — user will come back to implement the 6 pending fixes listed above
