---
trigger: always_on
description: Mandatory rules and guidelines for Telegram Multi-Daemon Parallel Uploads and Sequential Delivery in Streamly/CloudFlow.
---

# Telegram Multi-Daemon & Sequential Upload Guidelines

1. **Local C++ TDLib Daemon CLI Flags**:
   - Always use long option format: `--verbosity=0`, `--max-connections=10000`.
   - Do NOT use single-letter flag syntax `-v=0` or unsupported flags `--threads`.

2. **HTTP Timeouts for Local TDLib Daemons**:
   - Always configure `httpx.Timeout(1800.0, connect=60.0)` on HTTP clients posting to local daemon ports (8081, 8082, 8083).

3. **Telethon MTProto Mutex Serialization**:
   - Always wrap Telethon user session MTProto `upload_file` calls in `async with _TELETHON_MTPROTO_LOCK:` to prevent Telegram `FloodWaitError` rate limits.

4. **Sequential Completion Gate Buffer**:
   - Enforce natural folder/episode ordering during batch enqueueing.
   - Use `wait_for_sequence_turn` and `advance_sequence_turn` to hold finished uploads until their sequence turn arrives.

5. **Multi-Parallel UI Rendering**:
   - Return `active_items` array in progress endpoints and sort active cards by `seq_num` in `4b-telegram-transfers.js`.
