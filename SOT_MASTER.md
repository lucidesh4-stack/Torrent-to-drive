# 🧠 SOT_MASTER: CloudFlow Single Source of Truth

> This is the High-Density Context Map for the inheriting agent. 

## 🏗️ Core Architecture
CloudFlow is a Flask-based SPA designed as a high-performance Seedr client.

### 1. Backend Stack (Python/Flask)
- **Pattern**: Service-Route Decoupling.
- **API Layer**: `routes/` (Blueprints). Purely handles request/response.
- **Logic Layer**: `search_service.py` and `cloud_service.py`. Pure business logic.
- **Persistence**: Upstash Redis (`redis_store.py`). Used for sessions, magnet history, and logging.
- **Security**: `security.py` implements token guards and config validation.

### 2. Frontend Stack (JS/CSS)
- **Pattern**: Fragmented JS $\rightarrow$ Bundled `app.js`.
- **Build Process**: `build_js.py` concatenates `src/*.js` fragments. **CRITICAL**: Do not edit `app.js` directly; edit the fragments in `src/`.
- **UI**: Vanilla JS with a custom accordion-based grouped renderer for Series mode.

### 3. The "Series Mode" Logic (Highest Complexity)
The Series search is a multi-round orchestration:
- **Sequence**: Broad Query $\rightarrow$ Pack Queries $\rightarrow$ Encoder Queries.
- **Grouping**: Encoder $\rightarrow$ Quality (4K/1080p/720p) $\rightarrow$ Season $\rightarrow$ Episode.
- **Dedup**: Highest-seeder wins per `<series>+SxxExx` per encoder.
- **Relevance**: Exact title matching (prefix for movies) to eliminate spin-offs.

## 🚦 Operational Status
- **Stability**: Hardened. Architecture refactor complete.
- **Active Guard**: The Zero-Regression Protocol (ZRP) is the only way to maintain this stability.

## 📋 Decision Ledger (Key Trade-offs)
- **Provider Failover**: We moved from "Merge all" to "First-provider-to-yield-results" to kill cross-source duplicates.
- **Normal Mode**: Simplified to a single broad query $\rightarrow$ local quality filter $\rightarrow$ size-ascending display.
- **Daily Meter**: Removed for better UX; now using raw provider counts for debugging.
