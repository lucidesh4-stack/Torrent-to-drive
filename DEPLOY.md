# Deploying CloudFlow to Render (free tier)

End state: a public HTTPS URL like `https://cloudflow-yourname.onrender.com`
that you share with friends. Zero setup on their end.

CloudFlow = Seedr-based torrent search, cloud drive, and streaming client. No Colab bridge required.

---

## 1. Create an Upstash Redis (free, 2 min)

Used only to persist `SECRET_KEY` so users stay logged in across Render restarts.

1. <https://upstash.com> → sign up (GitHub login is fastest)
2. **Create Database** → name: `cloudflow` → region: closest to your Render region
3. Open the DB → **REST API** tab → copy:
   - `UPSTASH_REDIS_REST_URL`
   - `UPSTASH_REDIS_REST_TOKEN`

## 2. Push code to GitHub

```bash
cd "Web based"
git init && git add . && git commit -m "Initial CloudFlow deploy"
git branch -M main
git remote add origin https://github.com/<YOU>/cloudflow.git
git push -u origin main
```

## 3. Deploy on Render

1. <https://render.com> → sign up → **New +** → **Blueprint**
2. Connect your GitHub repo → Render auto-detects `render.yaml`
3. Click **Apply** — first build ~5 min
4. After deploy, open **cloudflow** service → **Environment** tab
5. Paste the two Upstash values from step 1
6. **Save changes** → Render redeploys (~1 min)
7. Open the service URL → login screen ✅

## 4. Keep it awake (UptimeRobot, 2 min)

Render free sleeps after 15 min idle (~30s cold start).

1. <https://uptimerobot.com> → free account
2. **Add New Monitor**:
   - Type: HTTP(s)
   - URL: `https://YOUR-APP.onrender.com/healthz`
   - Interval: 5 minutes
3. Save. Done.

## 5. (Optional) Cloudflare Worker for bitsearch proxy

Bypasses shared-IP rate limits on bitsearch.eu and adds 24h response caching.
See `cloudflare-worker/DEPLOY_WORKER.md` for instructions.

After deploying the worker, add this env var on Render:
- `BITSEARCH_URL` = `https://YOUR-WORKER.workers.dev/api/v1/search`

## 6. Share with your friend

Send them: `https://YOUR-APP.onrender.com`

They:
1. Log in with their own Seedr account (or close the dialog and use Guest Mode for search-only)
2. Search a torrent OR paste a magnet link
3. Click **Add** → torrent goes straight to their Seedr cloud

---

## Local dev

```bash
cd streamly_hardened
APP_ENV=development SECRET_KEY=dev python -m streamly_hardened.app
# http://127.0.0.1:5000
```

No Upstash vars → an ephemeral SECRET_KEY is generated per run.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `RuntimeError: SECRET_KEY must be set` | Add `SECRET_KEY` env var (Render auto-generates this via `generateValue: true`) |
| Users logged out after every redeploy | Upstash vars missing → SECRET_KEY changes each boot |
| Cold start every visit | UptimeRobot not configured / interval too long |
| `502 Bad Gateway` | Container crashed — check Render logs |
| Search returns no results | All configured providers empty/unavailable; default order is apibay → bitsearch → torrents-csv |
