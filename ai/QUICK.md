# Streamly — Quick Reference

> 1 page. Read this first in every chat. Go straight to work.

---

## What this is
Flask app — Seedr client + torrent search + custom video player + history.
Deployed on Render (Docker). Persistence via Upstash Redis.

---

## File layout

```
workspace/
├── ai/                    ← all AI-maintained files
│   ├── QUICK.md           ← you are here (1-page reference)
│   ├── CHANGES.md         ← template + examples for changes.json
│   ├── CONTEXT.md         ← full project state (read on resume)
│   ├── RULES.md           ← user's working rules
│   ├── CHANGELOG.md       ← decision history
│   ├── ACTIVITY_LOG.md    ← deployment history
│   ├── changes.json       ← current session changes (edit this)
│   └── deploy/
│       ├── deploy_all.py  ← single-command: write + verify + docs + zip
│       ├── check.py       ← JS + CSS + Flask smoke test
│       ├── deploy.bat     ← git commit + push (Render auto-deploys)
│       ├── check.bat
│       └── build.bat
├── render.yaml            ← Render deployment config (stays at root)
├── project.zip            ← auto-generated zip (from workspace root)
└── streamly_hardened/     ← source code
    ├── app.py
    ├── config.py
    ├── security.py
    ├── services.py
    ├── store.py
    ├── redis_store.py
    ├── __init__.py
    ├── static/
    │   ├── css/base.css         ← load FIRST
    │   ├── css/responsive.css   ← load SECOND
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

---

## DON'T TOUCH

- `streamly_hardened/static/js/app.js` — generated from src/ fragments
- CSS load order: `base.css` BEFORE `responsive.css`
- CHANGELOG.md decisions — don't re-litigate

---

## Env vars

| Var | Required | Default | Purpose |
|---|---|---|---|
| `SECRET_KEY` | auto | ephemeral | Flask session key |
| `UPSTASH_REDIS_REST_URL` | rec | — | History + token persistence |
| `UPSTASH_REDIS_REST_TOKEN` | rec | — | History + token persistence |
| `SEEDR_EMAIL` | no | — | Headless auto-login |
| `SEEDR_PASSWORD` | no | — | Headless auto-login |
| `BITSEARCH_URL` | no | bitsearch.eu | Override (e.g. Cloudflare Worker) |
| `SESSION_TTL_SECONDS` | no | 43200 | Session expiry |
| `APP_ENV` | no | production | production/development |

---

## Workflow (3 steps)

### You
1. Edit `ai/changes.json` with your changes (see CHANGES.md for template)
2. Run: `python3 ai/deploy/deploy_all.py`
3. Test on Render after `ai/deploy/deploy.bat`
4. Report bugs with exact error messages

### AI
1. Read QUICK.md → work
2. Write `ai/changes.json` with new content
3. Run `python3 ai/deploy/deploy_all.py`
4. Done. Check.py verified. Docs auto-updated. Zip ready.

### Deploy
`ai/deploy/deploy.bat` → commit + push → Render auto-deploys

---

## Changes.json format

```json
{
  "session": "YYYY-MM-DD — brief description",
  "changes": [
    {"file": "streamly_hardened/app.py", "content": "...full file..."},
    {"file": "streamly_hardened/static/js/src/5-search.js", "content": "...full file..."}
  ]
}
```

See `CHANGES.md` for examples.

---

## Pre-flight (check.py output)

```
[OK] Storage check before add
[OK] Empty token guard
[OK] Redis health check
[OK] Specific exception handlers
[OK] Safe error messages
[XX] Logout endpoint (user declined)
  5/6 fixes implemented
```

---

## Key decisions (don't re-litigate)

- Refresh token = full Token (not just refresh)
- Bitsearch DNS fallback via scoped DoH
- History is global (same Redis key for all users)
- Mobile cloud = Seedr-style list (not desktop table)
- Search on mobile = scaled desktop table (not card view)
- CSS: base.css load FIRST, responsive.css load SECOND
- Error handler catches ConnectionError + TimeoutError (not bare Exception)
