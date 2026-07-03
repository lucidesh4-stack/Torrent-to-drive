  window.updateBottomNavHighlight = function(index) {
    const highlight = $("bottomNavHighlight");
    if (!highlight) return;
    highlight.style.transform = `translateX(${index * 100}%)`;
    
    // Update active class on tab items
    const tabs = ["cloudTab", "searchTab", "historyBtn", "telegramTabBtn"];
    tabs.forEach((id, idx) => {
      const btn = $(id);
      if (btn) btn.classList.toggle("active", idx === index);
    });
  };

  window.restoreActiveMainTabHighlight = function() {
    const isCloud = !$("cloudView").classList.contains("hidden");
    if (isCloud) {
      window.updateBottomNavHighlight(0);
    } else {
      window.updateBottomNavHighlight(1);
    }
  };

  window.setTab = async function(name) {
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

    if (name === "cloud") window.updateBottomNavHighlight(0);
    if (name === "search") window.updateBottomNavHighlight(1);

    // Auto-load root folder when switching to cloud view; stop transfer polling off-cloud.
    if (name === "cloud" && isAuthenticated) {
      await loadFolder(currentFolder || 0);
    } else if (typeof syncCloudAutoRefresh === "function") {
      syncCloudAutoRefresh();
    }
  }

  /* Event wiring */
  $("loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const btn = $("loginBtn");
    btn.disabled = true;
    updateStatus($("loginStatus"), "Connecting to Seedr...", "");
    try {
      const data = await postJson("/api/login", { email: $("email").value, password: $("password").value });
      $("password").value = "";
      showApp(data.username || "Logged in");
      await loadFolder(0);
    } catch (err) {
      updateStatus($("loginStatus"), err.message || "Login failed", "error");
    } finally {
      btn.disabled = false;
    }
  });

  $("cloudTab").addEventListener("click", () => {
    const isCloud = !$("cloudView").classList.contains("hidden");
    if (isCloud) {
      currentFolder = 0;
      loadFolder(0);
    } else {
      setTab("cloud");
    }
  });
  $("searchTab").addEventListener("click", async () => {
    await setTab("search");
    if (typeof scheduleClipboardMagnetCheck === "function") scheduleClipboardMagnetCheck("tab");
  });

  $("refreshBtn").addEventListener("click", () => loadFolder(currentFolder));
  $("upBtn").addEventListener("click", () => { if (typeof window.cloudGoUp === "function") window.cloudGoUp(); });
  $("openBtn").addEventListener("click", () => openItem());
  $("downloadBtn").addEventListener("click", downloadSelected);
  if ($("copyLinkBtn")) $("copyLinkBtn").addEventListener("click", copySelectedLink);
  if ($("telegramBtn")) $("telegramBtn").addEventListener("click", () => {
    if (typeof sendSelectedToTelegram === "function") sendSelectedToTelegram();
  });
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
  window.toggleDd = function(ddId) {
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
  window.filterSearchTimer = null;
  window.debouncedFilterSearch = function() {
    if (typeof search !== "function" || !$("searchQuery").value.trim()) return;
    clearTimeout(filterSearchTimer);
    filterSearchTimer = setTimeout(() => search(false, 1), 350);
  }
  document.querySelectorAll(".qualityOpt, .encoderOpt").forEach((el) =>
    el.addEventListener("change", () => {
      if (typeof updateDropdownLabels === "function") updateDropdownLabels();
      debouncedFilterSearch();
    })
  );


  // Mobile search filters: bottom sheet mirrors the desktop dropdown checkbox state.
  window.syncMobileFiltersFromDesktop = function() {
    document.querySelectorAll(".mQualityOpt").forEach((m) => {
      const d = document.querySelector(`.qualityOpt[value="${m.value}"]`);
      if (d) m.checked = d.checked;
    });
    document.querySelectorAll(".mEncoderOpt").forEach((m) => {
      const d = document.querySelector(`.encoderOpt[value="${m.value}"]`);
      if (d) m.checked = d.checked;
    });
  }
  window.syncDesktopFiltersFromMobile = function() {
    document.querySelectorAll(".mQualityOpt").forEach((m) => {
      const d = document.querySelector(`.qualityOpt[value="${m.value}"]`);
      if (d) d.checked = m.checked;
    });
    document.querySelectorAll(".mEncoderOpt").forEach((m) => {
      const d = document.querySelector(`.encoderOpt[value="${m.value}"]`);
      if (d) d.checked = m.checked;
    });
    if (typeof updateDropdownLabels === "function") updateDropdownLabels();
  }
  window.closeMobileFilters = function() {
    const sheet = $("mobileFilterSheet");
    if (!sheet || sheet.classList.contains("hidden")) return;
    
    sheet.classList.add("mfs-closing");
    const panel = sheet.querySelector(".mfs-panel");
    const onEnd = () => {
      sheet.classList.remove("mfs-closing");
      sheet.classList.add("hidden");
      sheet.setAttribute("aria-hidden", "true");
      panel.removeEventListener("animationend", onEnd);
    };
    panel.addEventListener("animationend", onEnd);
    
    setTimeout(() => {
      if (sheet.classList.contains("mfs-closing")) {
        onEnd();
      }
    }, 350);
  }
  window.openMobileFilters = function() {
    if (typeof isMobileSearchUi === "function" && !isMobileSearchUi()) {
      const sidebar = $("searchSidebar");
      if (sidebar) {
        sidebar.classList.toggle("collapsed");
      }
      return;
    }
    const sheet = $("mobileFilterSheet");
    if (!sheet) return;
    syncMobileFiltersFromDesktop();
    sheet.classList.remove("hidden");
    sheet.setAttribute("aria-hidden", "false");
  }
  if ($("mobileFilterBtn")) $("mobileFilterBtn").addEventListener("click", openMobileFilters);
  if ($("mobileFilterClose")) $("mobileFilterClose").addEventListener("click", closeMobileFilters);
  if ($("mobileFilterApply")) $("mobileFilterApply").addEventListener("click", () => {
    syncDesktopFiltersFromMobile();
    closeMobileFilters();
    if (typeof search === "function" && $("searchQuery").value.trim()) {
      search(false, 1);
    }
  });
  if ($("mobileFilterSheet")) $("mobileFilterSheet").addEventListener("click", (e) => {
    if (e.target.dataset.close === "1") closeMobileFilters();
  });

  // ----- Mobile cloud wiring -----
  if ($("cmUpBtn")) $("cmUpBtn").addEventListener("click", () => { if (typeof window.cloudGoUp === "function") window.cloudGoUp(); });
  if ($("cmRefreshBtn")) $("cmRefreshBtn").addEventListener("click", () => loadFolder(currentFolder));
  if ($("cmSelectAll")) $("cmSelectAll").addEventListener("change", (e) => {
    if (e.target.checked) { for (const it of items) selectedKeys.add(it.key); }
    else { selectedKeys.clear(); }
    updateSelection();
  });
  if ($("cmBulkDownload")) $("cmBulkDownload").addEventListener("click", downloadSelected);
  if ($("cmBulkCopy")) $("cmBulkCopy").addEventListener("click", copySelectedLink);
  if ($("cmBulkTelegram")) $("cmBulkTelegram").addEventListener("click", sendSelectedToTelegram);
  if ($("cmBulkDelete")) $("cmBulkDelete").addEventListener("click", deleteSelected);

  if ($("pasteBtn")) {
    $("pasteBtn").addEventListener("click", async () => {
      const added = typeof ingestClipboardMagnet === "function" ? await ingestClipboardMagnet(true) : false;
      if (added) return;
      try {
        const text = await navigator.clipboard.readText();
        $("searchQuery").value = text;
        $("searchQuery").focus();
        if (typeof setMagnetUiState === "function") setMagnetUiState(text);
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
    if (e.key === "Escape") {
      if (!$("loginScreen").classList.contains("hidden")) {
        $("loginScreen").classList.add("hidden");
        $("appScreen").classList.remove("hidden");
      }
      if (!$("telegramAuthOverlay").classList.contains("hidden")) {
        $("telegramAuthOverlay").classList.add("hidden");
      }
      if (!$("telegramTransfersOverlay").classList.contains("hidden")) {
        const closeTransfers = $("closeTelegramTransfersBtn");
        if (closeTransfers) closeTransfers.click();
      }
    }
  });

  // Dismiss overlay when backdrop is clicked
  document.querySelectorAll(".overlay").forEach((ov) => {
    ov.addEventListener("click", (e) => {
      if (e.target === ov) {
        ov.classList.add("hidden");
        if (ov.id === "telegramTransfersOverlay") {
          const btn = $("closeTelegramTransfersBtn");
          if (btn) btn.click();
        } else if (ov.id === "historyOverlay") {
          const btn = $("closeHistoryBtn");
          if (btn) btn.click();
        } else if (ov.id === "videoOverlay") {
          const btn = $("closeVideoBtn");
          if (btn) btn.click();
        } else if (ov.id === "telegramAuthOverlay") {
          const btn = $("closeTelegramAuthBtn");
          if (btn) btn.click();
        }
      }
    });
  });

  // ----- Linked Devices modal (click account email in topbar) -----
  window.esc = function(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  window.openDevicesModal = async function() {
    const ov = $("devicesOverlay");
    if (!ov) return;
    const body = $("devicesBody");
    const empty = $("devicesEmpty");
    const status = $("devicesStatus");
    const sub = $("devicesSubtitle");
    if (body) body.innerHTML = "";
    if (empty) empty.classList.add("hidden");
    if (status) status.textContent = "Loading devices…";
    ov.classList.remove("hidden");
    try {
      const res = await fetch("/api/devices", { credentials: "same-origin" });
      const data = await res.json();
      const devices = (data && data.devices) || [];
      if (status) status.textContent = "";
      if (!devices.length) {
        if (empty) empty.classList.remove("hidden");
        if (sub) sub.textContent = "Apps & clients authorized on this Seedr account";
        return;
      }
      if (sub) sub.textContent = `${devices.length} client${devices.length > 1 ? "s" : ""} authorized on this Seedr account`;
      if (body) body.innerHTML = devices.map((d) =>
        `<tr><td class="truncate">${esc(d.name) || "Unknown client"}</td>` +
        `<td class="truncate muted">${esc(d.id) || "—"}</td></tr>`
      ).join("");
    } catch (e) {
      if (status) status.textContent = "Failed to load devices.";
    }
  }
  if ($("accountLabel")) {
    $("accountLabel").addEventListener("click", openDevicesModal);
    $("accountLabel").addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDevicesModal(); }
    });
  }
  if ($("closeDevicesBtn")) $("closeDevicesBtn").addEventListener("click", () => {
    $("devicesOverlay").classList.add("hidden");
  });

  // Telegram auth and settings controls
  if ($("closeTelegramAuthBtn")) {
    $("closeTelegramAuthBtn").addEventListener("click", () => {
      $("telegramAuthOverlay").classList.add("hidden");
    });
  }

  if ($("tgUnlinkBtn")) {
    $("tgUnlinkBtn").addEventListener("click", async () => {
      if (!confirm("Are you sure you want to unlink your Telegram account?")) return;
      updateStatus($("tgAuthStatus"), "Unlinking account...", "");
      try {
        await postJson("/api/telegram/logout", {});
        updateStatus($("tgAuthStatus"), "Account unlinked!", "ok");
        toast("Telegram account unlinked.");
        $("tgLinkedStep").classList.add("hidden");
        $("tgUnlinkedStep").classList.remove("hidden");
        $("tgPhoneStep").classList.remove("hidden");
        $("tgCodeStep").classList.add("hidden");
      } catch (err) {
        updateStatus($("tgAuthStatus"), err.message || "Failed to unlink account", "error");
      }
    });
  }

  if ($("tgSendCodeBtn")) {
    $("tgSendCodeBtn").addEventListener("click", async () => {
      const phone = $("tgPhone").value.trim();
      if (!phone) return updateStatus($("tgAuthStatus"), "Enter your phone number", "error");
      updateStatus($("tgAuthStatus"), "Requesting code...", "");
      try {
        await postJson("/api/telegram/send-code", { phone });
        updateStatus($("tgAuthStatus"), "Verification code sent to Telegram app", "ok");
        $("tgPhoneStep").classList.add("hidden");
        $("tgCodeStep").classList.remove("hidden");
        $("tgCode").focus();
      } catch (err) {
        updateStatus($("tgAuthStatus"), err.message || "Failed to send code", "error");
      }
    });
  }

  if ($("tgVerifyCodeBtn")) {
    $("tgVerifyCodeBtn").addEventListener("click", async () => {
      const code = $("tgCode").value.trim();
      if (!code) return updateStatus($("tgAuthStatus"), "Enter the verification code", "error");
      updateStatus($("tgAuthStatus"), "Verifying...", "");
      try {
        await postJson("/api/telegram/verify-code", { code });
        updateStatus($("tgAuthStatus"), "Telegram successfully linked!", "ok");
        toast("Telegram account linked successfully!");
        setTimeout(() => {
          $("telegramAuthOverlay").classList.add("hidden");
          if (typeof sendSelectedToTelegram === "function") sendSelectedToTelegram();
        }, 1500);
      } catch (err) {
        updateStatus($("tgAuthStatus"), err.message || "Verification failed", "error");
      }
    });
  }

    $("clearSearchBtn").addEventListener("click", () => {
      // Clear only the search text (and hide stale suggestions); keep results on screen
      clearTimeout(suggestTimer);
      $("searchQuery").value = "";
      $("suggestBox").classList.add("hidden");
      $("suggestBox").textContent = "";
      $("searchQuery").focus();
      // restore the Search button in case an "Add Link" state was showing
      if (typeof setSearchAction === "function") setSearchAction("search");
    });
  // Automatically toggle Search vs Add button based on input content
  $("searchQuery").addEventListener("input", (e) => {
    getSuggestions();
    const q = e.target.value.trim();
    if (typeof maybeAutoAddMagnet === "function" && maybeAutoAddMagnet(q, "input")) return;
    if (typeof setMagnetUiState === "function") setMagnetUiState(q);
  });
  $("searchQuery").addEventListener("paste", () => {
    setTimeout(() => {
      const q = $("searchQuery").value.trim();
      if (typeof maybeAutoAddMagnet === "function") maybeAutoAddMagnet(q, "paste");
    }, 0);
  });

  $("addMagnetBtn").addEventListener("click", () => search(false, 1));
  $("searchQuery").addEventListener("keydown", (e) => {
    if (e.key === "Enter") search(false, 1);
    else if (e.key === "Escape") $("suggestBox").classList.add("hidden");
  });
  $("searchQuery").addEventListener("blur", () => {
    setTimeout(() => {
      if (document.activeElement !== $("searchQuery")) {
        $("suggestBox").classList.add("hidden");
      }
    }, 150);
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".search-bar-integrated")) $("suggestBox").classList.add("hidden"); });
  $("closeVideoBtn").addEventListener("click", () => {
    const video = $("videoPlayer");
    video.pause();
    video.removeAttribute("src");
    video.load();
    $("videoOverlay").classList.add("hidden");
  });

  window.addEventListener("focus", () => {
    if (typeof scheduleClipboardMagnetCheck === "function") scheduleClipboardMagnetCheck("focus");
  });

  // Initialization Sequence
  window.init = async function() {
    try {
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const key = localStorage.key(i);
        if (key && key.startsWith("streamly:autoAddedMagnet:")) {
          localStorage.removeItem(key);
        }
      }
    } catch (_) {}

    let initialTab = window.location.hash.replace("#", "") || "search";
    if (initialTab !== "cloud" && initialTab !== "search") initialTab = "search";
    
    // Optimistically show header and search tab immediately
    showApp(null); 
    const hadUrlMagnet = typeof ingestUrlMagnet === "function" && ingestUrlMagnet();
    if (initialTab === "search") {
      setTab("search");
      if (!hadUrlMagnet && typeof ingestClipboardMagnet === "function") ingestClipboardMagnet(true);
    }

    try {
      let data;
      try {
        data = await parseResponse(await fetch("/api/status", { credentials: "same-origin", cache: "no-store" }));
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
        if (initialTab === "search" && typeof ingestClipboardMagnet === "function") ingestClipboardMagnet(true);
        if (initialTab === "cloud") {
          setTab("cloud");
          await loadFolder(0);
        } else if (typeof refreshStorageSnapshot === "function") {
          refreshStorageSnapshot();
        }
      }
    } catch (_) {
      // Not authenticated. Force them to search tab (Guest mode).
      setTab("search");
    }
  }

  init();

  // Enable instant touch active states on mobile (iOS/Android Safari/Chrome)
  document.addEventListener("touchstart", () => {}, { passive: true });
