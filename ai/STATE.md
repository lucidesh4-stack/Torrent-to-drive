# Streamly Project State

> This is the Single Source of Truth (SOT) for the agent. Read this first in every new chat.

---

## ⚡ Quick Reference
- **What**: Flask app — Seedr client + torrent search + custom video player + history.
- **Deploy**: Docker on Render (free tier).
- **Persistence**: Upstash Redis.
- **ZRP**: Zero-Regression Protocol is **ACTIVE** and mandatory.
- **SOT**: This file (`STATE.md`) replaces QUICK, CONTEXT, CHANGELOG, and ACTIVITY_LOG.

---

## 📖 Project Context

### User Journey
1. Login with Seedr account (or Guest mode).
2. **Search**: Title $\rightarrow$ bitsearch.eu $\rightarrow$ Add $\rightarrow$ Seedr Cloud.
3. **Cloud Drive**: Browse, stream, download/zip, delete.
4. **History**: Global magnet history stored in Upstash Redis.

### Architecture
- **Backend**: Flask Blueprints (`auth`, `cloud`, `search`, `history`) + decoupled services (`cloud_service`, `search_service`).
- **Frontend**: Generated `app.js` from `src/` fragments.
- **Session**: In-process `TTLStore`.

### User Preferences
- **Workflow**: Describe $\rightarrow$ Plan $\rightarrow$ Approve $\rightarrow$ Implement.
- **ZRP**: Path trace, Type check, Dependency audit, Side-effect mapping.
- **Format**: Summarized answers, no code in chat, update workspace files only.

---

## 🛠️ Current State

### Active Work
- **Status**: Architecture refactor and stability hardening completed.
- **Pending**:
    1. Logging system (Proposed).
    2. JS Namespacing (Proposed).
    3. Typed API responses (Proposed).
    4. Redis Session Store (Proposed).

### Known Tech Debt
1. Duplicate `init()` in `1-core.js` and `6-main.js`.
2. Dead code `updateSelected()` in `2-cloud.js`.
3. In-process session store (logout on multi-worker).
4. Bitsearch rate limits.

---

## 📜 Decision Ledger

### 2026-05-31 — Deploy Crash Fix: RequestIDFilter app-context safety
- **Bug**: `RuntimeError: Working outside of application context` at boot → gunicorn "Worker failed to boot" → Render deploy exit 1.
- **Cause**: `RequestIDFilter.filter` read `g` (request-only); boot-time Redis health-check log fired with no app context.
- **Fix**: Wrap `g.get("request_id", ...)` in try/except RuntimeError, fallback to "system".
- **Files**: streamly_hardened/app.py
- **Verified**: gunicorn boot with Upstash env vars set (prev. crash condition) now succeeds; in-request logging unchanged.

### 2026-05-31 — 2026-05-31 — Secure Logging System Implementation
- Files: streamly_hardened/app.py, ai/deploy/check.py


### 2026-05-31 — 2026-05-31 — Secure Logging System Implementation
- Files: streamly_hardened/app.py, ai/deploy/check.py


### 2026-05-31 — 2026-05-31 — Secure Logging System Implementation
- Files: streamly_hardened/app.py, ai/deploy/check.py


### 2026-05-31 — 2026-05-31 — Secure Logging System Implementation
- Files: streamly_hardened/app.py, ai/deploy/check.py


### 2026-05-31 — Protocol Adoption & Workspace Hardening
- **Deterministic Development**: Adopted Zero-Regression Protocol (ZRP).
- **AI Optimization**: Consolidated fragmented docs into `STATE.md`.
- **Workspace**: Purged `__pycache__` and temp files.

### 2026-05-31 — Security and Reliability Fixes
- **Storage Guard**: Mandatory check in `/api/add` to prevent over-filling Seedr.
- **Token Guard**: `RedisStore` rejects empty refresh tokens.
- **Redis Health**: Boot-time connectivity check for Upstash.
- **Exceptions**: Replaced broad `except Exception` with `(ConnectionError, TimeoutError)`.
- **Error Safety**: Generic messages to prevent internal leak.

### 2026-05-31 — Architecture Refactor
- **Blueprints**: Split `app.py` into `routes/` blueprints.
- **Services**: Split `services.py` into `cloud_service.py` and `search_service.py`.
- **Polyfill**: `_get_cfg` in `security.py` handles diverse config objects.

---

## 🚀 Deployment Activity
[2026-05-31] Protocol Adoption & Workspace Cleanup — ai/QUICK.md
[2026-05-31] Code quality: magic numbers extracted, route docstrings added, no behavior change
[2026-05-31] Security and reliability fixes (initial batch)
[2026-05-31] Security and reliability fixes (initial batch)
[2026-05-31] Initial fix batch: storage check, empty token guard, Redis health check, specific exception handlers, safe error messages.

[2026-05-31] 2026-05-31 — Secure Logging System Implementation — streamly_hardened/app.py, ai/deploy/check.py


## 🔄 Recent Changes
- **2026-05-31** — Deploy crash fix: made `RequestIDFilter` context-safe (no more boot-time `RuntimeError: working outside of application context`). Changed: streamly_hardened/app.py.
- **2026-05-31** — 2026-05-31 — Secure Logging System Implementation. Changed: streamly_hardened/app.py, ai/deploy/check.py.
