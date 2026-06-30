# Optimized Telegram Pipeline

## Key Fixes

### 1. Upload Chunk Size
```python
# BEFORE (default 256KB):
# Uploading file of 750001589 bytes in 2862 chunks of 262144

# AFTER (optimized 2MB):
# Uploading file of 750001589 bytes in ~360 chunks of 2097152
```

### 2. Progress Bar
```
Download phase:
  📥 DL: 105MB | 84.5 Mbps

Upload phase:
  📤 UL: 50MB | 48.2 Mbps
```

## Usage

```bash
export TELEGRAM_API_ID=12345
export TELEGRAM_API_HASH=abc123
export TELEGRAM_BOT_TOKEN=token
export CHAT_ID=123456789

python3 telegram_pipeline.py
```

## Code

```python
from telegram_pipeline import MediaPipeline, upload_with_progress

# Full pipeline
pipeline = MediaPipeline(api_id, api_hash, bot_token)
result = await pipeline.run(seedr_url, chat_id)

# Or just upload a file
result = await upload_with_progress(
    file_path=Path("/tmp/video.mkv"),
    chat_id=123456789,
    api_id=12345,
    api_hash="abc123",
    bot_token="token",
)
```

## Technical Details

| Metric | Default | Optimized | Improvement |
|--------|---------|-----------|-------------|
| Upload chunk size | 256KB | 2MB | **8x fewer requests** |
| Chunks for 750MB | ~2900 | ~360 | **8x less overhead** |
