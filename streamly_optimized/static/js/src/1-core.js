  window.csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  window.currentFolder = 0;
  window.parentFolder = 0;
  window.selectedKeys = new Set();
  window.lastClickedKey = null;
  window.selected = null;
  window.transfers = [];
  window.cloudAutoRefreshTimer = null;
  window.CLOUD_TRANSFER_REFRESH_MS = 8000;
  window.refreshSelectedShim = function() {
    if (window.selectedKeys.size === 0) { window.selected = null; return; }
    const firstKey = window.selectedKeys.values().next().value;
    window.selected = window.items.find(it => it.key === firstKey) || null;
  };
  window.items = [];
  window.suggestTimer = null;
  window.currentSort = "size";
  window.currentOrder = "asc";
  window.currentPage = 1;
  window.isAuthenticated = false;
  window.lastAutoAddedMagnet = "";
  window.autoAddTimer = null;
  window.clipboardMagnetCheckTimer = null;
  window.lastClipboardMagnetCheckAt = 0;
  window.CLIPBOARD_MAGNET_CHECK_DEBOUNCE_MS = 1200;


  window.$ = (id) => document.getElementById(id);

  window.status = function(el, message, kind) {
    el.textContent = message || "";
    el.className = "status" + (kind ? " " + kind : "");
  };

  window.toast = function(message) {
    const box = window.$("toast");
    box.textContent = message;
    box.classList.remove("hidden");
    setTimeout(() => box.classList.add("hidden"), 2600);
  };

  // Silent-relogin state: debounced so transient failures don't permanently disable.
  window.silentReloginAttempted = false;
  window.silentReloginTimer = null;
  window.SILENT_RELOGIN_DEBOUNCE_MS = 8000;

  window.attemptSilentRelogin = async function() {
    if (window.silentReloginAttempted) return false;
    window.silentReloginAttempted = true;
    try {
      const r = await fetch("/api/login/silent", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": window.csrfToken || "" },
      });
      if (r.ok) {
        const data = await r.json().catch(() => ({}));
        if (data && data.success) {
          window.showApp(data.username || "Logged in");
          // Reset flag after debounce window on success
          clearTimeout(window.silentReloginTimer);
          window.silentReloginTimer = setTimeout(() => { window.silentReloginAttempted = false; }, window.SILENT_RELOGIN_DEBOUNCE_MS);
          return true;
        }
      }
    } catch (_) {}
    // On failure: allow retry after debounce window
    clearTimeout(window.silentReloginTimer);
    window.silentReloginTimer = setTimeout(() => { window.silentReloginAttempted = false; }, window.SILENT_RELOGIN_DEBOUNCE_MS);
    return false;
  };

  window.parseResponse = async function(response) {
    if (response.status === 401 && !response.url.includes("/api/status") && !response.url.includes("/api/login")) {
      // Try silent re-login but do NOT show login popup here.
      // The caller (setTab or loadFolder) is responsible for that decision.
      await window.attemptSilentRelogin();
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.success === false) {
      const message = data && data.error && data.error.message ? data.error.message : `HTTP ${response.status}`;
      throw new Error(message);
    }
    return data;
  };

  window.postJson = async function(url, body) {
    if (!window.csrfToken) {
      const data = await window.parseResponse(await fetch("/api/csrf", { credentials: "same-origin" }));
      window.csrfToken = data.csrfToken;
    }
    return window.parseResponse(await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": window.csrfToken },
      body: JSON.stringify(body || {})
    }));
  };

  window.showApp = function(username) {
    window.isAuthenticated = !!username;
    window.$("loginScreen").classList.add("hidden");
    window.$("appScreen").classList.remove("hidden");
    const userPill = window.$("userPill");
    if (userPill) {
      userPill.classList.add("hidden");
      userPill.textContent = username ? username : "Guest";
    }
    const accountLabel = window.$("accountLabel");
    if (accountLabel) accountLabel.textContent = username ? username : "Guest Mode";
    const cmAcct = window.$("cmAccount");
    if (cmAcct) cmAcct.textContent = username ? `Connected as ${username}` : "Guest Mode";
  };

  window.showLogin = function() {
    window.$("loginScreen").classList.remove("hidden");
  };

  window.fmtDate = function(value) {
    if (!value) return "-";
    const d = new Date(value);
    return isNaN(d.getTime()) ? String(value).slice(0, 19) : d.toLocaleString();
  };

  // Client-side error reporting (Phase 4 / S16)
  window.onerror = function(message, source, lineno, colno, error) {
    fetch('/api/client-log', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken || '' },
      body: JSON.stringify({
        message: message,
        url: source,
        line: lineno,
        column: colno,
        stack: error ? error.stack : ''
      })
    }).catch(() => {});
  };
  window.onunhandledrejection = function(event) {
    fetch('/api/client-log', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken || '' },
      body: JSON.stringify({
        message: 'Unhandled promise rejection: ' + event.reason,
        url: window.location.href,
        stack: event.reason && event.reason.stack ? event.reason.stack : ''
      })
    }).catch(() => {});
  };

