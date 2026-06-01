# Streamly Audit Report — Deadweight, Optimisations, Errors

Date: 2026-06-01
Scope: current workspace source + uploaded `streamly.log` analysis.

---

## 1. Executive Summary

The app is functional and substantially cleaner after the recent high-confidence cleanup, but there are still three categories of work remaining:

1. **Security/logging fixes** — highest priority: sensitive Seedr tokens are present in historical logs because HTTP client libraries logged full URLs.
2. **Performance/reliability optimisations** — cloud storage/transfer polling and search parsing/provider behavior can be made lighter and more deterministic.
3. **Remaining deadweight / refactor candidates** — mostly compatibility leftovers, stale support files, overlapping CSS, and architectural debt.

---

## 2. Confirmed Log Findings

Source log: `uploads/streamly.log`

Summary:

- Total lines: `1072`
- `ERROR`: `1`
- `WARNING`: `75`
- `CRITICAL`: `0`
- `INFO`: `973`

### 2.1 Critical: Seedr access tokens leaked into logs

**What**

`httpx` logged full Seedr request URLs at INFO level, including query params such as:

```text
access_token=...
```

**Why it matters**

Access tokens are credentials. If logs are downloaded, copied, or exposed, a token can be abused until expiry/revocation.

**Status**

Historical log confirms leakage. Current source still configures root logging at INFO; library logger suppression is only present in `if __name__ == "__main__"`, not guaranteed inside `create_app()` on Render/Gunicorn.

**Fix**

- In `create_app()`, set:
  - `logging.getLogger("httpx").setLevel(logging.WARNING)`
  - `logging.getLogger("httpcore").setLevel(logging.WARNING)`
  - `logging.getLogger("urllib3").setLevel(logging.WARNING)`
- Add a redaction filter for all handlers:
  - `access_token=REDACTED`
  - `refresh_token=REDACTED`
  - `token=REDACTED`
  - `c=REDACTED` for Seedr signed progress URLs

**Priority**: P0

---

### 2.2 High: Seedr signed transfer progress URLs leaked

**What**

Logs include URLs like:

```text
subnode_actions.php?action=torrent_progress&torrent_id=...&c=...&t=...
```

**Why it matters**

The `c=` query param appears to be a signed/temporary auth value for transfer progress.

**Fix**

Same as 2.1: suppress HTTP client INFO logs and add redaction.

**Priority**: P0

---

### 2.3 Historical unhandled 413 / too-large Seedr add

**What**

One historical traceback:

```text
seedrcc.exceptions.APIError: API request failed.
HTTP/1.1 413 Payload Too Large
```

**Status**

Likely already fixed by current `CloudService.add_magnet()` converting Seedr APIError/413 into `ConnectionError` with a clear user-facing message.

**Fix needed**

No immediate fix unless it reappears in fresh logs.

**Priority**: Done / monitor

---

### 2.4 Repeated Seedr “too large / storage full” warnings

**What**

Warnings show Seedr rejected some add requests because torrent was too large.

**Why**

Search-result adds have `size_bytes`, so the app can pre-check storage. Raw magnets from clipboard/URL do not know the size before adding.

**Fix**

For raw magnet auto-add:

- Check free storage before auto-add.
- If free space is below a threshold, do not auto-add unknown-size magnets.
- Show: `Storage is low. Magnet size unknown. Tap Add to force.`

**Priority**: P2

---

### 2.5 Bitsearch warning spam

**What**

Many warnings show:

```text
Bitsearch request failed: 500 / 502 / 520 / timeout
```

**Status**

Search failover reduces user impact, but logs are noisy.

**Fix options**

- Remove Bitsearch from default providers:
  - `SEARCH_PROVIDERS=apibay,torrents-csv`
- Or add provider cooldown after repeated failures.
- Or demote repeated Bitsearch failures to INFO after first warning per time window.

**Priority**: P2

---

### 2.6 Duplicate startup log lines

**What**

Repeated startup pairs:

```text
Upstash Redis reachable — history, token & log persistence active
Upstash Redis reachable — history, token & log persistence active
```

**Possible causes**

- Multiple workers booting normally.
- Root logger handlers added multiple times by repeated `create_app()` calls.

**Fix**

Make logging setup idempotent:

- Do not add duplicate Streamly console/Redis handlers if already present.
- Mark handlers with a private attribute, e.g. `_streamly_handler = True`.

**Priority**: P1

---

## 3. Remaining Deadweight / Cleanup Candidates

These are not all equally safe to remove immediately. They are grouped by confidence.

---

### 3.1 High-confidence remaining workspace/support deadweight

| Item | What | Reason | Action |
|---|---|---|---|
| `ai/changes.json` | Old deploy payload | Contains stale full-file secure logging implementation, not current source of truth | Remove or regenerate only when using deploy tool |
| `ai/CHANGES.md` | Instructions for `changes.json` | Useful only for old deploy workflow | Keep only if still using deploy tool |
| `migration_map.txt` | Empty file | 0 bytes | Remove if not used externally |
| `DEPLOY.md` | Deploy notes | Keep if used; otherwise support doc only | Optional |
| `ai/deploy/*.bat` | Windows deploy scripts | Dead if not deploying from Windows | Optional |
| `ai/deploy/check.py` | Legacy check script | Appears stale relative to current code | Refresh or remove |

---

### 3.2 Compatibility leftovers in app source

| Item | What | Reason not removed yet | Recommendation |
|---|---|---|---|
| `userPill` DOM/CSS/JS | Hidden old right-side username pill | Still used as fallback in silent-login code | Replace fallback with explicit variable, then remove DOM/CSS |
| `pathLabel` guard | Old side-card folder ID label | DOM removed; JS guard remains harmless | Remove guard if no UI will restore folder ID |
| `storageMeter` / `storageText` guards | Old side-card storage meter | DOM removed; topbar/mobile storage remain | Remove guards after confirming no desktop side storage return |
| `resultCount` DOM | Hidden/mostly unused on mobile; blanked in JS | Kept for status layout compatibility | Remove or repurpose |

---

### 3.3 CSS deadweight / cascade debt

| Item | Issue | Recommendation |
|---|---|---|
| Multiple mobile search CSS blocks | Older mobile rules and V2 patch rules overlap | Consolidate into one authoritative mobile-search section |
| Duplicate `.episode-row` mobile styling | Earlier block then later override | Merge to one compact row definition |
| Old comments mentioning Add All | Add All removed | Update comments to match current behavior |
| Broad `#searchView .table` mobile styles | Some old table path removed; remaining table rules may only affect history/cloud if scoped wrong | Re-audit selectors after cleanup |
| Inline styles in `index.html` | Many one-off style attributes | Move to CSS classes for maintainability and CSP hardening |

---

### 3.4 Python deadweight / questionable code

| Item | What | Reason | Recommendation |
|---|---|---|---|
| `NORMAL_TOP_PER_QUALITY` | Old cap constant | Normal mode now calls `cap=None`; constant has no effect | Remove if no tests depend on it |
| `_extract_quality()` + `parse_release()["quality"]` | Fine-grained quality label | Current grouping uses `_quality_bucket`; likely unused | Remove only after verifying no route/UI/test needs it |
| `SearchService.bitsearch()` pagination payload | Old page-based response | Current provider flow only needs canonical rows | Simplify Bitsearch provider to row-fetch only |
| Bitsearch DNS fallback machinery | DoH + temporary DNS monkeypatch | Complex for last-priority flaky provider | Keep only if Bitsearch is required; otherwise remove Bitsearch provider |
| `stable_json_dumps()` | JSON helper | Need call audit; appears likely unused | Remove if grep confirms no usage |

---

## 4. Optimisation Opportunities

---

### 4.1 Cloud / Seedr provider calls

| Issue | Current behavior | Optimisation |
|---|---|---|
| Storage check before add is heavy | `/api/add` calls `cloud.list_items()`, which can list files and enrich transfer progress | Add `CloudService.get_storage()` lightweight method |
| Cloud listing calls settings every time | `list_items()` calls `client.get_settings()` after `list_contents()` | Use `contents.space_used/space_max` when available; fallback to settings only if missing |
| Transfer progress is sequential | One progress URL call per active torrent | Fetch progress lazily, concurrently, or via a separate endpoint |
| Cloud transfer polling reloads full folder | Every 5s while transfer exists | Split `/api/transfers`; only reload files when transfer completes |
| Auto-refresh fixed interval | Always 5s | Add exponential/backoff or pause when tab hidden |

---

### 4.2 Search backend

| Issue | Current behavior | Optimisation |
|---|---|---|
| Repeated parsing | `parse_release()` used in relevance, packs, grouping | Cache parse result per row per request |
| Series mode query count | Broad + pack + encoder rounds | Keep quota, but cache repeated broad/provider lock results |
| Provider failover can miss later-provider results | First provider with results wins | Intentional tradeoff; optionally add user “deep search” mode |
| Bitsearch warnings | Provider frequently fails | Remove Bitsearch by default or add cooldown |
| Normal mode broad result quality filtering | One provider result set only | Good for speed; might miss quality-specific results | Optional deep mode for missing qualities |

---

### 4.3 Frontend performance

| Issue | Current behavior | Optimisation |
|---|---|---|
| Global JS namespace | All fragments share globals | Introduce `window.Streamly = { cloud, search, history }` namespaces |
| No tree-shaking | `build_js.py` concatenates fragments | Accept for small app, or migrate to simple bundler later |
| Full re-render on quality/season change | Rebuilds grouped DOM | For large series, update active section only |
| Clipboard checks | Focus/visibility/pointer checks read clipboard when allowed | Add user setting toggle to disable auto-add clipboard |
| Suggestions | External IMDb request after debounce | Add local abort/race cancellation and cache suggestions per query |

---

### 4.4 UI/UX optimisation

| Issue | Recommendation |
|---|---|
| Clipboard auto-add can surprise users | Add visible toggle: `Auto-add copied magnets` |
| Duplicate magnet skip is only status text | Add toast as well; status may be off-screen |
| Transfers mixed with file rows | Create distinct “Transfers” section above files |
| Native `confirm()` for cancel/delete | Replace with app modal/bottom-sheet confirmation |
| Mobile filter sheet not persisted | Persist last chosen filters in localStorage |
| Quality tabs only show returned qualities | Show selected qualities even when empty with disabled/zero state |
| History modal table | Convert to card layout instead of table patching |
| Desktop selected-item side-card | Consider command bar/top actions for multi-select instead of side-only panel |

---

## 5. Reliability / Security Hardening

| Area | Issue | Recommendation | Priority |
|---|---|---|---|
| Logs | Sensitive query params leak | Redaction filter + library logger levels in `create_app()` | P0 |
| Logs | Duplicate handlers possible | Idempotent logging setup | P1 |
| Sessions | In-process `TTLStore` | Redis-backed session/client token store | P1 |
| CSRF/logs | `/api/logs` credential POST lacks route rate limit | Add `@rate_limited` | P1 |
| CSP | Uses `'unsafe-inline'` | Move inline JS/styles out, tighten CSP | P2 |
| Exceptions | Broad catches in provider/bulk paths | Narrow or document intentional broad catches | P2 |
| Raw magnets | Unknown size before add | Low-storage guard / confirmation | P2 |
| Clipboard | Auto-read privacy | User toggle and clear status | P2 |

---

## 6. Issues Already Fixed / No Immediate Action

| Issue | Status |
|---|---|
| Old unhandled Seedr 413 APIError | Fixed in current source via `CloudService.add_magnet()` handling |
| Old flat search table/pagination dead path | Removed |
| Add All buttons/function | Removed |
| Category UI/backend validation | Removed |
| Daily Bitsearch meter | Removed earlier |
| Desktop side-card storage block | Removed earlier |
| Active transfer visibility | Implemented |
| Transfer cancel | Implemented |
| Mobile Search V2 | Implemented and polished |
| Workspace upload/cache/log deadweight | Removed from workspace and zip |

---

## 7. Recommended Next Work Order

### P0 — Must fix

1. Add logging redaction filter.
2. Suppress `httpx/httpcore/urllib3` INFO logs in `create_app()`.

### P1 — Reliability

3. Make logging setup idempotent.
4. Add rate limit to `/api/logs`.
5. Add lightweight storage-only method for `/api/add` checks.
6. Use `contents.space_used/space_max` before calling `get_settings()`.

### P2 — UX/performance

7. Add low-storage guard for raw magnet auto-add.
8. Split transfer polling into `/api/transfers`.
9. Add clipboard auto-add setting toggle.
10. Consolidate mobile search CSS.

### P3 — Cleanup/refactor

11. Remove/refresh stale `ai/changes.json` and deploy scripts if not used.
12. Namespace frontend JS.
13. Simplify or remove Bitsearch provider.
14. Split runtime/dev requirements.

---

## 8. Notes

- Historical logs contain sensitive tokens. Treat the uploaded `streamly.log` as sensitive and do not share it.
- If those tokens are still valid, rotate/revoke Seedr tokens by logging out/re-authenticating or changing credentials if necessary.
- After implementing log redaction, verify with a fresh log sample that no `access_token=`, `refresh_token=`, or progress `c=` values appear.
