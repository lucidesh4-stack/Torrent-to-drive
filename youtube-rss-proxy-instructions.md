# Cloudflare Worker Deployment Instructions for YouTube RSS Proxy

Deploying the YouTube RSS proxy to Cloudflare Workers is quick and straightforward. You can deploy it using the **Cloudflare Dashboard** (Quickest) or the **Wrangler CLI** (Developer-friendly).

---

## Option A: Deployment via Cloudflare Dashboard (Quickest)

1. **Log in**: Go to [dash.cloudflare.com](https://dash.cloudflare.com) and log in to your account.
2. **Create Worker**:
   - In the left-hand sidebar, navigate to **Compute (Workers & Pages)** -> **Overview**.
   - Click **Create application** (or **Create Worker**).
   - Name your worker (e.g., `streamly-youtube-proxy`).
   - Click **Deploy** to initialize the default template.
3. **Upload Code**:
   - Click **Edit Code** to open the online IDE.
   - Replace the entire contents of `worker.js` (or the editor panel) with the contents of the [youtube-rss-proxy.js](file:///d:/Web%20based/Streamly/youtube-rss-proxy.js) file.
4. **Deploy & Save**:
   - Click **Save and Deploy** in the top right corner.
   - Note the deployed URL (e.g., `https://streamly-youtube-proxy.your-subdomain.workers.dev`).
5. **Update constant inside trailers.py**:
   - Update `_YOUTUBE_RSS_PROXY` inside [trailers.py](file:///d:/Web%20based/Streamly/streamly_hardened/routes/trailers.py) to match your custom subdomain if it differs from:
     `https://streamly-youtube-proxy.lucidesh.workers.dev`.

---

## Option B: Deployment via Wrangler CLI

If you have Node.js and Wrangler installed locally:

1. **Create Wrangler Configuration**:
   Create a `wrangler.toml` in your project workspace directory:
   ```toml
   name = "streamly-youtube-proxy"
   main = "youtube-rss-proxy.js"
   compatibility_date = "2024-01-01"
   ```
2. **Login to Wrangler**:
   ```bash
   npx wrangler login
   ```
3. **Deploy**:
   ```bash
   npx wrangler deploy
   ```
4. **Verify**:
   Once deployed, check that requests to `https://<your-worker-url>/?url=https%3A%2F%2Fwww.youtube.com%2Ffeeds%2Fvideos.xml%3Fchannel_id%3DUCuPivVjnfNo4mb3Oog_frZg` return the correct YouTube feed XML.
