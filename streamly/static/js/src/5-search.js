
  window.suppressSuggestions = false;
  window.searchAbort = null;

  window.isMagnetLink = function(value) {
    const val = String(value || "").trim();
    if (/^\s*magnet:\?xt=urn:btih:/i.test(val)) return true;
    const lower = val.toLowerCase();
    if (lower.endsWith(".torrent") && (lower.startsWith("http://") || lower.startsWith("https://"))) return true;
    return false;
  }

  window.magnetInfoHash = function(value) {
    const text = String(value || "");
    const m = text.match(/xt=urn:btih:([^&]+)/i);
    if (!m) return "";
    try { return decodeURIComponent(m[1]).trim().toLowerCase(); }
    catch (_) { return String(m[1] || "").trim().toLowerCase(); }
  }

  window.setSearchAction = function(action) {
    const isAdd = action === "add";
    const searchBtn = $("searchBtn");
    const addBtn = $("addMagnetBtn");
    if (searchBtn && addBtn) {
      searchBtn.classList.toggle("hidden", isAdd);
      addBtn.classList.toggle("hidden", !isAdd);
    }
  }

  window.setMagnetUiState = function(value) {
    const isMagnet = isMagnetLink(value);
    setSearchAction(isMagnet ? "add" : "search");
    return isMagnet;
  }

  window.maybeAutoAddMagnet = function(value, source = "input") {
    const magnet = String(value || "").trim();
    if (!setMagnetUiState(magnet)) return false;

    if (source === "clipboard" || source === "url") {
      $("searchQuery").value = magnet;
      setMagnetUiState(magnet);
      toast("Magnet detected! Tap [+] to add to Seedr.");
      return true;
    }

    if (lastAutoAddedMagnet === magnet) return true;
    clearTimeout(autoAddTimer);
    autoAddTimer = setTimeout(() => {
      if ($("searchQuery").value.trim() !== magnet) return;
      if (lastAutoAddedMagnet === magnet) return;
      lastAutoAddedMagnet = magnet;
      search(false, 1);
    }, source === "input" ? 250 : 0);
    return true;
  }

  // Deliberate, user-triggered clipboard paste (manual paste button, double-tap on the
  // Search tab). This is the ONLY place in the app that reads the clipboard, and it only
  // runs in direct response to an explicit user gesture -- never automatically on page
  // load, tab switch, or window focus. (Those automatic/passive checks were removed:
  // silently reading the clipboard on ordinary navigation is intrusive and was flagged
  // as such -- a deliberate click/double-tap is the same trust level as native Ctrl+V.)
  //
  // Behavior: pastes whatever text is on the clipboard into the search box. If it's a
  // magnet link, it's auto-added to Seedr (reusing the same detection/add path as a
  // normal manual paste); otherwise the text is just placed in the box for the user to
  // search manually.
  window.pasteClipboardIntoSearch = async function() {
    if (!navigator.clipboard || !navigator.clipboard.readText) {
      toast("Clipboard access is not available in this browser");
      return false;
    }
    try {
      const text = (await navigator.clipboard.readText()).trim();
      if (!text) return false;
      $("searchQuery").value = text;
      $("searchQuery").focus();
      if (typeof maybeAutoAddMagnet === "function" && maybeAutoAddMagnet(text, "paste")) return true;
      if (typeof setMagnetUiState === "function") setMagnetUiState(text);
      return true;
    } catch (err) {
      toast("Clipboard access denied");
      return false;
    }
  }

  window.extractMagnetFromUrl = function() {
    const candidates = [];
    const url = new URL(window.location.href);
    candidates.push(url.searchParams.get("magnet"));
    const rawHash = window.location.hash ? window.location.hash.slice(1) : "";
    if (rawHash) {
      candidates.push(rawHash);
      try {
        const hp = new URLSearchParams(rawHash.startsWith("?") ? rawHash.slice(1) : rawHash);
        candidates.push(hp.get("magnet"));
      } catch (_) {}
    }
    for (const c of candidates) {
      if (!c) continue;
      let value = String(c).trim();
      for (let i = 0; i < 2; i++) {
        try { value = decodeURIComponent(value); } catch (_) { break; }
      }
      if (isMagnetLink(value)) return value;
    }
    return "";
  }

  window.cleanMagnetUrl = function() {
    const url = new URL(window.location.href);
    url.searchParams.delete("magnet");
    const keepHash = window.location.hash && !window.location.hash.toLowerCase().includes("magnet") ? window.location.hash : "";
    window.history.replaceState(null, null, url.pathname + url.search + keepHash);
  }

  window.ingestUrlMagnet = function() {
    // Do not auto-detect/overwrite if the user already has text in the search box.
    if ($("searchQuery") && $("searchQuery").value.trim()) return false;
    const magnet = extractMagnetFromUrl();
    if (!magnet) return false;
    $("searchQuery").value = magnet;
    setMagnetUiState(magnet);
    cleanMagnetUrl();
    maybeAutoAddMagnet(magnet, "url");
    return true;
  }

  window.providerStatusText = function(data) {
    if (!data || !data.provider) return "";
    const provider = data.provider;
    const attempts = Array.isArray(data.provider_attempts) ? data.provider_attempts : [];
    const before = [];
    for (const a of attempts) {
      if (!a || !a.provider) continue;
      if (a.provider === provider && Number(a.filtered || 0) > 0) break;
      if (a.provider !== provider && !before.some(x => x.provider === a.provider)) {
        before.push({ provider: a.provider, raw: Number(a.raw || 0), filtered: Number(a.filtered || 0) });
      }
    }
    let label = "via " + provider;
    if (data.provider_fallback === "unfiltered") label += " · unfiltered fallback";
    if (data.provider_fallback === "less_relevant") label += " · showing less relevant matches";
    if (data.provider_fallback === "other") label += " · showing other matches";
    if (!before.length) return label;
    const details = before.map(a => a.provider + (a.raw > 0 && a.filtered === 0 ? " filtered out" : " no results")).join(", ");
    return label + " after " + details;
  }

  window.search = async function(keepPage, page) {
    const q = $("searchQuery").value.trim();
    if (!q) return updateStatus($("searchStatus"), "Enter a search query", "error");

    suppressSuggestions = true;
    if (searchAbort) searchAbort.abort();
    searchAbort = new AbortController();
    const _signal = searchAbort.signal;
    clearTimeout(suggestTimer);
    $("suggestBox").classList.add("hidden");
    $("suggestBox").textContent = "";

    if (isMagnetLink(q)) {
      let magnetName = "Unknown Magnet";
      const dnMatch = q.match(/[?&]dn=([^&]+)/);
      if (dnMatch) {
        try { magnetName = decodeURIComponent(dnMatch[1].replace(/\+/g, " ")); } catch (_) {}
      }
      saveToHistory(q, magnetName);
      updateStatus($("searchStatus"), "Adding magnet...", "");
      try {
        const res = await postJson("/api/add", { magnet: q, provider: window.driveProvider || "auto" });
        if (res && res.queued) {
          updateStatus($("searchStatus"), "✓ Added to Queue: " + magnetName, "ok");
          toast("Added to local queue: " + magnetName);
        } else if (res && res.provider === "offcloud") {
          updateStatus($("searchStatus"), "✓ Added to Offcloud: " + magnetName, "ok");
          toast("Added to Offcloud: " + magnetName);
        } else {
          updateStatus($("searchStatus"), "✓ Added: " + magnetName, "ok");
          toast("Added to Seedr: " + magnetName);
        }
        if (isAuthenticated && $("cloudView") && !$("cloudView").classList.contains("hidden")) loadFolder(currentFolder || 0, { silent: true });
        else if (typeof refreshStorageSnapshot === "function") refreshStorageSnapshot(true);
        $("searchQuery").value = "";
        setMagnetUiState("");
      } catch (err) {
        updateStatus($("searchStatus"), err.message || "Failed to add magnet", "error");
      }
      return;
    }

    if (!keepPage) currentPage = page || 1;
    const providerOrderText = (typeof seriesMode !== "undefined" && seriesMode)
      ? "apibay → bitsearch → torrents-csv"
      : "bitsearch → apibay → torrents-csv";
    updateStatus($("searchStatus"), "Searching providers: " + providerOrderText + "...", "");

    const resultsContainer = $("seriesResults");
    if (resultsContainer) {
      resultsContainer.classList.remove("hidden");
      resultsContainer.textContent = "";
      resultsContainer.appendChild(window.seriesHeaderRow());
      
      const frag = document.createDocumentFragment();
      for (let i = 0; i < 5; i++) {
        const row = document.createElement("div");
        row.className = "episode-row skeleton";
        row.innerHTML = `
          <span class="skeleton-bar name skeleton-title"></span>
          <span class="skeleton-bar encoder skeleton-encoder"></span>
          <span class="skeleton-bar se skeleton-se"></span>
          <span class="skeleton-bar time skeleton-time"></span>
          <span class="skeleton-bar size skeleton-size"></span>
          <span class="skeleton-bar add skeleton-add"></span>
        `;
        frag.appendChild(row);
      }
      resultsContainer.appendChild(frag);
    }
    try {
      const params = new URLSearchParams();
      params.set("q", q);
      params.set("sort", currentSort);
      params.set("order", currentOrder);
      params.set("page", String(currentPage));
      params.set("dedup", "1"); // dedup is always on (checkbox removed)
      // Quality + Encoder are FILTERS in both modes:
      //  - Normal: quality picks which sections show; encoder filters release groups.
      //  - Series: used per-query as before.
      params.set("quality", getSelectedQualities().join(","));
      params.set("encoders", getSelectedEncoders().join(","));
      if (typeof seriesMode !== "undefined" && seriesMode) {
        params.set("mode", "series");
      }
      const data = await parseResponse(await fetch("/api/search?" + params.toString(), { credentials: "same-origin", signal: _signal }));

      if (data && data.mode === "series") {
        $("seriesResults").classList.remove("hidden");
        renderSeriesGrouped(data);
        const packs = (data.packs || []).length;
        const eps = (data.encoders || []).reduce((a, e) => a + (e.episode_count || 0), 0);
        const less = (data.less_relevant || []).length;
        const other = (data.other || []).length;
        const extra = (less || other) ? " + " + (less + other) + " other" : "";
        const providerText = providerStatusText(data);
        updateStatus($("searchStatus"), "Found " + packs + " pack(s) + " + eps + " episode(s)" + extra + " \u00b7 " + (data.requests_used || 0) + " request(s)" + (providerText ? " \u00b7 " + providerText : ""), "ok");
        return;
      }

      // Normal mode = quality-grouped sections
      const groups = Array.isArray(data.quality_groups) ? data.quality_groups : [];
      $("seriesResults").classList.remove("hidden");
      renderNormalGrouped(groups);
      // "less_relevant" is a separate section, NOT a quality group — exclude it from the group count.
      const primaryGroups = groups.filter(g => g.quality !== "less_relevant");
      const total = groups.reduce((a, g) => a + (g.count || 0), 0);
      const groupCount = primaryGroups.length;
      const providerText = providerStatusText(data);
      updateStatus($("searchStatus"), "Found " + total + " results" + (groupCount ? " across " + groupCount + " quality group" + (groupCount === 1 ? "" : "s") : "") + (providerText ? " · " + providerText : ""), "ok");
    } catch (err) {
      if (err && err.name === "AbortError") return; // superseded by a newer search
      updateStatus($("searchStatus"), err.message || "Search failed", "error");
    }
  }

  window.makeAddButton = function(result) {
    const add = document.createElement("button");
    add.type = "button";
    add.className = "add-btn";
    
    function setButtonState(state) {
      add.dataset.state = state;
      add.textContent = "";
      if (state === "idle") {
        add.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-plus"><path d="M5 12h14M12 5v14"/></svg>`;
      } else if (state === "adding") {
        add.innerHTML = `<svg class="btn-spinner" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><circle cx="12" cy="12" r="10" stroke="rgba(255,255,255,0.2)"/><path d="M12 2a10 10 0 0 1 10 10" class="spin-path"/></svg>`;
      } else if (state === "done") {
        add.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-check"><polyline points="20 6 9 17 4 12"/></svg>`;
      }
    }
    
    setButtonState("idle");
    
    add.addEventListener("click", () => {
      add.disabled = true;
      setButtonState("adding");
      setTimeout(async () => {
        saveToHistory(result.magnet, result.name, result.size);
        try {
          const res = await postJson("/api/add", { magnet: result.magnet, size: result.size_bytes || 0, provider: window.driveProvider || "auto" });
          if (res && res.queued) {
            toast("Added to queue: " + (result.name || "torrent"));
          } else if (res && res.provider === "offcloud") {
            toast("Added to Offcloud: " + (result.name || "torrent"));
          } else {
            toast("Added to Seedr: " + (result.name || "torrent"));
          }
          if (isAuthenticated && $("cloudView") && !$("cloudView").classList.contains("hidden")) loadFolder(currentFolder || 0, { silent: true });
          else if (typeof refreshStorageSnapshot === "function") refreshStorageSnapshot(true);
          setButtonState("done");
        } catch (err) {
          toast(err.message || "Failed to add");
          setButtonState("idle");
          add.disabled = false;
        }
      }, 0);
    });
    return add;
  }

  window.fieldsWarned = false;

  window.getSuggestions = async function() {
    const q = $("searchQuery").value.trim();
    const box = $("suggestBox");
    clearTimeout(suggestTimer);
    
    if (q.length < 3 || isMagnetLink(q)) {
      box.classList.add("hidden");
      box.textContent = "";
      return;
    }
    suppressSuggestions = false;
    suggestTimer = setTimeout(async () => {
      try {
        if ($("searchQuery").value.trim() !== q) return;
        const data = await parseResponse(await fetch("/api/suggest?q=" + encodeURIComponent(q), { credentials: "same-origin" }));
        box.textContent = "";
        const rows = Array.isArray(data) ? data : [];
        if (!rows.length) {
          box.classList.add("hidden");
          return;
        }

        if (!fieldsWarned && rows.length > 0) {
          const missing = [];
          const testItem = rows[0] || {};
          if (testItem.title === undefined) missing.push("title");
          if (testItem.year === undefined) missing.push("year");
          if (testItem.type === undefined) missing.push("type");
          if (testItem.rating === undefined) missing.push("rating");
          if (testItem.poster_url === undefined && testItem.poster === undefined) missing.push("poster_url");
          if (missing.length > 0) {
            console.warn("IMDb suggestions missing expected backend fields: " + missing.join(", "));
            fieldsWarned = true;
          }
        }

        for (const item of rows.slice(0, 5)) {
          const row = document.createElement("div");
          row.className = "suggest-item";

          const posterContainer = document.createElement("div");
          posterContainer.className = "suggest-poster-container";

          const placeholder = document.createElement("div");
          placeholder.className = "suggest-poster-placeholder";
          placeholder.textContent = (item.title || "U").charAt(0).toUpperCase();

          const posterUrl = item.poster_url || item.poster;
          if (posterUrl) {
            const img = document.createElement("img");
            img.className = "suggest-poster-img";
            img.src = posterUrl;
            img.alt = item.title || "";
            img.onerror = () => {
              img.style.display = "none";
              placeholder.style.display = "flex";
            };
            placeholder.style.display = "none";
            posterContainer.append(img, placeholder);
          } else {
            posterContainer.appendChild(placeholder);
          }

          const content = document.createElement("div");
          content.className = "suggest-content";

          const title = document.createElement("div");
          title.className = "suggest-title";
          title.textContent = item.title || "Untitled";

          const meta = document.createElement("div");
          meta.className = "suggest-meta";

          const metaParts = [];
          if (item.year && item.year !== "N/A") {
            metaParts.push(item.year);
          }
          const isTv = String(item.year || "").includes("-") || String(item.year || "").includes("–");
          const typeName = item.type || (isTv ? "TV" : "Movie");
          if (typeName) {
            metaParts.push(typeName);
          }
          if (item.rating) {
            metaParts.push(`⭐ ${item.rating}`);
          }
          meta.textContent = metaParts.join(" \u2009·\u2009 ");

          content.append(title, meta);
          row.append(posterContainer, content);

          row.addEventListener("mousedown", (e) => {
            e.preventDefault();
          });
          row.addEventListener("click", () => {
            $("searchQuery").value = item.title || "";
            box.classList.add("hidden");
          });
          box.appendChild(row);
        }
        if (suppressSuggestions) return;
        box.classList.remove("hidden");
      } catch (_) {
        box.classList.add("hidden");
      }
    }, 350);
  }

