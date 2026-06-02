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
- **Log Access Hardening**: Restricted log download credentials checking to prevent blank login bypasses and timing attacks by validating configured credentials exist before doing `hmac.compare_digest` comparison.
- **Wave 1 Cleanup**: Purged unused functions and imports (including legacy `multi_search`, rate limiter `prune`, `stable_json_dumps`, and redundant local imports) to reduce technical debt and codebase noise.
- **Wave 2 Performance**: Added `@lru_cache` to `parse_release` (avoiding redundant regex calculations during sorting/grouping) and optimized `list_items` to pull storage quota data directly from `list_contents` (saving a redundant `get_settings` API call to Seedr).
- **Desktop Search UI Redesign**: Transitioned the search view from a sparse table layout to a dense dashboard app layout. Replaced the search dropdowns with a collapsible left sidebar filters panel and centered Quality navigation chips above results. Reshaped result rows as high-density cards containing an explicit Encoder column. Integrated DocumentFragment rendering to ensure smooth batch UI insertions. Modified `responsive.css` to hide the sidebar and Encoder column under 768px while maintaining the mobile filter sheet and responsive season chips.
- **UI/UX Upgrades (Batches 1-3)**: Refined typography using Google Font **Outfit**, established standard radii `--radius-*` variables, boosted muted text contrast, and refined accent colors. Aligned desktop filters and modes under a unified `.controls-group` container (with mobile `display: contents` grid preservation). Standardized spacing, resolved header-to-row alignments, and removed lopsided layout padding. Designed custom scrollbars, added smooth fade-in results animations, updated overlays with glassmorphic blurs, implemented a shimmering skeleton mockup search placeholder, and refactored the Add-to-Seedr actions to utilize platform-independent vector SVGs inside fixed-dimension buttons to eliminate layout-jitter.

