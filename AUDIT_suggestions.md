# FULL AUDIT — Suggestions Feature (unified mobile + desktop)
> Planner artifact. Executor/Deployer: **Antigravity**. Goal: ONE code path, identical on mobile & desktop.

## Target behavior (acceptance criteria)
1. Appears when the user types **3+ characters**.
2. **350ms** debounce.
3. **Up to 5** results from IMDb.
4. Click a suggestion → fills `#searchQuery` and dismisses the box.
5. Visually **attached to the search bar** (full bar width, directly below it) and the panel
   **grows to fit up to 5 rows** (no detaching, no fixed-position fork, no scroll desync).

## DOM today (the problem)
```
.search-bar-integrated  (position:relative, overflow:visible, full width)
 ├── .search-box-wrap   (flex:1 — INPUT AREA ONLY, narrower than bar; position:relative)
 │    ├── input#searchQuery
 │    ├── button#pasteBtn / #clearSearchBtn
 │    └── div#suggestBox        <-- anchored to box-wrap, so never full bar width
 └── .search-bar-actions (filter + search buttons)
```

## Root causes (see AUDIT table in chat)
- A. `#suggestBox` nested in `.search-box-wrap` → anchors to input area, not full bar.
- B. Mobile JS `position:fixed` computed once, no scroll/resize tracking → detaches.
- C. No 5-cap on frontend (backend returns up to 10).
- D. `max-height:288px` with 60px rows shows ~4.8 rows → 5th clipped.
- E. Backend returns `{title,year,poster,id}`; frontend warns about missing `type/rating/poster_url`.
- F. `app.js` was never rebuilt from fragments → prior change not live.

---

## CHANGE SET (unified, single path)

### 1) `templates/index.html` — move `#suggestBox` to the full bar
**BEFORE**
```html
          <div class="search-bar-integrated">
            <div class="search-box-wrap">
              <input id="searchQuery" ...>
              <button id="pasteBtn" ...>...</button>
              <button id="clearSearchBtn" ...>...</button>
              <div id="suggestBox" class="suggest-box hidden"></div>
            </div>
            <div class="search-bar-actions">
              ...
            </div>
          </div>
```
**AFTER** (move `#suggestBox` to be a direct child of `.search-bar-integrated`, after `.search-bar-actions`)
```html
          <div class="search-bar-integrated">
            <div class="search-box-wrap">
              <input id="searchQuery" ...>
              <button id="pasteBtn" ...>...</button>
              <button id="clearSearchBtn" ...>...</button>
            </div>
            <div class="search-bar-actions">
              ...
            </div>
            <div id="suggestBox" class="suggest-box hidden"></div>
          </div>
```
> `.search-bar-integrated` is already `position:relative; overflow:visible !important` — so an absolute child anchors to the full bar and won't clip.

### 2) `static/js/src/5-search.js` — delete the positioning fork, cap to 5
**BEFORE** (in `getSuggestions()`, the entire mobile/desktop block ~lines 323-338)
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
**AFTER** — delete the whole block. Positioning is now 100% CSS (identical both viewports). No `getBoundingClientRect`, no `position:fixed`, no scroll/resize listeners needed.

**BEFORE** (render loop ~line 339)
```js
        for (const item of rows) {
```
**AFTER** (cap to 5)
```js
        for (const item of rows.slice(0, 5)) {
```
> Optional belt-and-suspenders: also `const rows = (Array.isArray(data) ? data : []).slice(0, 5);` at the top of the handler. Pick ONE place to cap, not both, to keep it clear.

> Leave intact: 3+ char gate (`q.length < 3`), 350ms debounce (`setTimeout(..., 350)`), click-to-fill handler, and all dismiss handlers in `6-main.js`. `isMobileSearchUi()` in `3b-series.js` stays UNMODIFIED.

### 3) `static/css/base.css` — unified anchor + fit-5 height
**BEFORE** (`.suggest-box`, ~line 218)
```css
    .suggest-box {
      position: absolute;
      left: 0;
      right: 0;
      top: calc(100% + 8px);
      z-index: 25;
      background: rgba(11, 14, 20, 0.85);
      ...
      max-height: 288px; /* Max 6 visible rows scroll if more */
      overflow-y: auto;
    }
```
**AFTER**
```css
    .suggest-box {
      position: absolute;
      left: 0;
      right: 0;
      top: calc(100% + 8px);
      z-index: 9900;
      background: rgba(11, 14, 20, 0.97);
      ...
      max-height: 312px; /* 5 rows × 60px + borders; grows to fit up to 5 */
      overflow-y: auto;
    }
```
> Anchored to full `.search-bar-integrated` (because of the DOM move in step 1). `z-index:9900` keeps it above results/nav. Near-opaque bg kills bleed-through. With a hard 5-cap, scrolling is essentially never needed but `overflow-y:auto` stays as a safety net.

### 4) `static/css/responsive.css` — remove the now-dead mobile override
**BEFORE** (~lines 773-775)
```css
  #searchView .suggest-box {
    max-height: 360px !important;
  }
```
**AFTER** — delete this rule (unified `max-height` from base.css now governs both viewports).
Keep the `overflow: visible !important` ancestor rules (433-435, 766-772) — they prevent clipping and are harmless.

---

## OPTIONAL (only if you want zero console noise) — backend contract
`search_service.py` `imdb_suggestions` returns `{title, year, poster, id}`. Frontend warns about missing
`type`/`rating`/`poster_url`. NOT required for the fix. If desired, the frontend warning block can be removed,
OR the backend can add `"type": item.get("q", "")` / `"poster_url"` aliasing. **Do not** change behavior here
unless explicitly asked — out of scope for this audit fix.

---

## STAGE 2 — Audit checklist (all ✅)
- [ ] Single code path: NO `isMobileSearchUi()` branch left in `getSuggestions()`.
- [ ] 3+ char gate, 350ms debounce, click-to-fill, dismiss handlers all intact.
- [ ] Hard cap of 5 rows (verify with a query returning >5).
- [ ] Box spans full `.search-bar-integrated` width, directly below, on BOTH desktop and mobile.
- [ ] Box stays attached while scrolling (it's now an absolute child — moves with the bar).
- [ ] No new imports; no `except Exception:`; no Python config change (unless optional backend opted in, with `current_app.config.get`).
- [ ] `isMobileSearchUi()` (3b-series.js) UNMODIFIED.

## STAGE 3 — Build & lock
- [ ] Resolve the pre-existing dirty tree (finish/keep/revert the un-bundled auto-add removal) FIRST.
- [ ] Run `python build_js.py` in `static/js/` → regenerate `app.js`.
- [ ] VERIFY rebuild: concat(wrap_open + sorted src/*.js + wrap_close) must byte-equal `app.js`.
- [ ] Confirm live `app.js` contains `rows.slice(0, 5)` and NO `position = "fixed"` in the suggest path.
- [ ] Ledger entry in `ai/STATE.md`: "Unified suggestion dropdown: moved #suggestBox to .search-bar-integrated,
      removed mobile fixed-position fork, capped to 5 results, fit-5 height, near-opaque panel.
      Files: templates/index.html, src/5-search.js, css/base.css, css/responsive.css, app.js."
- [ ] Final BEFORE/AFTER diff limited to those files; deploy.
