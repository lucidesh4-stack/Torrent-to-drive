# Streamly — Project Context

> Single source of truth for resuming work on this project in any new chat.

---

## 🎯 What this is

**Streamly** is a self-hosted web app that:
- Searches torrents (via Bitsearch API)
- Lets users paste magnet links directly
- Adds torrents to **Seedr.cc** (cloud torrenting)
- Deployed publicly on Render so user can share with friends (zero setup)

## 👤 User context

- **OS:** Windows
- **GitHub:** `lucidesh4-stack/Torrent-to-drive` (public)
- **Local path:** `D:\Web based`
- **Render URL:** `https://streamly-xxxx.onrender.com`
- **Skill level:** Comfortable with PowerShell + dashboards. Not a developer.

## 🏗️ Live architecture

```
Friend's browser
      │
      ▼
Render.com (free, Docker) ─── reads/writes ──► Upstash Redis (free)
      │  Flask app                              ├── SECRET_KEY (persistent)
      │  Streamly UI + API                      └── streamly:refresh:<sid> (Seedr Token b64)
      │  /healthz pinged by UptimeRobot every 5 min
      │
      ▼ (when user adds magnet)
Seedr.cc API
```

## 📦 Code structure (inside `project.zip`)

```
Web based/
├── streamly_hardened/
│   ├── app.py                   ← routes + auth glue + auto-relogin
│   ├── config.py                ← env-var-driven AppConfig
│   ├── security.py              ← CSRF, rate limiter, validators
│   ├── services.py              ← Seedr + Bitsearch + IMDb; serialize_token + login_with_saved_token
│   ├── store.py                 ← in-memory TTLStore (live Seedr sessions)
│   ├── redis_store.py           ← Upstash REST wrapper (SECRET_KEY + refresh tokens, 30-day TTL)
│   ├── Dockerfile               ← Python 3.11 + gunicorn
│   ├── requirements.txt
│   ├── .dockerignore
│   ├── templates/index.html
│   └── static/{css,js}
├── render.yaml                  ← Render Blueprint
└── DEPLOY.md
```

## 🔑 Render env vars

- `SECRET_KEY` — auto-generated
- `UPSTASH_REDIS_REST_URL`
- `UPSTASH_REDIS_REST_TOKEN`
- `BITSEARCH_URL` (optional)
- `APP_ENV=production`
- `SESSION_TTL_SECONDS=43200`

## ✅ What's working

- Streamly live on Render, always-on (UptimeRobot)
- Search (Bitsearch 200/day quota)
- Magnet paste at top of Search tab → 1-click add to Seedr
- IMDb autocomplete on typing; clicking suggestion fills input (no auto-search)
- Login overlay dismissible (×, Escape) — Guest mode
- **Auto re-login via Seedr full Token (base64 in Upstash)** — silent across Render restarts. Popup only shows if token truly invalid.
- **Cloud drive multi-select** — checkbox column + Ctrl+click + Shift+click range
- **Bulk download** — N files trigger N forced-save downloads with 500ms spacing
- **Bulk zip** — N items combined into one Seedr archive
- **Bulk delete** — confirm + atomic via `/api/delete/bulk`
- Auto-deploy on `git push` (Render "On Commit")

## 🐛 Known limitations

- **Bitsearch 200 req/day** — shared from Render IP. Magnet paste is primary.
- **Shared instance** — all friends share one Seedr session. Fine for 1-3 trusted.
- **No bridge** — torrent→Drive removed (Colab too high maintenance). Files archived.
- **No move-to-folder** — Seedr SDK lacks move API. Skipped intentionally.

## 📜 Major decisions made

1. Render free + Upstash chosen over Fly.io (no CC required)
2. UptimeRobot keep-alive every 5 min
3. Upstash REST API used in command-array form `["SET", key, value]`
4. Bridge fully removed; Colab files archived
5. Cloudflare Worker removed
6. Login overlay dismissible for Guest Mode
7. Magnet paste = primary workflow
8. Multi-select uses both checkboxes AND Ctrl+click (max ergonomics)
9. **Auth: store full Token base64 (not just refresh_token) because seedrcc's `from_refresh_token()` crashes when Seedr doesn't rotate refresh tokens. Use `Seedr(token=Token.from_base64(...))` to restore.**

## 🚀 Update workflow

```powershell
cd "D:\Web based"
git add .
git commit -m "describe change"
git push
# Render auto-deploys (~1 min)
```

## 🎁 Open ideas (not started)

- Per-user accounts (each friend = own Seedr)
- Bitsearch API key for 1k/day
- Custom domain
- Find a Drive alternative less painful than Colab
- "Stalled" detection / better status FSM (was planned for bridge era, no longer needed)
