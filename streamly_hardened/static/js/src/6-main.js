  async function setTab(name) {
    if (name === "cloud" && !isAuthenticated) {
      // Trigger a silent re-login attempt first. If that works, proceed.
      const restored = await attemptSilentRelogin();
      if (!restored) {
        showLogin();
        return;
      }
    }
    // Automatically dismiss login popup if we switch back to search
    if (name === "search") {
      $("loginScreen").classList.add("hidden");
    }
    // Update the URL hash so refresh restores the correct tab
    window.history.replaceState(null, null, `#${name}`);

    $("cloudView").classList.toggle("hidden", name !== "cloud");
    $("searchView").classList.toggle("hidden", name !== "search");
    $("cloudTab").classList.toggle("active", name === "cloud");
    $("searchTab").classList.toggle("active", name === "search");

    // Auto-load root folder when switching to cloud view
    if (name === "cloud" && isAuthenticated) {
      await loadFolder(currentFolder || 0);
    }
  }

  /* Event wiring */
  $("loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const btn = $("loginBtn");
    btn.disabled = true;
    status($("loginStatus"), "Connecting to Seedr...", "");
    try {
      const data = await postJson("/api/login", { email: $("email").value, password: $("password").value });
      $("password").value = "";
      showApp(data.username || "Logged in");
      await loadFolder(0);
    } catch (err) {
      status($("loginStatus"), err.message || "Login failed", "error");
    } finally {
      btn.disabled = false;
    }
  });

  $("cloudTab").addEventListener("click", () => setTab("cloud"));
  $("searchTab").addEventListener("click", () => setTab("search"));
  $("refreshBtn").addEventListener("click", () => loadFolder(currentFolder));
  $("upBtn").addEventListener("click", () => { if (currentFolder !== 0) loadFolder(parentFolder || 0); });
  $("openBtn").addEventListener("click", () => openItem());
  $("downloadBtn").addEventListener("click", downloadSelected);
  $("deleteBtn").addEventListener("click", deleteSelected);
  $("selectAllCheck").addEventListener("change", (e) => {
    if (e.target.checked) {
      for (const it of items) selectedKeys.add(it.key);
    } else {
      selectedKeys.clear();
    }
    updateSelection();
  });
  $("clearSelBtn").addEventListener("click", () => {
    selectedKeys.clear();
    lastClickedKey = null;
    updateSelection();
  });
  $("searchBtn").addEventListener("click", () => search(false, 1));
  if ($("modeNormal")) $("modeNormal").addEventListener("click", () => setSeriesMode(false));
  if ($("modeSeries")) $("modeSeries").addEventListener("click", () => setSeriesMode(true));

  // Multi-select dropdowns (Quality / Encoders)
  function toggleDd(ddId) {
    const dd = $(ddId);
    if (!dd) return;
    const panel = dd.querySelector(".ms-dd-panel");
    const isOpen = !panel.classList.contains("hidden");
    // close all panels first
    document.querySelectorAll(".ms-dd-panel").forEach((p) => p.classList.add("hidden"));
    if (!isOpen) panel.classList.remove("hidden");
  }
  if ($("qualityDdBtn")) $("qualityDdBtn").addEventListener("click", (e) => { e.stopPropagation(); toggleDd("qualityDd"); });
  if ($("encoderDdBtn")) $("encoderDdBtn").addEventListener("click", (e) => { e.stopPropagation(); toggleDd("encoderDd"); });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".ms-dd")) document.querySelectorAll(".ms-dd-panel").forEach((p) => p.classList.add("hidden"));
  });
  document.querySelectorAll(".qualityOpt, .encoderOpt").forEach((el) =>
    el.addEventListener("change", () => {
      if (typeof updateDropdownLabels === "function") updateDropdownLabels();
    })
  );

  // ----- Mobile cloud wiring -----
  if ($("cmUpBtn")) $("cmUpBtn").addEventListener("click", () => { if (currentFolder !== 0) loadFolder(parentFolder || 0); });
  if ($("cmRefreshBtn")) $("cmRefreshBtn").addEventListener("click", () => loadFolder(currentFolder));
  if ($("cmSelectAll")) $("cmSelectAll").addEventListener("change", (e) => {
    if (e.target.checked) { for (const it of items) selectedKeys.add(it.key); }
    else { selectedKeys.clear(); }
    updateSelection();
  });
  if ($("cmBulkDownload")) $("cmBulkDownload").addEventListener("click", downloadSelected);
  if ($("cmBulkDelete")) $("cmBulkDelete").addEventListener("click", deleteSelected);
  if ($("cmBulkClear")) $("cmBulkClear").addEventListener("click", () => { selectedKeys.clear(); lastClickedKey = null; updateSelection(); });
  document.querySelectorAll("#cloudCtxMenu .cm-ctx-item").forEach((b) => b.addEventListener("click", () => ctxAction(b.dataset.act)));
  document.addEventListener("click", (e) => { if (!e.target.closest("#cloudCtxMenu") && !e.target.closest(".cm-kebab")) closeCtxMenu(); });

  if ($("pasteBtn")) {
    $("pasteBtn").addEventListener("click", async () => {
      try {
        const text = await navigator.clipboard.readText();
        $("searchQuery").value = text;
        $("searchQuery").focus();
        
        // Auto-add if it's a magnet link
        if (/^magnet:\?xt=urn:btih:/i.test(text)) {
           search(false, 1);
        }
      } catch (err) {
        toast("Clipboard access denied");
      }
    });
  }

  // Allow dismissing login overlay (continue as guest)
  $("loginCloseBtn").addEventListener("click", () => {
    $("loginScreen").classList.add("hidden");
    $("appScreen").classList.remove("hidden");
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("loginScreen").classList.contains("hidden")) {
      $("loginScreen").classList.add("hidden");
      $("appScreen").classList.remove("hidden");
    }
  });

    $("clearSearchBtn").addEventListener("click", () => {
      // Clear only the search text (and hide stale suggestions); keep results on screen
      clearTimeout(suggestTimer);
      $("searchQuery").value = "";
      $("suggestBox").classList.add("hidden");
      $("suggestBox").textContent = "";
      $("searchQuery").focus();
      // restore the Search button in case an "Add Link" state was showing
      $("searchBtn").classList.remove("hidden");
      $("addMagnetBtn").classList.add("hidden");
    });
  // Automatically toggle Search vs Add button based on input content
  $("searchQuery").addEventListener("input", (e) => {
    getSuggestions();
    const q = e.target.value.trim();
    if (/^magnet:\?xt=urn:btih:/i.test(q)) {
      $("searchBtn").classList.add("hidden");
      $("addMagnetBtn").classList.remove("hidden");
    } else {
      $("searchBtn").classList.remove("hidden");
      $("addMagnetBtn").classList.add("hidden");
    }
  });

  $("addMagnetBtn").addEventListener("click", () => search(false, 1));
  $("searchQuery").addEventListener("keydown", (e) => { if (e.key === "Enter") search(false, 1); });
  $("category").addEventListener("change", () => { if ($("searchQuery").value.trim()) search(false, 1); });
  document.querySelectorAll(".sortable[data-sort]").forEach((el) => el.addEventListener("click", () => cycleSort(el.dataset.sort)));
  document.addEventListener("click", (e) => { if (!e.target.closest(".search-box-wrap")) $("suggestBox").classList.add("hidden"); });
  syncSortControls();
  $("closeVideoBtn").addEventListener("click", () => {
    const video = $("videoPlayer");
    video.pause();
    video.removeAttribute("src");
    video.load();
    $("videoOverlay").classList.add("hidden");
  });

  // Initialization Sequence
  async function init() {
    let initialTab = window.location.hash.replace("#", "") || "search";
    if (initialTab !== "cloud" && initialTab !== "search") initialTab = "search";
    
    // Optimistically show header and search tab immediately
    showApp(null); 
    if (initialTab === "search") {
      setTab("search");
    }

    try {
      let data;
      try {
        data = await parseResponse(await fetch("/api/status", { credentials: "same-origin" }));
      } catch (_) {
        // Status check failed (likely 401) — try silent re-login before giving up
        const restored = await attemptSilentRelogin();
        if (restored) {
          data = { authenticated: true, username: $("userPill").textContent };
        } else {
          throw new Error("not authenticated");
        }
      }
      if (data.authenticated) {
        showApp(data.username || "Logged in");
        if (initialTab === "cloud") {
          setTab("cloud");
          await loadFolder(0);
        }
      }
    } catch (_) {
      // Not authenticated. Force them to search tab (Guest mode).
      setTab("search");
    }
  }

  init();
