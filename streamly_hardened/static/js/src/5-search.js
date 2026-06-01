
  function isMagnetLink(value) {
    return /^magnet:\?xt=urn:btih:/i.test(String(value || "").trim());
  }

  function magnetInfoHash(value) {
    const text = String(value || "");
    const m = text.match(/xt=urn:btih:([^&]+)/i);
    if (!m) return "";
    try { return decodeURIComponent(m[1]).trim().toLowerCase(); }
    catch (_) { return String(m[1] || "").trim().toLowerCase(); }
  }

  function autoAddedStorageKey(magnet) {
    const hash = magnetInfoHash(magnet);
    return hash ? "streamly:autoAddedMagnet:" + hash : "";
  }

  function wasAutoAddedRecently(magnet) {
    const key = autoAddedStorageKey(magnet);
    if (!key) return false;
    try {
      const raw = localStorage.getItem(key);
      const ts = Number(raw || 0);
      if (!ts) return false;
      if (Date.now() - ts > AUTO_ADD_MAGNET_TTL_MS) {
        localStorage.removeItem(key);
        return false;
      }
      return true;
    } catch (_) {
      return false;
    }
  }

  function rememberAutoAddedMagnet(magnet) {
    const key = autoAddedStorageKey(magnet);
    if (!key) return;
    try { localStorage.setItem(key, String(Date.now())); } catch (_) {}
  }

  function showRecentMagnetSkip() {
    status($("searchStatus"), "Magnet already auto-added recently. Tap Add to force add again.", "ok");
  }

  function setMagnetUiState(value) {
    const isMagnet = isMagnetLink(value);
    if ($("searchBtn") && $("addMagnetBtn")) {
      $("searchBtn").classList.toggle("hidden", isMagnet);
      $("addMagnetBtn").classList.toggle("hidden", !isMagnet);
      $("addMagnetBtn").textContent = "➕";
      $("addMagnetBtn").title = "Add magnet";
    }
    return isMagnet;
  }

  function maybeAutoAddMagnet(value, source = "input") {
    const magnet = String(value || "").trim();
    if (!setMagnetUiState(magnet)) return false;
    if (lastAutoAddedMagnet === magnet) return true;
    if (wasAutoAddedRecently(magnet)) {
      showRecentMagnetSkip();
      return true;
    }
    clearTimeout(autoAddTimer);
    autoAddTimer = setTimeout(() => {
      if ($("searchQuery").value.trim() !== magnet) return;
      if (lastAutoAddedMagnet === magnet) return;
      if (wasAutoAddedRecently(magnet)) {
        showRecentMagnetSkip();
        return;
      }
      lastAutoAddedMagnet = magnet;
      rememberAutoAddedMagnet(magnet);
      search(false, 1);
    }, source === "input" ? 250 : 0);
    return true;
  }

  async function ingestClipboardMagnet(autoAdd = true) {
    if (!navigator.clipboard || !navigator.clipboard.readText) return false;
    try {
      const text = (await navigator.clipboard.readText()).trim();
      if (!isMagnetLink(text)) return false;
      $("searchQuery").value = text;
      setMagnetUiState(text);
      if (autoAdd) maybeAutoAddMagnet(text, "clipboard");
      return true;
    } catch (_) {
      return false;
    }
  }

  function scheduleClipboardMagnetCheck(reason = "event") {
    if (!$("searchView") || $("searchView").classList.contains("hidden")) return;
    const now = Date.now();
    const wait = Math.max(0, CLIPBOARD_MAGNET_CHECK_DEBOUNCE_MS - (now - lastClipboardMagnetCheckAt));
    clearTimeout(clipboardMagnetCheckTimer);
    clipboardMagnetCheckTimer = setTimeout(async () => {
      lastClipboardMagnetCheckAt = Date.now();
      await ingestClipboardMagnet(true);
    }, wait);
  }

  function extractMagnetFromUrl() {
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

  function cleanMagnetUrl() {
    const url = new URL(window.location.href);
    url.searchParams.delete("magnet");
    const keepHash = window.location.hash && !window.location.hash.toLowerCase().includes("magnet") ? window.location.hash : "";
    window.history.replaceState(null, null, url.pathname + url.search + keepHash);
  }

  function ingestUrlMagnet() {
    const magnet = extractMagnetFromUrl();
    if (!magnet) return false;
    $("searchQuery").value = magnet;
    setMagnetUiState(magnet);
    cleanMagnetUrl();
    maybeAutoAddMagnet(magnet, "url");
    return true;
  }

  function providerStatusText(data) {
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
    const label = "via " + provider;
    if (!before.length) return label;
    const details = before.map(a => a.provider + (a.raw > 0 && a.filtered === 0 ? " filtered out" : " no results")).join(", ");
    return label + " after " + details;
  }

  async function search(keepPage, page) {
    const q = $("searchQuery").value.trim();
    if (!q) return status($("searchStatus"), "Enter a search query", "error");

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
      status($("searchStatus"), "Adding magnet to Seedr...", "");
      try {
        await postJson("/api/add", { magnet: q });
        rememberAutoAddedMagnet(q);
        status($("searchStatus"), "\u2713 Added: " + magnetName, "ok");
        if (isAuthenticated && $("cloudView") && !$("cloudView").classList.contains("hidden")) loadFolder(currentFolder || 0, { silent: true });
        else if (typeof refreshStorageSnapshot === "function") refreshStorageSnapshot(true);
        $("searchQuery").value = "";
        setMagnetUiState("");
      } catch (err) {
        status($("searchStatus"), err.message || "Failed to add magnet", "error");
      }
      return;
    }

    if (!keepPage) currentPage = page || 1;
    status($("searchStatus"), "Searching providers: apibay → bitsearch → torrents-csv...", "");
    if ($("resultCount")) $("resultCount").textContent = "";
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
      const data = await parseResponse(await fetch("/api/search?" + params.toString(), { credentials: "same-origin" }));

      if (data && data.mode === "series") {
        $("seriesResults").classList.remove("hidden");
        renderSeriesGrouped(data);
        const packs = (data.packs || []).length;
        const eps = (data.encoders || []).reduce((a, e) => a + (e.episode_count || 0), 0);
        if ($("resultCount")) $("resultCount").textContent = "";
        const providerText = providerStatusText(data);
        status($("searchStatus"), "Found " + packs + " pack(s) + " + eps + " episode(s) \u00b7 " + (data.requests_used || 0) + " request(s)" + (providerText ? " \u00b7 " + providerText : ""), "ok");
        return;
      }

      // Normal mode = quality-grouped sections
      const groups = Array.isArray(data.quality_groups) ? data.quality_groups : [];
      $("seriesResults").classList.remove("hidden");
      renderNormalGrouped(groups);
      const total = groups.reduce((a, g) => a + (g.count || 0), 0);
      if ($("resultCount")) $("resultCount").textContent = "";
      const providerText = providerStatusText(data);
      status($("searchStatus"), "Found " + total + " results" + (groups.length ? " across " + groups.length + " quality group" + (groups.length === 1 ? "" : "s") : "") + (providerText ? " · " + providerText : ""), "ok");
    } catch (err) {
      if ($("resultCount")) $("resultCount").textContent = "";
      status($("searchStatus"), err.message || "Search failed", "error");
    }
  }

  function makeAddButton(result) {
    const add = document.createElement("button");
    add.type = "button";
    add.className = "add-btn";
    add.dataset.state = "idle";
    add.textContent = "+";
    add.addEventListener("click", async () => {
      saveToHistory(result.magnet, result.name);
      add.disabled = true;
      add.dataset.state = "adding";
      add.textContent = "Adding...";
      try {
        await postJson("/api/add", { magnet: result.magnet, size: result.size_bytes || 0 });
        toast("Added to Seedr: " + (result.name || "torrent"));
        if (isAuthenticated && $("cloudView") && !$("cloudView").classList.contains("hidden")) loadFolder(currentFolder || 0, { silent: true });
        else if (typeof refreshStorageSnapshot === "function") refreshStorageSnapshot(true);
        add.dataset.state = "done";
        add.textContent = "\u2713";
      } catch (err) {
        toast(err.message || "Failed to add");
        add.dataset.state = "idle";
        add.textContent = "+";
        add.disabled = false;
      }
    });
    return add;
  }
