# ANTIGRAVITY PROMPT - Telegram Upload / Send Function

---

## CONTEXT

Production codebase for downloading from Seedr and uploading to Telegram.
Achieves **80-100 Mbps** via Cloudflare Worker proxy.

**Key Fix**: Permission denied on `/app/temp_downloads` → Use `/tmp/streamly_downloads`

---

## SPEED RESULTS (Tested Live)

| Method | Speed | Status |
|--------|-------|--------|
| **Worker Proxy** | 80-100 Mbps | ✅ BEST |
| **Direct Stream** | 60-70 Mbps | ✅ Fallback |
| **Range Chunks** | 3-10 Mbps | ❌ AVOID |

---

## OPTIMAL CONFIGURATION

```python
# Use Worker proxy for maximum speed
downloader = OptimizedDownloader(
    worker_url="https://streamly-proxy.lucidesh.workers.dev/",
    temp_dir="/tmp/streamly_downloads",  # Docker fix
)
```

---

## CRITICAL RULES

### ✅ DO

1. **Use Worker proxy** - 80-100 Mbps (5x faster than Range)
2. **Use streaming** - No Range headers (avoids per-request overhead)
3. **Use /tmp for temp** - Docker compatibility fix
4. **Use native Telethon** - Handles chunking internally

### ❌ DON'T

1. **Don't use Range requests** - 10x slower than streaming
2. **Don't use /app for temp** - Permission denied in Docker
3. **Don't implement custom upload** - Causes FilePartsInvalidError

---

## ENVIRONMENT VARIABLES

```bash
# Docker - MUST USE /tmp
TEMP_DIR=/tmp/streamly_downloads

# Worker proxy
WORKER_URL=https://streamly-proxy.lucidesh.workers.dev/

# Telegram
TELEGRAM_API_ID=xxx
TELEGRAM_API_HASH=xxx
TELEGRAM_BOT_TOKEN=xxx
```

---

## FILE STRUCTURE

```
streamly_hardened/
├── core/http_client.py       # OptimizedDownloader (80-100 Mbps)
├── routes/telegram.py        # Fixed for Docker permissions
tests/
└── test_speed.py             # Speed comparison tests
```

---

## HANDOFF NOTES

1. Worker proxy gives 5x speed boost over Range requests
2. Direct stream is fallback when Worker blocked (403)
3. Always use `/tmp` for temp in Docker
4. Native Telethon upload_file() is stable

---

## END