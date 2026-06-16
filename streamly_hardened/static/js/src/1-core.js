  let csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  let currentFolder = 0;
  let parentFolder = 0;
  let selectedKeys = new Set();
  let lastClickedKey = null;
  // Backwards-compat shim: code reading "selected" expects single item.
  // We expose a getter that returns the first selected item or null.
  let selected = null;
  let transfers = [];
  let cloudAutoRefreshTimer = null;
  const CLOUD_TRANSFER_REFRESH_MS = 8000;
  function refreshSelectedShim() {
    if (selectedKeys.size === 0) { selected = null; return; }
    const firstKey = selectedKeys.values().next().value;
    selected = items.find(it => it.key === firstKey) || null;
  }
  let items = [];
  let suggestTimer = null;
  let currentSort = "size";
  let currentOrder = "asc";
  let currentPage = 1;
  let isAuthenticated = false;
  let lastAutoAddedMagnet = "";
  let autoAddTimer = null;
  let clipboardMagnetCheckTimer = null;
  let lastClipboardMagnetCheckAt = 0;
  const CLIPBOARD_MAGNET_CHECK_DEBOUNCE_MS = 1200;


  const $ = (id) => document.getElementById(id);

  function status(el, message, kind) {
    el.textContent = message || "";
    el.className = "status" + (kind ? " " + kind : "");
  }

  function toast(message) {
    const box = $("toast");
    box.textContent = message;
    box.classList.remove("hidden");
    setTimeout(() => box.classList.add("hidden"), 2600);
  }

  // Silent-relogin state: debounced so transient failures don't permanently disable.
  let silentReloginAttempted = false;
  let silentReloginTimer = null;
  const SILENT_RELOGIN_DEBOUNCE_MS = 8000;

  async function attemptSilentRelogin() {
    if (silentReloginAttempted) return false;
    silentReloginAttempted = true;
    try {
      const r = await fetch("/api/login/silent", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrfToken || "" },
      });
      if (r.ok) {
        const data = await r.json().catch(() => ({}));
        if (data && data.success) {
          showApp(data.username || "Logged in");
          // Reset flag after debounce window on success
          clearTimeout(silentReloginTimer);
          silentReloginTimer = setTimeout(() => { silentReloginAttempted = false; }, SILENT_RELOGIN_DEBOUNCE_MS);
          return true;
        }
      }
    } catch (_) {}
    // On failure: allow retry after debounce window
    clearTimeout(silentReloginTimer);
    silentReloginTimer = setTimeout(() => { silentReloginAttempted = false; }, SILENT_RELOGIN_DEBOUNCE_MS);
    return false;
  }

  async function parseResponse(response) {
    if (response.status === 401 && !response.url.includes("/api/status") && !response.url.includes("/api/login")) {
      // Try silent re-login but do NOT show login popup here.
      // The caller (setTab or loadFolder) is responsible for that decision.
      await attemptSilentRelogin();
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.success === false) {
      const message = data && data.error && data.error.message ? data.error.message : `HTTP ${response.status}`;
      throw new Error(message);
    }
    return data;
  }

  async function postJson(url, body) {
    if (!csrfToken) {
      const data = await parseResponse(await fetch("/api/csrf", { credentials: "same-origin" }));
      csrfToken = data.csrfToken;
    }
    return parseResponse(await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify(body || {})
    }));
  }

  function showApp(username) {
    isAuthenticated = !!username;
    $("loginScreen").classList.add("hidden");
    $("appScreen").classList.remove("hidden");
    const userPill = $("userPill");
    if (userPill) {
      userPill.classList.add("hidden");
      userPill.textContent = username ? username : "Guest";
    }
    const accountLabel = $("accountLabel");
    if (accountLabel) accountLabel.textContent = username ? username : "Guest Mode";
    const cmAcct = $("cmAccount");
    if (cmAcct) cmAcct.textContent = username ? `Connected as ${username}` : "Guest Mode";
  }

  function showLogin() {
    $("loginScreen").classList.remove("hidden");
  }

  function fmtDate(value) {
    if (!value) return "-";
    const d = new Date(value);
    return isNaN(d.getTime()) ? String(value).slice(0, 19) : d.toLocaleString();
  }

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

