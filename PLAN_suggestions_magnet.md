# PLAN — Suggestions Overlay Positioning + Magnet Auto-Detect Guard
> Author: Arena planner. Executor/Deployer: **Antigravity**. Scope: mobile UI + magnet auto-detect.
> Repo root: `Streamly/streamly_hardened/`. NEVER edit `static/js/app.js` directly — edit `src/*.js`, then run `build_js.py`.

---

## REQUIREMENT 1 — Mobile suggestions: directly below the search bar, full bar width, glued on scroll

### Root cause
- `getSuggestions()` mobile branch measures `.search-box-wrap` (input-only; excludes the filter + search action buttons) → dropdown is **narrower** and **left-offset** vs the bar.
- `position:fixed` is computed once with no scroll/resize tracking → detaches/floats when the page scrolls.
- Mobile `.suggest-box` background is translucent → underlying results bleed through (looks broken).

### File: `static/js/src/5-search.js`

**BEFORE** (inside `getSuggestions()`, the `if (isMobileSearchUi()) { ... } else { ... }` block ~lines 323–338):
```js
        if (isMobileSearchUi()) {
          const wrap = $("searchQuery").closest(".search-box-wrap") || $("searchQuery");
          const rect = wrap.getBoundingClientRect();
          box.style.position = "fixed";
          box.style.top = (rect.bottom + 6) + "px";
          box.style.left = rect.left + "px";
          box.style.width = rect.width + "px";
          box.style.zIndex = "9900";
        } else {
          box.style.position = "";
          box.style.top = "";
          box.style.left = "";
          box.style.width = "";
          box.style.zIndex = "";
        }
```

**AFTER** (call a shared positioner; measure the FULL bar `.search-bar-integrated`):
```js
        positionSuggestBox();
```

Add a module-level helper (near the other suggestion helpers, e.g. just above `getSuggestions`):
```js
  function positionSuggestBox() {
    const box = $("suggestBox");
    if (!box || box.classList.contains("hidden")) return;
    if (isMobileSearchUi()) {
      // Anchor to the FULL search bar (includes filter + search buttons),
      // so the dropdown matches the bar width and sits directly beneath it.
      const bar = $("searchQuery").closest(".search-bar-integrated")
                || $("searchQuery").closest(".search-box-wrap")
                || $("searchQuery");
      const rect = bar.getBoundingClientRect();
      box.style.position = "fixed";
      box.style.top = (rect.bottom + 6) + "px";
      box.style.left = rect.left + "px";
      box.style.width = rect.width + "px";
      box.style.zIndex = "9900";
    } else {
      box.style.position = "";
      box.style.top = "";
      box.style.left = "";
      box.style.width = "";
      box.style.zIndex = "";
    }
  }
```

Keep it glued — add ONCE (top of the search wiring, or end of file inside the IIFE):
```js
  window.addEventListener("scroll", () => positionSuggestBox(), { passive: true });
  window.addEventListener("resize", () => positionSuggestBox(), { passive: true });
```
> Note: `positionSuggestBox()` self-guards (returns when box is `.hidden`), so these listeners are cheap no-ops when no suggestions are showing.

### File: `static/css/responsive.css`

**BEFORE** (mobile patch ~lines 773–775):
```css
  #searchView .suggest-box {
    max-height: 360px !important;
  }
```

**AFTER** (kill bleed-through by making the panel near-opaque on mobile):
```css
  #searchView .suggest-box {
    max-height: 360px !important;
    background: rgba(11, 14, 20, 0.97) !important;
  }
```

> Desktop path is intentionally NOT changed (screenshot 3 confirms it is correct: full width, just below bar).

---

## REQUIREMENT 2 — Magnet auto-detect must NOT fire when the search box already has text

### Intent
The **clipboard/URL auto-paste detection** (which overwrites the input on tab-focus / page-load) must be suppressed if the user already typed something. A magnet the user types/pastes themselves must keep working — so DO NOT touch `maybeAutoAddMagnet(..., "input"|"paste")`.

### File: `static/js/src/5-search.js`

**BEFORE** (`ingestClipboardMagnet`, ~lines 56–68):
```js
  async function ingestClipboardMagnet(autoAdd = true) {
    if (!navigator.clipboard || !navigator.clipboard.readText) return false;
    try {
```
**AFTER** (add the guard as the first line of the body):
```js
  async function ingestClipboardMagnet(autoAdd = true) {
    // Do not auto-detect/overwrite if the user already has text in the search box.
    if ($("searchQuery") && $("searchQuery").value.trim()) return false;
    if (!navigator.clipboard || !navigator.clipboard.readText) return false;
    try {
```

**BEFORE** (`ingestUrlMagnet`, ~lines 111–118):
```js
  function ingestUrlMagnet() {
    const magnet = extractMagnetFromUrl();
    if (!magnet) return false;
```
**AFTER**:
```js
  function ingestUrlMagnet() {
    // Do not auto-detect/overwrite if the user already has text in the search box.
    if ($("searchQuery") && $("searchQuery").value.trim()) return false;
    const magnet = extractMagnetFromUrl();
    if (!magnet) return false;
```

---

## STAGE 2 — Audit checklist (executor must verify all ✅)
- [ ] No new imports needed; no `ImportError` risk (JS/CSS only).
- [ ] No Python/Flask config touched (`current_app.config.get` rule N/A here).
- [ ] No `except Exception:` introduced.
- [ ] `isMobileSearchUi()` (in `3b-series.js`) left UNMODIFIED.
- [ ] Dismiss handlers (blur/Escape/outside-click/clearBtn in `6-main.js`) still hide `#suggestBox`.
- [ ] User-typed/pasted magnet flow (`maybeAutoAddMagnet`) still works.
- [ ] Implementation matches this plan 1:1.

## STAGE 3 — Build & lock
- [ ] Run `python build_js.py` in `static/js/` → `app.js` regenerated from fragments.
- [ ] Confirm app.js now reflects BOTH this change AND the pre-existing pending auto-add removal (decide keep/finish/revert that first — see warning below).
- [ ] Draft ledger entry in `ai/STATE.md` (SOT): "Mobile suggestion dropdown anchored to full `.search-bar-integrated` width with scroll/resize tracking + opaque panel; magnet auto-detect suppressed when search box already has text."
- [ ] Rebuild/deploy.

## ⚠️ Pre-existing dirty-tree warning
`src/1-core.js` + `src/5-search.js` already contain an **unfinished, un-bundled** removal of the magnet auto-add TTL logic, and `app.js` is **stale** vs the fragments. Running `build_js.py` will bundle that pending change too. Resolve (finish / keep / revert) before building so the deploy diff is intentional.
