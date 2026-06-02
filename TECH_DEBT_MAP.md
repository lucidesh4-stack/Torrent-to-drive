# ⚠️ TECH_DEBT_MAP: CloudFlow Landmines

> This document lists "Working but fragile" areas of the codebase. Handle with care.

## 1. JS Build Process (`static/js/build_js.py`)
- **The Debt**: The frontend is a collection of fragments.
- **The Risk**: If a fragment is renamed or the order in `build_js.py` is shifted, `app.js` will break due to dependency errors (e.g., `core.js` must load before `search.js`).
- **Guidance**: Only add new fragments to the end of the list.

## 2. Bitsearch Rate Limits
- **The Debt**: Bitsearch is the primary provider but is flaky.
- **The Risk**: Heavy multi-round Series searches can trigger 429/500 errors.
- **Guidance**: Always implement timeouts and failover to Apibay/Torrents-CSV.

## 3. Seedr 413 (Payload Too Large)
- **The Debt**: Seedr returns a 413 when a torrent exceeds available space.
- **The Risk**: If not caught specifically, this manifests as a generic 500 error.
- **Guidance**: Ensure `cloud_service.add_magnet` specifically catches `APIError` and maps it to a clear "Insufficient Space" message.

## 4. CSS Responsive Grid
- **The Debt**: Mobile search rows use a mix of Flexbox and Grid.
- **The Risk**: Adding new metadata columns to the search rows often clips the "Add" button on small screens.
- **Guidance**: Always verify changes in `responsive.css` using a 360px width viewport.

## 5. Redis Session Store
- **The Debt**: Current sessions are in-process or basic Redis.
- **The Risk**: Scaling to multiple Gunicorn workers might cause session instability if not handled by a centralized Redis store.
- **Guidance**: If implementing a Redis Session Store, ensure the `session_interface` is correctly configured in `app.py`.
