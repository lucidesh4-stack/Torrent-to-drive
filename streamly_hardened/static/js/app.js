
(() => {
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

  let storageSnapshotLoading = false;
  let storageSnapshotLoaded = false;
  let seedrQueue = [];
  let lastRequestedFolderId = 0;
  // Navigation stack of folder IDs we descended through. Bottom is always root (0),
  // so going "up" from a top-level folder reliably returns to 0 (where queue +
  // loading-transfers are shown) regardless of Seedr's non-zero root parent_id.
  let folderStack = [];
  // Expose a deterministic "go up" the Up buttons can call.
  window.cloudGoUp = function () {
    if (currentFolder === 0) return;
    const target = folderStack.length ? folderStack.pop() : 0;
    loadFolder(target, { _fromStack: true });
  };
  // Expose folder-open that records the stack.
  window.cloudOpenFolder = function (id) {
    folderStack.push(currentFolder || 0);
    loadFolder(id);
  };

  function updateSelection() {
    refreshSelectedShim();
    const count = selectedKeys.size;
    const heading = $("selectionHeading");
    const clearBtn = $("clearSelBtn");

    if (count === 0) {
      heading.textContent = "Selected Item";
      $("selName").textContent = "None";
      $("selType").textContent = "-";
      $("selSize").textContent = "-";
      clearBtn.style.display = "none";
    } else if (count === 1) {
      const item = selected;
      heading.textContent = "Selected Item";
      $("selName").textContent = item ? item.name : "None";
      $("selType").textContent = item ? item.type : "-";
      $("selSize").textContent = item ? (item.size_str || "-") : "-";
      clearBtn.style.display = "";
    } else {
      // Multi-select: aggregate
      const selectedItems = items.filter(it => selectedKeys.has(it.key));
      const totalBytes = selectedItems.reduce((sum, it) => sum + Number(it.size || 0), 0);
      const types = new Set(selectedItems.map(it => it.type));
      heading.textContent = `${count} items selected`;
      $("selName").textContent = `${selectedItems.length} items`;
      $("selType").textContent = types.size === 1 ? [...types][0] + "s" : "mixed";
      $("selSize").textContent = bytes(totalBytes);
      clearBtn.style.display = "";
    }

    // Visual: toggle row classes + checkbox state
    document.querySelectorAll("#cloudBody tr").forEach((tr) => {
      const isSel = selectedKeys.has(tr.dataset.key);
      tr.classList.toggle("selected", isSel);
      const cb = tr.querySelector(".row-check");
      if (cb) cb.checked = isSel;
    });

    // Master checkbox indeterminate state
    const allCb = $("selectAllCheck");
    if (allCb) {
      if (count === 0) { allCb.checked = false; allCb.indeterminate = false; }
      else if (count === items.length) { allCb.checked = true; allCb.indeterminate = false; }
      else { allCb.checked = false; allCb.indeterminate = true; }
    }

    // Open button: disabled when multi-select (open only makes sense for one)
    $("openBtn").disabled = count !== 1;
    const copyBtn = $("copyLinkBtn");
    if (copyBtn) copyBtn.disabled = count === 0;
    const selectedFiles = Array.from(selectedKeys).map(k => items.find(x => x.key === k)).filter(x => x && x.type === "file");
    const hasFolder = Array.from(selectedKeys).map(k => items.find(x => x.key === k)).some(x => x && x.type === "folder");
    const telegramBtn = $("telegramBtn");
    if (telegramBtn) telegramBtn.disabled = selectedFiles.length === 0 || hasFolder;

    // ----- Mobile selection sync -----
    document.querySelectorAll("#cloudMobileList .cm-row").forEach((row) => {
      row.classList.toggle("sel", selectedKeys.has(row.dataset.key));
    });
    const bulk = $("cloudBulkBar");
    if (bulk) {
      bulk.classList.toggle("hidden", count === 0);
      const bc = $("cmBulkCount");
      if (bc) bc.textContent = String(count);
    }
    const tgBtn = $("cmBulkTelegram");
    if (tgBtn) tgBtn.disabled = selectedFiles.length === 0 || hasFolder;
    // Mobile select-all checkbox state
    const cmAll = $("cmSelectAll");
    if (cmAll) {
      if (count === 0) { cmAll.checked = false; cmAll.indeterminate = false; }
      else if (count === items.length) { cmAll.checked = true; cmAll.indeterminate = false; }
      else { cmAll.checked = false; cmAll.indeterminate = true; }
    }
  }

  function toggleKey(key, additive, range) {
    if (range && lastClickedKey) {
      // Shift+click: select range between lastClickedKey and key
      const visibleKeys = items.map(it => it.key);
      const i1 = visibleKeys.indexOf(lastClickedKey);
      const i2 = visibleKeys.indexOf(key);
      if (i1 !== -1 && i2 !== -1) {
        const [lo, hi] = i1 < i2 ? [i1, i2] : [i2, i1];
        for (let i = lo; i <= hi; i++) selectedKeys.add(visibleKeys[i]);
      }
    } else if (additive) {
      // Ctrl/Cmd+click: toggle
      if (selectedKeys.has(key)) selectedKeys.delete(key);
      else selectedKeys.add(key);
      lastClickedKey = key;
    } else {
      // Plain click: single-select
      selectedKeys.clear();
      selectedKeys.add(key);
      lastClickedKey = key;
    }
    updateSelection();
  }

  function transferPct(t) {
    const n = Number(t && t.progress);
    if (!isFinite(n)) return 0;
    return Math.max(0, Math.min(100, n));
  }

  function transferMeta(t) {
    const parts = [];
    const pct = transferPct(t).toFixed(1).replace(/\.0$/, "");
    parts.push(pct + "%");
    if (t && t.status) parts.push(t.status);
    if (t && t.download_rate_str && t.download_rate > 0) parts.push(t.download_rate_str);
    if (t && t.seeders) parts.push(t.seeders + " seeders");
    return parts.join(" · ");
  }

  function transferBar(t) {
    const bar = document.createElement("div");
    bar.className = "transfer-bar";
    const fill = document.createElement("div");
    fill.style.width = transferPct(t).toFixed(1) + "%";
    bar.appendChild(fill);
    return bar;
  }

  function renderTransferRow(t) {
    const tr = document.createElement("tr");
    tr.className = "transfer-row";
    const iconTd = document.createElement("td");
    iconTd.textContent = "⏳";
    iconTd.title = "Transfer loading";

    const nameTd = document.createElement("td");
    const box = document.createElement("div");
    box.className = "transfer-cell";
    const title = document.createElement("div");
    title.className = "transfer-title truncate";
    title.textContent = t.name || "Loading torrent";
    title.title = t.name || "";
    const meta = document.createElement("div");
    meta.className = "transfer-meta";
    meta.textContent = transferMeta(t);
    box.append(title, transferBar(t), meta);
    nameTd.appendChild(box);

    const typeTd = document.createElement("td");
    typeTd.className = "muted";
    typeTd.textContent = "loading";
    const sizeTd = document.createElement("td");
    sizeTd.className = "muted";
    sizeTd.textContent = t.size_str || "-";
    const dateTd = document.createElement("td");
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "transfer-cancel-btn";
    cancelBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-x"><path d="M18 6 6 18M6 6l12 12"/></svg>`;
    cancelBtn.title = "Cancel transfer";
    cancelBtn.addEventListener("click", () => cancelTransfer(t));
    dateTd.appendChild(cancelBtn);
    tr.append(iconTd, nameTd, typeTd, sizeTd, dateTd);
    return tr;
  }

  function renderQueuedRow(q) {
    const tr = document.createElement("tr");
    tr.className = "transfer-row queued-row";
    
    const iconTd = document.createElement("td");
    iconTd.textContent = "⏱️";
    iconTd.title = "Queued for download";

    const nameTd = document.createElement("td");
    const box = document.createElement("div");
    box.className = "transfer-cell";
    const title = document.createElement("div");
    title.className = "transfer-title truncate";
    title.textContent = q.name || "Queued torrent";
    title.title = q.name || "";
    const meta = document.createElement("div");
    meta.className = "transfer-meta";
    meta.textContent = "Queued (Waiting for storage/idle slot)";
    box.append(title, meta);
    nameTd.appendChild(box);

    const typeTd = document.createElement("td");
    typeTd.className = "muted";
    typeTd.textContent = "queued";
    
    const sizeTd = document.createElement("td");
    sizeTd.className = "muted";
    sizeTd.textContent = q.size ? bytes(q.size) : "-";
    
    const dateTd = document.createElement("td");
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "transfer-cancel-btn";
    cancelBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-x"><path d="M18 6 6 18M6 6l12 12"/></svg>`;
    cancelBtn.title = "Cancel queue item";
    cancelBtn.addEventListener("click", () => cancelQueuedItem(q));
    dateTd.appendChild(cancelBtn);
    
    tr.append(iconTd, nameTd, typeTd, sizeTd, dateTd);
    return tr;
  }

  function syncCloudAutoRefresh() {
    clearTimeout(cloudAutoRefreshTimer);
    cloudAutoRefreshTimer = null;
    const cloudVisible = $("cloudView") && !$("cloudView").classList.contains("hidden");
    if (isAuthenticated && cloudVisible && (transfers.length > 0 || seedrQueue.length > 0)) {
      cloudAutoRefreshTimer = setTimeout(() => loadFolder(currentFolder || 0, { silent: true }), CLOUD_TRANSFER_REFRESH_MS);
    }
  }

  function renderCloud() {
    const body = $("cloudBody");
    body.textContent = "";
    const pathLabel = $("pathLabel");
    if (pathLabel) pathLabel.textContent = `Folder ID: ${currentFolder}`;
    $("upBtn").disabled = currentFolder == 0;
    $("cloudEmpty").classList.toggle("hidden", items.length + transfers.length + seedrQueue.length !== 0);
    selectedKeys.clear();
    lastClickedKey = null;
    updateSelection();

    // 1. Render items (folders and files) FIRST
    for (const item of items) {
      const tr = document.createElement("tr");
      tr.dataset.key = item.key;

      const checkTd = document.createElement("td");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "row-check";
      cb.addEventListener("click", (e) => { e.stopPropagation(); toggleKey(item.key, true, false); });
      checkTd.appendChild(cb);

      const nameTd = document.createElement("td");
      const nameCell = document.createElement("div");
      nameCell.className = "name-cell";
      const icon = document.createElement("span");
      icon.className = "icon";
      if (item.type === "folder") {
        icon.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#eab308" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-folder"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>`;
      } else {
        icon.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-video"><path d="m22 8-6 4 6 4V8Z"/><rect width="14" height="12" x="2" y="6" rx="2" ry="2"/></svg>`;
      }
      const name = document.createElement("span");
      name.className = "truncate";
      name.textContent = item.name || "Unnamed";
      nameCell.append(icon, name);
      nameTd.appendChild(nameCell);

      const typeTd = document.createElement("td");
      typeTd.className = "muted";
      typeTd.textContent = item.type;

      const sizeTd = document.createElement("td");
      sizeTd.className = "muted";
      sizeTd.textContent = item.size_str || "-";

      const dateTd = document.createElement("td");
      dateTd.className = "muted";
      dateTd.textContent = fmtDate(item.last_update);

      tr.append(checkTd, nameTd, typeTd, sizeTd, dateTd);
      tr.addEventListener("click", (e) => {
        if (e.target.closest(".row-check")) return; // checkbox handles its own
        toggleKey(item.key, e.ctrlKey || e.metaKey, e.shiftKey);
      });
      tr.addEventListener("dblclick", () => openItem(item));
      body.appendChild(tr);
    }

    // 2. Render transfers SECOND - only if currentFolder == 0
    if (currentFolder == 0) {
      for (const t of transfers) body.appendChild(renderTransferRow(t));
    }

    // 3. Render queued items THIRD - only if currentFolder == 0
    if (currentFolder == 0) {
      for (const q of seedrQueue) body.appendChild(renderQueuedRow(q));
    }

    renderCloudMobile();
  }

  let cmTapTimer = null; // distinguishes single-tap (select) from double-tap (open)

  function renderCloudMobile() {
    const list = $("cloudMobileList");
    if (!list) return;
    list.textContent = "";
    const cnt = $("cmCount");
    if (cnt) {
      let activeCount = transfers.length + seedrQueue.length;
      let activeText = activeCount ? ` · ${activeCount} pending` : "";
      cnt.textContent = `${items.length} item${items.length === 1 ? "" : "s"}${activeText}`;
    }
    const empty = $("cloudMobileEmpty");
    if (empty) empty.classList.toggle("hidden", items.length + transfers.length + seedrQueue.length !== 0);
    $("cmUpBtn").disabled = currentFolder == 0;

    // 1. Render items (folders and files) FIRST
    for (const item of items) {
      const row = document.createElement("div");
      row.className = "cm-row";
      row.dataset.key = item.key;

      const tick = document.createElement("div");
      tick.className = "cm-tick";
      tick.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-check"><polyline points="20 6 9 17 4 12"/></svg>`;

      const ic = document.createElement("div");
      ic.className = "cm-ic";
      if (item.type === "folder") {
        ic.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#eab308" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-folder"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>`;
      } else {
        ic.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-video"><path d="m22 8-6 4 6 4V8Z"/><rect width="14" height="12" x="2" y="6" rx="2" ry="2"/></svg>`;
      }

      const info = document.createElement("div");
      info.className = "cm-info";
      const fn = document.createElement("div");
      fn.className = "cm-fn";
      fn.textContent = item.name || "Unnamed";
      const meta = document.createElement("div");
      meta.className = "cm-meta";
      const s1 = document.createElement("span");
      s1.textContent = item.size_str || "-";
      const s2 = document.createElement("span");
      s2.textContent = fmtDate(item.last_update);
      meta.append(s1, s2);
      info.append(fn, meta);

      row.append(tick, ic, info);

      row.addEventListener("click", (e) => {
        if (cmTapTimer) {
          clearTimeout(cmTapTimer);
          cmTapTimer = null;
          openItem(item); // double-tap
          return;
        }
        cmTapTimer = setTimeout(() => {
          cmTapTimer = null;
          toggleKey(item.key, true, false); // single-tap toggles selection
        }, 240);
      });

      list.appendChild(row);
    }

    // 2. Render transfers SECOND - only if currentFolder == 0
    if (currentFolder == 0) {
      for (const t of transfers) {
        const row = document.createElement("div");
        row.className = "cm-row cm-transfer";
        const ic = document.createElement("div");
        ic.className = "cm-ic";
        ic.textContent = "⏳";
        const info = document.createElement("div");
        info.className = "cm-info";
        const fn = document.createElement("div");
        fn.className = "cm-fn";
        fn.textContent = t.name || "Loading torrent";
        const meta = document.createElement("div");
        meta.className = "cm-meta";
        meta.textContent = transferMeta(t) + (t.size_str ? " · " + t.size_str : "");
        
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "cm-transfer-cancel";
        cancel.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-x"><path d="M18 6 6 18M6 6l12 12"/></svg>`;
        cancel.title = "Cancel transfer";
        cancel.addEventListener("click", (e) => { e.stopPropagation(); cancelTransfer(t); });
        
        info.append(fn, transferBar(t), meta);
        row.append(ic, info, cancel);
        list.appendChild(row);
      }
    }

    // 3. Render queued items THIRD - only if currentFolder == 0
    if (currentFolder == 0) {
      for (const q of seedrQueue) {
        const row = document.createElement("div");
        row.className = "cm-row cm-transfer cm-queued";
        const ic = document.createElement("div");
        ic.className = "cm-ic";
        ic.textContent = "⏱️";
        const info = document.createElement("div");
        info.className = "cm-info";
        const fn = document.createElement("div");
        fn.className = "cm-fn";
        fn.textContent = q.name || "Queued torrent";
        const meta = document.createElement("div");
        meta.className = "cm-meta";
        meta.textContent = "Queued (Waiting for storage/idle slot)" + (q.size ? " · " + bytes(q.size) : "");
        
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "cm-transfer-cancel";
        cancel.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-x"><path d="M18 6 6 18M6 6l12 12"/></svg>`;
        cancel.title = "Cancel queued item";
        cancel.addEventListener("click", (e) => { e.stopPropagation(); cancelQueuedItem(q); });
        
        info.append(fn, meta);
        row.append(ic, info, cancel);
        list.appendChild(row);
      }
    }
  }



  function updateStorage(used, max) {
    storageSnapshotLoaded = true;
    const pct = max > 0 ? Math.min(100, Math.max(0, (used / max) * 100)) : 0;
    const label = `${bytes(used)} / ${bytes(max)} used (${pct.toFixed(1)}%)`;
    const compactLabel = `${bytes(used)} / ${bytes(max)} · ${pct.toFixed(1)}%`;

    const uGB = used / (1024 ** 3);
    const mGB = max / (1024 ** 3);
    const uText = uGB.toFixed(1);
    const mText = (mGB % 1 === 0) ? mGB.toFixed(0) : mGB.toFixed(1);
    const usedTotalLabel = `${uText} / ${mText} GB · ${pct.toFixed(0)}%`;

    const storageMeter = $("storageMeter");
    const storageText = $("storageText");
    if (storageMeter) storageMeter.style.width = pct.toFixed(1) + "%";
    if (storageText) storageText.textContent = label;

    const topMeter = $("topStorageMeter");
    const topText = $("topStorageText");
    const pctText = $("storagePercentText");
    const meterWrap = $("topStorageMeterWrap");

    if (topMeter) {
      topMeter.style.width = pct.toFixed(1) + "%";
      topMeter.style.backgroundImage = "none";
      topMeter.style.backgroundColor = pct >= 95 ? "#ef4444" : (pct >= 80 ? "#f59e0b" : "#2f9cf0");
      topMeter.style.boxShadow = pct >= 95 ? "0 0 8px rgba(239, 68, 68, 0.65)" : (pct >= 80 ? "0 0 8px rgba(245, 158, 11, 0.65)" : "0 0 8px rgba(47, 156, 240, 0.65)");
    }
    if (topText) topText.textContent = usedTotalLabel;
    if (pctText) pctText.textContent = pct.toFixed(0) + "%";
    if (meterWrap) meterWrap.title = `${used.toLocaleString()} / ${max.toLocaleString()} bytes`;

    const cmMeter = $("cmStorageMeter");
    const cmText = $("cmStorageText");
    if (cmMeter) cmMeter.style.width = pct.toFixed(1) + "%";
    if (cmText) cmText.textContent = compactLabel;
  }

  function bytes(n) {
    n = Number(n || 0);
    if (n >= 1024 ** 4) return (n / 1024 ** 4).toFixed(2) + " TB";
    if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(2) + " GB";
    if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
  }

  async function refreshStorageSnapshot(force = false) {
    if (!isAuthenticated) return;
    if (storageSnapshotLoading) return;
    if (storageSnapshotLoaded && !force) return;
    storageSnapshotLoading = true;
    try {
      const data = await parseResponse(await fetch("/fs/folder/0/items", { credentials: "same-origin", cache: "no-store" }));
      updateStorage(data.used || 0, data.max || 1);
    } catch (_) {
      // Silent by design: topbar storage should not interrupt Search/Guest flows.
    } finally {
      storageSnapshotLoading = false;
    }
  }

  async function loadFolder(id, opts = {}) {
    const silent = !!(opts && opts.silent);
    const folderId = Number(id) || 0;
    
    // Loading root directly (initial load, cloud-tab click, or refresh) clears the
    // navigation stack so "up" history stays consistent. _fromStack skips this.
    if (folderId === 0 && !opts._fromStack && !silent) folderStack = [];
    
    // Track requested folder to prevent race conditions on slow connections
    lastRequestedFolderId = folderId;
    
    if (!silent) status($("cloudStatus"), "Loading folder...", "");
    try {
      // Fetch Seedr folder contents (backend already embeds queue in root response via data.queue)
      const folderRes = await fetch(`/fs/folder/${encodeURIComponent(folderId)}/items`, { credentials: "same-origin", cache: "no-store" });
      const data = await parseResponse(folderRes);
      
      // If the user has navigated to another folder in the meantime, ignore this stale response
      if (lastRequestedFolderId !== folderId) return;
      
      currentFolder = folderId;
      parentFolder = Number(data.parent) || 0;
      items = [];
      transfers = [];
      // Only reset seedrQueue when loading root (non-root folders don't carry queue data)
      if (folderId === 0) seedrQueue = [];
      for (const transfer of data.transfers || []) transfers.push({ ...transfer, type: "transfer", key: `transfer:${transfer.id}` });
      for (const folder of data.folders || []) items.push({ ...folder, type: "folder", key: `folder:${folder.id}` });
      for (const file of data.files || []) items.push({ ...file, type: "file", key: `file:${file.id}` });
      
      // Populate queue from data.queue (already injected by backend for root folder)
      if (currentFolder == 0) {
        const rawQueue = Array.isArray(data.queue) ? data.queue : [];
        
        // Deduplicate: remove any queued items that are already active in transfers
        const activeMagnets = new Set(transfers.map(t => (t.magnet || "").toLowerCase()));
        const activeNames = new Set(transfers.map(t => (t.name || "").toLowerCase()));
        
        seedrQueue = rawQueue.filter(q => {
          if (q.magnet && activeMagnets.has(q.magnet.toLowerCase())) return false;
          if (q.name && activeNames.has(q.name.toLowerCase())) return false;
          return true;
        });
      }
      
      updateStorage(data.used || 0, data.max || 1);
      renderCloud();
      if (!silent) status($("cloudStatus"), `Loaded ${items.length} item(s)` + (transfers.length ? ` · ${transfers.length} loading` : "") + (seedrQueue.length ? ` · ${seedrQueue.length} queued` : "") + ".", "ok");
      syncCloudAutoRefresh();
    } catch (err) {
      if (lastRequestedFolderId !== folderId) return;
      if ((err.message || "").toLowerCase().includes("login")) showLogin();
      if (!silent) status($("cloudStatus"), err.message || "Failed to load folder", "error");
      syncCloudAutoRefresh();
    }
  }

  async function cancelTransfer(t) {
    if (!t || !t.id) return toast("Transfer id unavailable");
    if (!confirm(`Cancel transfer: ${t.name || "loading torrent"}?`)) return;
    status($("cloudStatus"), "Cancelling transfer...", "");
    try {
      await postJson("/api/transfer/cancel", { id: t.id });
      toast("Transfer cancelled");
      await loadFolder(currentFolder || 0, { silent: true });
      status($("cloudStatus"), "Transfer cancelled.", "ok");
    } catch (err) {
      const message = err.message || "Cancel failed";
      toast(message);
      status($("cloudStatus"), message, "error");
    }
  }

  async function cancelQueuedItem(q) {
    if (!q || !q.task_id) return toast("Task ID unavailable");
    if (!confirm(`Remove from queue: ${q.name || "queued torrent"}?`)) return;
    status($("cloudStatus"), "Cancelling queued item...", "");
    try {
      await postJson("/api/queue/cancel", { task_id: q.task_id });
      toast("Item removed from queue");
      await loadFolder(currentFolder || 0, { silent: true });
      status($("cloudStatus"), "Queue item removed.", "ok");
    } catch (err) {
      const message = err.message || "Failed to cancel queue item";
      toast(message);
      status($("cloudStatus"), message, "error");
    }
  }

  async function getFileUrl(item) {
    if (!item || item.type !== "file") throw new Error("Select a file first");
    const data = await parseResponse(await fetch(`/api/url?file_id=${encodeURIComponent(item.id)}`, { credentials: "same-origin" }));
    if (!data.url) throw new Error("No download/stream URL returned");
    return data.url;
  }

  async function copySelectedLink() {
    if (selectedKeys.size === 0) return toast("Select item(s) first");
    const selectedItems = items.filter(it => selectedKeys.has(it.key));
    if (selectedItems.length === 0) return toast("Select item(s) first");

    status($("cloudStatus"), selectedItems.length === 1 && selectedItems[0].type === "file" ? "Preparing file link..." : "Preparing zip link...", "");
    try {
      let url = "";
      if (selectedItems.length === 1 && selectedItems[0].type === "file") {
        url = await getFileUrl(selectedItems[0]);
      } else {
        const payload = selectedItems.map(it => ({ type: it.type, id: it.id }));
        const endpoint = payload.length === 1 ? "/api/zip" : "/api/zip/bulk";
        const body = payload.length === 1 ? { type: payload[0].type, id: payload[0].id } : { items: payload };
        const data = await postJson(endpoint, body);
        if (!data.url) throw new Error("Link URL was not returned");
        url = data.url;
      }
      if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error("Clipboard is not available in this browser");
      await navigator.clipboard.writeText(url);
      toast("Link copied to clipboard");
      status($("cloudStatus"), "Link copied to clipboard.", "ok");
    } catch (err) {
      const message = err.message || "Could not copy link";
      toast(message);
      status($("cloudStatus"), message, "error");
    }
  }


  async function openItem(item = selected) {
    if (!item) return toast("Select an item first");
    if (item.type === "folder") return window.cloudOpenFolder(item.id);
    try {
      const url = await getFileUrl(item);
      const ext = String(item.name || "").split(".").pop().toLowerCase();
      if (["mp4", "webm", "mov", "m4v", "mkv", "avi"].includes(ext)) {
        $("videoTitle").textContent = item.name || "Video";
        const video = $("videoPlayer");
        video.src = url;
        
        // Setup Native Player Button
        const nativeBtn = $("nativePlayerBtn");
        nativeBtn.onclick = () => {
          video.pause();
          // Open in StreamlyPlayer via deep link (Android + Windows)
          const deepLink = `streamlyplayer://play?url=${encodeURIComponent(url)}`;
          window.location.href = deepLink;
        };
        $("videoOverlay").classList.remove("hidden");
        video.play().catch(() => {});
      } else {
        window.open(url, "_blank", "noopener,noreferrer");
      }
    } catch (err) {
      toast(err.message || "Could not open item");
    }
  }

  async function downloadSelected() {
    if (selectedKeys.size === 0) return toast("Select item(s) first");
    const selectedItems = items.filter(it => selectedKeys.has(it.key));

    // Folders cannot be direct-downloaded — must be zipped
    const folders = selectedItems.filter(it => it.type === "folder");
    const files = selectedItems.filter(it => it.type === "file");

    if (folders.length > 0 && files.length === 0) {
      // All folders → redirect to zip
      return zipSelected();
    }
    if (folders.length > 0) {
      if (!confirm(`Selection has ${folders.length} folder(s). Folders will be zipped together with files. Continue?`)) return;
      return zipSelected();
    }

    // All files: trigger individual downloads with delay
    status($("cloudStatus"), `Downloading ${files.length} file(s)...`, "");
    let done = 0;
    for (const file of files) {
      try {
        const url = await getFileUrl(file);
        // Force "save" behavior: create hidden <a download> and click it
        const a = document.createElement("a");
        a.href = url;
        a.download = file.name || "";
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        done++;
        status($("cloudStatus"), `Downloading ${done}/${files.length}...`, "");
        if (done < files.length) await new Promise(r => setTimeout(r, 500));
      } catch (err) {
        toast(`Failed: ${file.name} — ${err.message}`);
      }
    }
    status($("cloudStatus"), `Started ${done} download(s).`, "ok");
  }

  async function zipSelected() {
    if (selectedKeys.size === 0) return toast("Select item(s) first");
    const payload = items
      .filter(it => selectedKeys.has(it.key))
      .map(it => ({ type: it.type, id: it.id }));
    status($("cloudStatus"), `Preparing zip of ${payload.length} item(s)...`, "");
    try {
      const endpoint = payload.length === 1 ? "/api/zip" : "/api/zip/bulk";
      const body = payload.length === 1 ? { type: payload[0].type, id: payload[0].id } : { items: payload };
      const data = await postJson(endpoint, body);
      if (!data.url) throw new Error("Zip URL was not returned");
      window.open(data.url, "_blank", "noopener,noreferrer");
      status($("cloudStatus"), "Zip link opened.", "ok");
    } catch (err) {
      status($("cloudStatus"), err.message || "Zip failed", "error");
    }
  }

  async function deleteSelected() {
    if (selectedKeys.size === 0) return toast("Select item(s) first");
    const payload = items
      .filter(it => selectedKeys.has(it.key))
      .map(it => ({ type: it.type, id: it.id }));
    const msg = payload.length === 1
      ? `Delete ${selected.type}: ${selected.name}?`
      : `Delete ${payload.length} items? This cannot be undone.`;
    if (!confirm(msg)) return;
    status($("cloudStatus"), `Deleting ${payload.length} item(s)...`, "");
    try {
      if (payload.length === 1) {
        await postJson("/api/delete", { type: payload[0].type, id: payload[0].id });
      } else {
        await postJson("/api/delete/bulk", { items: payload });
      }
      toast(`Deleted ${payload.length} item(s)`);
      await loadFolder(currentFolder);
    } catch (err) {
      status($("cloudStatus"), err.message || "Delete failed", "error");
    }
  }

  async function sendSelectedToTelegram() {
    if (selectedKeys.size === 0) return toast("Select a file first");
    
    const filesToSend = [];
    for (const key of selectedKeys) {
      const it = items.find(x => x.key === key);
      if (it && it.type === "file") {
        filesToSend.push(it);
      }
    }
    
    if (filesToSend.length === 0) {
      return toast("Select at least one file. Folders cannot be sent directly.");
    }
    
    // Check sizes
    for (const item of filesToSend) {
      if (item.size > 2097152000) {
        toast(`File "${item.name}" exceeds 1.95 GB limit.`);
        return status($("cloudStatus"), `File "${item.name}" exceeds 1.95 GB limit`, "error");
      }
    }
    
    status($("cloudStatus"), `Preparing transfer for ${filesToSend.length} file(s)...`, "");
    
    let successCount = 0;
    for (const item of filesToSend) {
      try {
        const data = await postJson("/api/telegram/send", { file_id: item.id });
        if (data.success) {
          successCount++;
          if (data.warning) {
            toast(`Warning: ${data.warning}`);
          }
        }
      } catch (err) {
        toast(`Failed to send "${item.name}": ${err.message || "Error"}`);
      }
    }
    
    if (successCount > 0) {
      toast(`Started upload for ${successCount} file(s)`);
      isTgTransferring = true;
      // Do NOT auto-open the Transfers overlay. Just start background polling so the
      // tab badge updates; the user opens the overlay themselves when they want it.
      if (typeof window.triggerQueuePolling === "function") {
        window.triggerQueuePolling();
      }
    }
  }


  let telegramPollTimer = null;
  let isTgTransferring = false;

  async function pollActiveTransfer() {
    if (telegramPollTimer) clearTimeout(telegramPollTimer);
    
    try {
      const response = await fetch("/api/transfer/status", { credentials: "same-origin", cache: "no-store" });
      if (response.ok) {
        const data = await response.json();
        
        if (data.status === "QUEUED" || data.status === "UPLOADING") {
          status($("cloudStatus"), "", "");
          telegramPollTimer = setTimeout(pollActiveTransfer, 10000);
        } else if (data.status === "COMPLETED") {
          status($("cloudStatus"), "", "");
          isTgTransferring = false;
        } else if (data.status === "FAILED") {
          status($("cloudStatus"), `Telegram upload failed: ${data.error || "unknown error"}`, "error");
          if (isTgTransferring) {
            toast("Upload failed");
            isTgTransferring = false;
          }
        }
      }
    } catch (err) {
      console.error("Error polling Telegram task status:", err);
      telegramPollTimer = setTimeout(pollActiveTransfer, 8000);
    }
  }

  async function openTelegramSettings() {
    $("telegramAuthOverlay").classList.remove("hidden");
    status($("tgAuthStatus"), "Checking status...", "");
    
    // Clear inputs
    $("tgPhone").value = "";
    $("tgCode").value = "";
    
    try {
      // Check auth status
      const authRes = await fetch("/api/telegram/status", { credentials: "same-origin" });
      if (authRes.ok) {
        const authData = await authRes.json();
        if (authData.authenticated) {
          $("tgUnlinkedStep").classList.add("hidden");
          $("tgLinkedStep").classList.remove("hidden");
        } else {
          $("tgUnlinkedStep").classList.remove("hidden");
          $("tgLinkedStep").classList.add("hidden");
          $("tgPhoneStep").classList.remove("hidden");
          $("tgCodeStep").classList.add("hidden");
        }
      }
      status($("tgAuthStatus"), "", "");
    } catch (err) {
      console.error("Error loading Telegram status:", err);
      status($("tgAuthStatus"), "Failed to load connection status", "error");
    }
  }

  function showTelegramAuthModal() {
    openTelegramSettings();
  }

  // History Management (Redis Backend)
  /* ===== Series Mode v2 + Normal grouped ===== */
  let seriesMode = false;
  // Holds the last rendered dataset so client-side sorting can re-order without re-fetching.
  let lastNormalGroups = null;   // [{quality,label,count,rows}]
  let lastSeriesData = null;     // {packs, encoders, ...}
  // True once the user clicks a column header; until then Series keeps its native
  // S/E order (Normal always uses size-asc default regardless).
  let userSorted = false;
  let activeNormalQuality = "";
  let activeSeriesQuality = "";
  const activeSeriesSeason = Object.create(null);

  function isMobileSearchUi() {
    return window.matchMedia && window.matchMedia("(max-width: 768px)").matches;
  }

  function qualityLabel(q) {
    return ({ "2160p": "4K", "1080p": "1080p", "720p": "720p", "Other": "Other" })[q] || q || "Other";
  }

  function qualityBucketFromName(name) {
    const m = String(name || "").match(/(?:^|[^0-9])(2160p|1080p|720p)(?:[^0-9]|$)/i);
    return m ? m[1].toLowerCase() : "Other";
  }

  function normalizeQualityList(list) {
    const order = ["2160p", "1080p", "720p", "Other"];
    const set = new Set((list || []).filter(Boolean));
    return order.filter(q => set.has(q));
  }

  function chooseActiveQuality(available, current) {
    const qs = normalizeQualityList(available);
    if (!qs.length) return "";
    if (current && qs.includes(current)) return current;
    const selected = getSelectedQualities();
    for (const q of selected) if (qs.includes(q)) return q;
    if (qs.includes("1080p")) return "1080p";
    return qs[0];
  }

  function mobileQualityNav(available, active, onPick) {
    const qs = normalizeQualityList(available);
    if (!qs.length) return null;
    const nav = document.createElement("div");
    nav.className = "mobile-quality-nav";
    for (const q of qs) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "mobile-quality-tab" + (q === active ? " active" : "");
      btn.textContent = qualityLabel(q);
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (btn.classList.contains("active")) return;
        nav.querySelectorAll(".mobile-quality-tab").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        onPick(q);
      });
      nav.appendChild(btn);
    }
    return nav;
  }

  function openSectionKeys(container) {
    if (!container) return new Set();
    return new Set(Array.from(container.querySelectorAll(":scope > .encoder-section:not(.collapsed)[data-acc-key]")).map(el => el.dataset.accKey));
  }

  function applyOpenState(section, key, openKeys) {
    section.dataset.accKey = key;
    if (openKeys && openKeys.has(key)) section.classList.remove("collapsed");
  }

  function getSelectedQualities() {
    const sel = isMobileSearchUi() ? ".mQualityOpt:checked" : ".qualityOpt:checked";
    const values = Array.from(document.querySelectorAll(sel)).map(c => c.value);
    return values.length ? values : Array.from(document.querySelectorAll(".qualityOpt:checked")).map(c => c.value);
  }
  function getSelectedEncoders() {
    const sel = isMobileSearchUi() ? ".mEncoderOpt:checked" : ".encoderOpt:checked";
    return Array.from(document.querySelectorAll(sel)).map(c => c.value);
  }

  function updateDropdownLabels() {
    const qs = getSelectedQualities();
    const qLabelMap = { "2160p": "4K", "1080p": "1080p", "720p": "720p" };
    const qBtn = $("qualityDdBtn");
    if (qBtn) qBtn.textContent = "Quality: " + (qs.length ? qs.map(x => qLabelMap[x] || x).join(", ") : "none");
    const es = getSelectedEncoders();
    const eBtn = $("encoderDdBtn");
    if (eBtn) eBtn.textContent = "Encoders: " + (es.length ? (es.length <= 2 ? es.join(", ") : es.length + " selected") : "none");
  }

  function setSeriesMode(on) {
    seriesMode = !!on;
    const nBtn = $("modeNormal"), sBtn = $("modeSeries");
    if (nBtn) nBtn.classList.toggle("active", !seriesMode);
    if (sBtn) sBtn.classList.toggle("active", seriesMode);
    // The control row (Quality/Encoder dropdowns) stays visible in BOTH modes.
    // Toggling only changes how the backend processes the next search.
    updateDropdownLabels();
    $("seriesResults").classList.add("hidden");
  }

  // ---- Client-side sort state (re-orders loaded rows; no re-fetch) ----
  function sortRows(rows) {
    const dir = currentOrder === "asc" ? 1 : -1;
    const key = currentSort;
    const val = (r) => {
      if (key === "seeders") return Number(r.seeds || 0);
      if (key === "size") return Number(r.size_bytes || 0);
      if (key === "date") return Date.parse(r.date || "") || 0;
      return 0;
    };
    return rows.slice().sort((a, b) => (val(a) - val(b)) * dir);
  }

  // Clickable header row for the sectioned views (Normal + Series).
  // Mirrors the desktop table columns: Name | SE(seeds) | Time | Size | Add.
    function seriesHeaderRow() {
    return document.createDocumentFragment();
  }

  function makeAccordion(section, header, container, groupSel) {
    header.addEventListener("click", (e) => {
      if (e.target.closest("button")) return; // ignore Add-all clicks
      const wasCollapsed = section.classList.contains("collapsed");
      container.querySelectorAll(":scope > " + groupSel).forEach((s) => s.classList.add("collapsed"));
      if (wasCollapsed) section.classList.remove("collapsed");
    });
  }

  function plainRow(row) {
    const wrap = document.createElement("div");
    wrap.className = "episode-row";

    const content = document.createElement("div");
    content.className = "row-content";

    const title = document.createElement("div");
    title.className = "row-title";
    title.textContent = row.name || "Untitled";
    title.title = row.name || "";

    const meta = document.createElement("div");
    meta.className = "row-meta";

    const seeds = Number(row.seeds || 0);
    const dotColor = seeds >= 50 ? "seed-green" : (seeds >= 10 ? "seed-amber" : "seed-red");
    const dot = document.createElement("span");
    dot.className = `seed-dot ${dotColor}`;
    dot.textContent = "●";

    const seedsText = document.createElement("span");
    seedsText.className = "meta-seeds";
    seedsText.textContent = `${seeds} seeds`;

    meta.append(dot, seedsText);

    function addSep() {
      const sep = document.createElement("span");
      sep.className = "meta-sep";
      sep.textContent = " · ";
      meta.appendChild(sep);
    }

    if (row.size && row.size !== "-") {
      addSep();
      const s = document.createElement("span");
      s.textContent = row.size;
      meta.appendChild(s);
    }
    if (row.encoder && row.encoder !== "-") {
      addSep();
      const e = document.createElement("span");
      e.textContent = row.encoder;
      meta.appendChild(e);
    }
    if (row.date && row.date !== "-") {
      addSep();
      const d = document.createElement("span");
      d.textContent = row.date;
      meta.appendChild(d);
    }

    content.append(title, meta);

    const action = document.createElement("div");
    action.className = "row-action";
    action.appendChild(makeAddButton(row));

    wrap.append(content, action);
    return wrap;
  }

  // Normal mode: render quality sections. On mobile, quality tabs navigate one
  // quality at a time; desktop keeps the existing accordion sections.
  function renderNormalGrouped(groups) {
    lastNormalGroups = groups || [];
    const container = $("seriesResults");
    const prevOpen = openSectionKeys(container);
    container.textContent = "";
    if (!lastNormalGroups.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No results.";
      container.appendChild(empty);
      return;
    }

    const fragment = document.createDocumentFragment();
    fragment.appendChild(seriesHeaderRow());

    const primaryGroups = lastNormalGroups.filter(g => g.quality !== "less_relevant");
    const lessGroup = lastNormalGroups.find(g => g.quality === "less_relevant");
    const available = primaryGroups.map(g => g.quality);
    activeNormalQuality = chooseActiveQuality(available, activeNormalQuality);
    const nav = mobileQualityNav(available, activeNormalQuality, (q) => {
      activeNormalQuality = q;
      setTimeout(() => {
        renderNormalGrouped(lastNormalGroups);
      }, 0);
    });
    if (nav) fragment.appendChild(nav);

    const active = primaryGroups.find(g => g.quality === activeNormalQuality) || primaryGroups[0];
    if (active) {
      for (const r of sortRows(active.rows || [])) {
        fragment.appendChild(plainRow(r));
      }
    }

    if (lessGroup && (lessGroup.rows || []).length) {
      const section = document.createElement("div");
      section.className = "encoder-section collapsed";
      applyOpenState(section, "normal:less_relevant", prevOpen);
      const header = sectionHeader({
        title: lessGroup.label || "Less relevant",
        sub: null,
        count: lessGroup.count + (lessGroup.count === 1 ? " result" : " results"),
      });
      const body = document.createElement("div");
      body.className = "encoder-body";
      for (const r of sortRows(lessGroup.rows || [])) {
        body.appendChild(plainRow(r));
      }
      section.append(header, body);
      fragment.appendChild(section);
    }

    container.appendChild(fragment);

    // Call accordion wiring after container has the elements
    const sections = container.querySelectorAll(".encoder-section");
    sections.forEach(sec => {
      const header = sec.querySelector(".encoder-header");
      if (header) {
        makeAccordion(sec, header, container, ".encoder-section");
      }
    });
  }

  function seriesEpisodeRow(row, labelParts) {
    const wrap = document.createElement("div");
    wrap.className = "episode-row";

    const content = document.createElement("div");
    content.className = "row-content";

    const title = document.createElement("div");
    title.className = "row-title";
    title.textContent = (labelParts || [row.name]).filter(Boolean).join(" · ") || row.name || "Untitled";
    title.title = row.name || "";

    const meta = document.createElement("div");
    meta.className = "row-meta";

    const seeds = Number(row.seeds || 0);
    const dotColor = seeds >= 50 ? "seed-green" : (seeds >= 10 ? "seed-amber" : "seed-red");
    const dot = document.createElement("span");
    dot.className = `seed-dot ${dotColor}`;
    dot.textContent = "●";

    const seedsText = document.createElement("span");
    seedsText.className = "meta-seeds";
    seedsText.textContent = `${seeds} seeds`;

    meta.append(dot, seedsText);

    function addSep() {
      const sep = document.createElement("span");
      sep.className = "meta-sep";
      sep.textContent = " · ";
      meta.appendChild(sep);
    }

    if (row.size && row.size !== "-") {
      addSep();
      const s = document.createElement("span");
      s.textContent = row.size;
      meta.appendChild(s);
    }
    if (row.encoder && row.encoder !== "-") {
      addSep();
      const e = document.createElement("span");
      e.textContent = row.encoder;
      meta.appendChild(e);
    }
    if (row.date && row.date !== "-") {
      addSep();
      const d = document.createElement("span");
      d.textContent = row.date;
      meta.appendChild(d);
    }

    content.append(title, meta);

    const action = document.createElement("div");
    action.className = "row-action";
    action.appendChild(makeAddButton(row));

    wrap.append(content, action);
    return wrap;
  }

  function sectionHeader(opts) {
    // opts: {title, sub, count}. Bulk Add-all buttons intentionally removed.
    const header = document.createElement("div");
    header.className = "encoder-header";
    const titleWrap = document.createElement("div");
    titleWrap.className = "encoder-title";
    const chevron = document.createElement("span");
    chevron.className = "chevron";
    chevron.textContent = "▼";
    const nameEl = document.createElement("span");
    nameEl.className = "encoder-name";
    nameEl.textContent = opts.title;
    titleWrap.append(chevron, nameEl);
    if (opts.sub) {
      const q = document.createElement("span");
      q.className = "encoder-quality";
      q.textContent = "— " + opts.sub;
      titleWrap.appendChild(q);
    }
    if (opts.count != null) {
      const countEl = document.createElement("span");
      countEl.className = "encoder-count";
      countEl.textContent = opts.count;
      titleWrap.appendChild(countEl);
    }
    header.appendChild(titleWrap);
    return header;
  }

  function renderSeriesGrouped(data) {
    lastSeriesData = data || null;
    const container = $("seriesResults");
    const prevOpen = openSectionKeys(container);
    container.textContent = "";
    if (!data) return;

    const packs = data.packs || [];
    const encoders = data.encoders || [];
    const lessRelevant = data.less_relevant || [];
    const otherRows = data.other || [];

    if (!packs.length && !encoders.length && !lessRelevant.length && !otherRows.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No grouped results. Try different quality/encoder selections.";
      container.appendChild(empty);
      return;
    }

    const fragment = document.createDocumentFragment();
    fragment.appendChild(seriesHeaderRow());

    const mobile = isMobileSearchUi();
    const available = [];
    for (const p of packs) available.push(qualityBucketFromName(p.name));
    for (const enc of encoders) for (const qg of (enc.qualities || [])) available.push(qg.quality);
    activeSeriesQuality = chooseActiveQuality(available, activeSeriesQuality);

    // Renders the global Quality chips on both desktop and mobile
    const nav = mobileQualityNav(available, activeSeriesQuality, (q) => {
      activeSeriesQuality = q;
      setTimeout(() => {
        renderSeriesGrouped(lastSeriesData);
      }, 0);
    });
    if (nav) fragment.appendChild(nav);

    // Both desktop and mobile now filter packs by the active quality chip
    const packsToShow = packs.filter(p => qualityBucketFromName(p.name) === activeSeriesQuality);
    if (packsToShow.length) {
      const section = document.createElement("div");
      section.className = "encoder-section packs collapsed";
      applyOpenState(section, mobile ? "packs" : "packs:all", prevOpen);
      const header = sectionHeader({
        title: "📦 Season Packs",
        sub: mobile ? null : "complete seasons · smallest first",
        count: packsToShow.length + (packsToShow.length === 1 ? " pack" : " packs"),
      });
      const body = document.createElement("div");
      body.className = "encoder-body";
      const displayPacks = userSorted ? sortRows(packsToShow) : packsToShow;
      for (const p of displayPacks) body.appendChild(seriesEpisodeRow(p, [p.name]));
      section.append(header, body);
      fragment.appendChild(section);
    }

    for (const enc of encoders) {
      // Both desktop and mobile now filter qualities by activeSeriesQuality
      const qualityGroups = (enc.qualities || []).filter(qg => qg.quality === activeSeriesQuality);
      if (!qualityGroups.length) continue;
      const visibleCount = qualityGroups.reduce((a, qg) => a + (qg.episode_count || 0), 0);
      if (!visibleCount) continue;

      const section = document.createElement("div");
      section.className = "encoder-section collapsed";
      applyOpenState(section, "enc:" + enc.encoder_norm, prevOpen);
      const header = sectionHeader({
        title: enc.name,
        sub: mobile ? null : qualityGroups.length + " quality group(s)",
        count: visibleCount + (visibleCount === 1 ? " episode" : " episodes"),
      });
      const body = document.createElement("div");
      body.className = "encoder-body";

      for (const qg of qualityGroups) {
        if (mobile) {
          const seasons = qg.seasons || [];
          if (seasons.length) {
            const skey = enc.encoder_norm + ":" + qg.quality;
            const availableSeasons = seasons.map(s => s.season);
            if (!activeSeriesSeason[skey] || !availableSeasons.includes(activeSeriesSeason[skey])) {
              activeSeriesSeason[skey] = availableSeasons[0];
            }
            const sNav = document.createElement("div");
            sNav.className = "mobile-season-nav";
            for (const season of availableSeasons) {
              const btn = document.createElement("button");
              btn.type = "button";
              btn.className = "mobile-season-tab" + (season === activeSeriesSeason[skey] ? " active" : "");
              btn.textContent = "S" + season;
              btn.addEventListener("click", (e) => {
                e.stopPropagation();
                activeSeriesSeason[skey] = season;
                renderSeriesGrouped(lastSeriesData);
              });
              sNav.appendChild(btn);
            }
            body.appendChild(sNav);
            const activeSeason = seasons.find(s => s.season === activeSeriesSeason[skey]) || seasons[0];
            const eps = activeSeason ? (activeSeason.episodes || []) : [];
            for (const ep of eps) body.appendChild(seriesEpisodeRow(ep, [ep.se, qg.label || qg.quality]));
          }
          continue;
        }

        // Desktop
        for (const s of (qg.seasons || [])) {
          const slabel = document.createElement("div");
          slabel.className = "season-label";
          slabel.textContent = "Season " + (s.season || "?");
          body.appendChild(slabel);
          const eps = s.episodes || [];
          for (const ep of eps) {
            body.appendChild(seriesEpisodeRow(ep, [ep.series, ep.se, qg.label || qg.quality]));
          }
        }
      }
      section.append(header, body);
      fragment.appendChild(section);
    }

    function appendPlainSeriesSection(key, title, rows) {
      if (!rows || !rows.length) return;
      const section = document.createElement("div");
      section.className = "encoder-section other collapsed";
      applyOpenState(section, key, prevOpen);
      const header = sectionHeader({
        title,
        sub: null,
        count: rows.length + (rows.length === 1 ? " result" : " results"),
      });
      const body = document.createElement("div");
      body.className = "encoder-body";
      for (const row of rows) body.appendChild(plainRow(row));
      section.append(header, body);
      fragment.appendChild(section);
    }

    appendPlainSeriesSection("series:less_relevant", "Less relevant", lessRelevant);
    appendPlainSeriesSection("series:other", "Other / Unparsed", otherRows);

    container.appendChild(fragment);

    // Call accordion wiring after container has the elements
    const sections = container.querySelectorAll(".encoder-section");
    sections.forEach(sec => {
      const header = sec.querySelector(".encoder-header");
      if (header) {
        makeAccordion(sec, header, container, ".encoder-section");
      }

      // Wire internal uploader-group accordion on desktop
      if (!mobile) {
        const uploaderGroups = sec.querySelectorAll(".uploader-group");
        uploaderGroups.forEach(ug => {
          const uLabel = ug.querySelector(".uploader-label");
          if (uLabel) {
            makeAccordion(ug, uLabel, ug.parentNode, ".uploader-group");
          }
        });
      }
    });
  }
  async function saveToHistory(magnet, title, size) {
    try {
      await postJson("/api/history/add", { magnet: magnet, name: title || "Unknown Magnet", size: size || "" });
    } catch (e) {
      console.warn("Failed to save history", e);
      // Optional: toast("History save failed: " + (e.message || "Unknown error"));
    }
  }

  async function renderHistory() {
    const tbody = $("historyBody");
    tbody.innerHTML = "<tr><td colspan='2' class='muted' style='text-align:center;'>Loading...</td></tr>";
    
    try {
      const data = await parseResponse(await fetch("/api/history", { credentials: "same-origin" }));
      const history = data.items || [];
      
      tbody.innerHTML = "";
      $("historyEmpty").classList.toggle("hidden", history.length > 0);
      
      history.forEach(item => {
        const tr = document.createElement("tr");
        
        const nameTd = document.createElement("td");
        nameTd.style.maxWidth = "0"; // allows truncate inside table-layout: fixed
        nameTd.style.width = "100%";
        const titleDiv = document.createElement("div");
        titleDiv.className = "truncate";
        titleDiv.style.fontWeight = "bold";
        titleDiv.textContent = item.title;
        nameTd.append(titleDiv);
        
        const sizeDiv = document.createElement("div");
        sizeDiv.className = "text-meta";
        sizeDiv.style.fontSize = "11px";
        sizeDiv.style.marginTop = "2px";
        sizeDiv.textContent = item.size ? `${item.size} · ${item.time}` : item.time;
        nameTd.append(sizeDiv);
        
        const actionTd = document.createElement("td");
        actionTd.style.textAlign = "right";
        const btnGroup = document.createElement("div");
        btnGroup.style.display = "inline-flex";
        btnGroup.style.gap = "4px";
        
        const copyBtn = document.createElement("button");
        copyBtn.className = "secondary hist-icon";
        copyBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-copy"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>`;
        copyBtn.title = "Copy magnet link";
        copyBtn.onclick = async () => {
          try {
            await navigator.clipboard.writeText(item.magnet);
            copyBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-check"><polyline points="20 6 9 17 4 12"/></svg>`;
            toast("Magnet copied");
            setTimeout(() => {
              copyBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-copy"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>`;
            }, 1500);
          } catch (e) {
            toast("Copy failed");
          }
        };

        const addBtn = document.createElement("button");
        addBtn.className = "hist-icon";
        addBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-plus"><path d="M5 12h14M12 5v14"/></svg>`;
        addBtn.title = "Add to Destination";
        addBtn.onclick = async () => {
          addBtn.disabled = true;
          addBtn.innerHTML = `<svg class="btn-spinner" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><circle cx="12" cy="12" r="10" stroke="rgba(255,255,255,0.2)"/><path d="M12 2a10 10 0 0 1 10 10" class="spin-path"/></svg>`;
          try {
            await postJson("/api/add", { magnet: item.magnet });
            toast("Added from history: " + item.title);
            await saveToHistory(item.magnet, item.title, item.size); // Update timestamp
            addBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-check"><polyline points="20 6 9 17 4 12"/></svg>`;
          } catch (e) {
            toast("Failed: " + e.message);
            addBtn.disabled = false;
            addBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-plus"><path d="M5 12h14M12 5v14"/></svg>`;
          }
        };
        
        const delBtn = document.createElement("button");
        delBtn.className = "danger ghost hist-icon";
        delBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-trash-2"><path d="M3 6h18M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2M10 11v6M14 11v6"/></svg>`;
        delBtn.title = "Remove from history";
        delBtn.onclick = async () => {
          delBtn.disabled = true;
          try {
            await postJson("/api/history/delete", { magnet: item.magnet });
            renderHistory();
          } catch(e) {
             toast("Failed to delete from history");
             delBtn.disabled = false;
          }
        };
        
        btnGroup.append(copyBtn, addBtn, delBtn);
        actionTd.appendChild(btnGroup);
        
        tr.append(nameTd, actionTd);
        tbody.appendChild(tr);
      });
    } catch(e) {
      tbody.innerHTML = "<tr><td colspan='2' class='error' style='text-align:center;'>Failed to load history</td></tr>";
    }
  }

  $("historyBtn").addEventListener("click", () => {
    if (typeof window.updateBottomNavHighlight === "function") window.updateBottomNavHighlight(2);
    renderHistory();
    $("historyOverlay").classList.remove("hidden");
  });

  $("closeHistoryBtn").addEventListener("click", () => {
    $("historyOverlay").classList.add("hidden");
    if (typeof window.restoreActiveMainTabHighlight === "function") window.restoreActiveMainTabHighlight();
  });

  $("clearHistoryBtn").addEventListener("click", async () => {
    if (confirm("Clear global magnet history?")) {
      await postJson("/api/history/clear", {});
      renderHistory();
    }
  });

(() => {
  let pollTimer = null;
  let isOverlayOpen = false;

  // Escape HTML so server/torrent-supplied values (e.g. filenames) can't inject
  // markup/script when inserted via innerHTML (XSS hardening).
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatBytes(bytes, decimals = 2) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const dm = decimals < 0 ? 0 : decimals;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
  }

  async function cancelTransfer(taskId) {
    if (!confirm("Are you sure you want to cancel this transfer?")) return;
    try {
      const res = await postJson("/api/telegram/cancel", { task_id: taskId });
      if (res.success) {
        toast(res.message || "Transfer cancelled successfully.");
        // Immediate refresh
        refreshQueueStatus();
      } else {
        toast(res.error || "Failed to cancel transfer.");
      }
    } catch (e) {
      toast(e.message || "Failed to cancel transfer.");
    }
  }

  function renderQueue(data) {
    // 1. Render Limit / Target
    const usage = Number(data.bandwidth_usage_gb || 0);
    const projected = Number(data.bandwidth_projected_gb || usage);
    const limit = Number(data.bandwidth_limit_gb || 4.5);
    
    let limitText = `${usage.toFixed(2)} GB / ${limit.toFixed(1)} GB`;
    if (projected > usage) {
      limitText = `${usage.toFixed(2)} GB (Proj: ${projected.toFixed(2)} GB) / ${limit.toFixed(1)} GB`;
    }
    $("tgTransfersLimitText").textContent = limitText;
    
    const pct = Math.min(100, (usage / limit) * 100);
    $("tgTransfersLimitBar").style.width = `${pct}%`;
    
    if (projected >= limit) {
      $("tgTransfersLimitBar").style.background = "#ef4444";
    } else if (projected >= 4.0) {
      $("tgTransfersLimitBar").style.background = "#f59e0b";
    } else {
      $("tgTransfersLimitBar").style.background = "var(--accent)";
    }
    
    $("tgTransfersTargetText").textContent = data.destination || "me";

    // 2. Render Active Transfer
    const activeCard = $("tgActiveTransferCard");
    if (data.active) {
      const active = data.active;
      const progress = active.progress !== undefined ? Number(active.progress).toFixed(1) : "0.0";
      const speed = active.speed_mb !== undefined ? `${active.speed_mb.toFixed(2)} MB/s` : "0.00 MB/s";
      // Escape all server-supplied values before interpolating into innerHTML.
      const fname = escapeHtml(active.filename || "file");
      const fstatus = escapeHtml(active.status || "UPLOADING");
      const ftask = escapeHtml(active.task_id || "");
      
      activeCard.innerHTML = `
        <div style="display: flex; justify-content: space-between; align-items: start; gap: 12px;">
          <div style="flex: 1; min-width: 0;">
            <strong style="display: block; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--text);" title="${fname}">${fname}</strong>
            <span class="muted" style="font-size: 12px;">Status: <span style="color: var(--accent); font-weight: 600;">${fstatus}</span></span>
          </div>
          <button class="tg-cancel-btn danger ghost" data-task-id="${ftask}" style="padding: 6px 12px; font-size: 12px;">Cancel</button>
        </div>
        <div style="width: 100%; height: 6px; background: var(--panel-1); border-radius: 3px; overflow: hidden; border: 1px solid var(--line);">
          <div style="width: ${progress}%; height: 100%; background: var(--accent); transition: width 0.3s;"></div>
        </div>
        <div style="display: flex; justify-content: space-between; font-size: 11px;" class="muted">
          <span>Progress: ${progress}% (${formatBytes(active.sent_bytes || 0)} / ${formatBytes(active.total_bytes || 0)})</span>
          <strong style="color: var(--accent);">${speed}</strong>
        </div>
      `;
    } else {
      activeCard.innerHTML = `<div class="empty" style="padding: 8px; margin: 0;">No active transfers running.</div>`;
    }

    // 3. Render Queue List
    const qBody = $("tgQueueBody");
    if (data.queue && data.queue.length > 0) {
      qBody.innerHTML = "";
      data.queue.forEach((item) => {
        const tr = document.createElement("tr");
        
        const nameTd = document.createElement("td");
        nameTd.style.cssText = "font-size: 13px; padding: 10px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;";
        nameTd.textContent = item.filename;
        nameTd.title = item.filename;
        
        const sizeTd = document.createElement("td");
        sizeTd.style.cssText = "width: 100px; font-size: 13px; padding: 10px; text-align: right;";
        sizeTd.textContent = formatBytes(item.total_bytes);
        
        const actionTd = document.createElement("td");
        actionTd.style.cssText = "width: 90px; font-size: 13px; padding: 10px; text-align: center;";
        
        const cancelBtn = document.createElement("button");
        cancelBtn.className = "danger ghost";
        cancelBtn.style.cssText = "padding: 4px 8px; font-size: 11px;";
        cancelBtn.textContent = "Cancel";
        cancelBtn.dataset.taskId = item.task_id;
        
        actionTd.appendChild(cancelBtn);
        tr.append(nameTd, sizeTd, actionTd);
        qBody.appendChild(tr);
      });
    } else {
      qBody.innerHTML = `<tr><td colspan="3" class="muted" style="text-align: center; padding: 20px; font-size: 13px;">No transfers in queue.</td></tr>`;
    }

    // 4. Update tab badge count
    const activeCount = (data.active && (data.active.status === "UPLOADING" || data.active.status === "QUEUED")) ? 1 : 0;
    const queueCount = data.queue ? data.queue.length : 0;
    const totalCount = activeCount + queueCount;
    
    const badge = $("tgBadge");
    if (badge) {
      if (totalCount > 0) {
        badge.textContent = totalCount;
        badge.classList.remove("hidden");
      } else {
        badge.classList.add("hidden");
      }
    }

    // Wire up cancel events
    document.querySelectorAll(".tg-cancel-btn, #tgQueueBody button").forEach((btn) => {
      btn.onclick = (e) => {
        const tid = e.target.dataset.taskId;
        if (tid) cancelTransfer(tid);
      };
    });
  }

  async function refreshQueueStatus() {
    try {
      const response = await fetch("/api/telegram/queue", { credentials: "same-origin" });
      if (response.ok) {
        const data = await response.json();
        renderQueue(data);
        
        // Keep polling if overlay is open OR if there's an active/queued transfer
        const hasWork = data.active || (data.queue && data.queue.length > 0);
        if (isOverlayOpen || hasWork) {
          if (pollTimer) clearTimeout(pollTimer);
          const interval = hasWork ? 10000 : 30000; // Poll every 10s if active, 30s if idle
          pollTimer = setTimeout(refreshQueueStatus, interval);
        }
      }
    } catch (e) {
      console.error("Error refreshing Telegram queue status:", e);
      if (isOverlayOpen) {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(refreshQueueStatus, 15000); // Poll every 15s on error if overlay open
      }
    }
  }

  // Hook Navigation button
  if ($("telegramTabBtn")) {
    $("telegramTabBtn").addEventListener("click", () => {
      if (typeof window.updateBottomNavHighlight === "function") window.updateBottomNavHighlight(3);
      isOverlayOpen = true;
      $("telegramTransfersOverlay").classList.remove("hidden");
      refreshQueueStatus();
    });
  }

  // Hook Close action
  if ($("closeTelegramTransfersBtn")) {
    $("closeTelegramTransfersBtn").addEventListener("click", () => {
      isOverlayOpen = false;
      $("telegramTransfersOverlay").classList.add("hidden");
      if (pollTimer) clearTimeout(pollTimer);
      if (typeof window.restoreActiveMainTabHighlight === "function") window.restoreActiveMainTabHighlight();
    });
  }

  // Expose triggers so external actions can start the polling loop
  window.triggerQueuePolling = function() {
    refreshQueueStatus();
  };
})();

  let suppressSuggestions = false;
  let searchAbort = null;

  function isMagnetLink(value) {
    const val = String(value || "").trim();
    if (/^\s*magnet:\?xt=urn:btih:/i.test(val)) return true;
    const lower = val.toLowerCase();
    if (lower.endsWith(".torrent") && (lower.startsWith("http://") || lower.startsWith("https://"))) return true;
    return false;
  }

  function magnetInfoHash(value) {
    const text = String(value || "");
    const m = text.match(/xt=urn:btih:([^&]+)/i);
    if (!m) return "";
    try { return decodeURIComponent(m[1]).trim().toLowerCase(); }
    catch (_) { return String(m[1] || "").trim().toLowerCase(); }
  }

  function setSearchAction(action) {
    const isAdd = action === "add";
    const searchBtn = $("searchBtn");
    const addBtn = $("addMagnetBtn");
    if (searchBtn && addBtn) {
      searchBtn.classList.toggle("hidden", isAdd);
      addBtn.classList.toggle("hidden", !isAdd);
    }
  }

  function setMagnetUiState(value) {
    const isMagnet = isMagnetLink(value);
    setSearchAction(isMagnet ? "add" : "search");
    return isMagnet;
  }

  function maybeAutoAddMagnet(value, source = "input") {
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

  async function ingestClipboardMagnet(autoAdd = true) {
    // Do not auto-detect/overwrite if the user already has text in the search box.
    if ($("searchQuery") && $("searchQuery").value.trim()) return false;
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
    let label = "via " + provider;
    if (data.provider_fallback === "unfiltered") label += " · unfiltered fallback";
    if (data.provider_fallback === "less_relevant") label += " · showing less relevant matches";
    if (data.provider_fallback === "other") label += " · showing other matches";
    if (!before.length) return label;
    const details = before.map(a => a.provider + (a.raw > 0 && a.filtered === 0 ? " filtered out" : " no results")).join(", ");
    return label + " after " + details;
  }

  async function search(keepPage, page) {
    const q = $("searchQuery").value.trim();
    if (!q) return status($("searchStatus"), "Enter a search query", "error");

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
      status($("searchStatus"), "Adding magnet to Seedr...", "");
      try {
        const res = await postJson("/api/add", { magnet: q });
        if (res && res.queued) {
          status($("searchStatus"), "\u2713 Added to Queue: " + magnetName, "ok");
          toast("Added to local queue: " + magnetName);
        } else {
          status($("searchStatus"), "\u2713 Added: " + magnetName, "ok");
          toast("Added to Seedr: " + magnetName);
        }
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
    const providerOrderText = (typeof seriesMode !== "undefined" && seriesMode)
      ? "apibay → bitsearch → torrents-csv"
      : "bitsearch → apibay → torrents-csv";
    status($("searchStatus"), "Searching providers: " + providerOrderText + "...", "");
    if ($("resultCount")) $("resultCount").textContent = "";

    const resultsContainer = $("seriesResults");
    if (resultsContainer) {
      resultsContainer.classList.remove("hidden");
      resultsContainer.textContent = "";
      resultsContainer.appendChild(seriesHeaderRow());
      
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
        if ($("resultCount")) $("resultCount").textContent = "";
        const providerText = providerStatusText(data);
        status($("searchStatus"), "Found " + packs + " pack(s) + " + eps + " episode(s)" + extra + " \u00b7 " + (data.requests_used || 0) + " request(s)" + (providerText ? " \u00b7 " + providerText : ""), "ok");
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
      if ($("resultCount")) $("resultCount").textContent = "";
      const providerText = providerStatusText(data);
      status($("searchStatus"), "Found " + total + " results" + (groupCount ? " across " + groupCount + " quality group" + (groupCount === 1 ? "" : "s") : "") + (providerText ? " · " + providerText : ""), "ok");
    } catch (err) {
      if (err && err.name === "AbortError") return; // superseded by a newer search
      if ($("resultCount")) $("resultCount").textContent = "";
      status($("searchStatus"), err.message || "Search failed", "error");
    }
  }

  function makeAddButton(result) {
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
          const res = await postJson("/api/add", { magnet: result.magnet, size: result.size_bytes || 0 });
          if (res && res.queued) {
            toast("Added to queue: " + (result.name || "torrent"));
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

  let fieldsWarned = false;

  async function getSuggestions() {
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

  window.updateBottomNavHighlight = function(index) {
    const highlight = $("bottomNavHighlight");
    if (!highlight) return;
    highlight.style.transform = `translateX(${index * 100}%)`;
    
    // Update active class on tab items
    const tabs = ["cloudTab", "searchTab", "historyBtn", "telegramTabBtn", "trailersTab"];
    tabs.forEach((id, idx) => {
      const btn = $(id);
      if (btn) btn.classList.toggle("active", idx === index);
    });
  };

  window.restoreActiveMainTabHighlight = function() {
    const isCloud = !$("cloudView").classList.contains("hidden");
    const isTrailers = !$("trailersView")?.classList.contains("hidden");
    if (isCloud) {
      window.updateBottomNavHighlight(0);
    } else if (isTrailers) {
      window.updateBottomNavHighlight(4);
    } else {
      window.updateBottomNavHighlight(1);
    }
  };

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
    const trailersView = $("trailersView");
    if (trailersView) trailersView.classList.add("hidden");
    
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
  $("trailersTab")?.addEventListener("click", () => {
    if (typeof window.setTrailersTab === "function") {
      window.setTrailersTab();
    }
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
  let filterSearchTimer = null;
  function debouncedFilterSearch() {
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
  function syncMobileFiltersFromDesktop() {
    document.querySelectorAll(".mQualityOpt").forEach((m) => {
      const d = document.querySelector(`.qualityOpt[value="${m.value}"]`);
      if (d) m.checked = d.checked;
    });
    document.querySelectorAll(".mEncoderOpt").forEach((m) => {
      const d = document.querySelector(`.encoderOpt[value="${m.value}"]`);
      if (d) m.checked = d.checked;
    });
  }
  function syncDesktopFiltersFromMobile() {
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
  function closeMobileFilters() {
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
  function openMobileFilters() {
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
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  async function openDevicesModal() {
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
      status($("tgAuthStatus"), "Unlinking account...", "");
      try {
        await postJson("/api/telegram/logout", {});
        status($("tgAuthStatus"), "Account unlinked!", "ok");
        toast("Telegram account unlinked.");
        $("tgLinkedStep").classList.add("hidden");
        $("tgUnlinkedStep").classList.remove("hidden");
        $("tgPhoneStep").classList.remove("hidden");
        $("tgCodeStep").classList.add("hidden");
      } catch (err) {
        status($("tgAuthStatus"), err.message || "Failed to unlink account", "error");
      }
    });
  }

  if ($("tgSendCodeBtn")) {
    $("tgSendCodeBtn").addEventListener("click", async () => {
      const phone = $("tgPhone").value.trim();
      if (!phone) return status($("tgAuthStatus"), "Enter your phone number", "error");
      status($("tgAuthStatus"), "Requesting code...", "");
      try {
        await postJson("/api/telegram/setup/send-code", { phone });
        status($("tgAuthStatus"), "Verification code sent to Telegram app", "ok");
        $("tgPhoneStep").classList.add("hidden");
        $("tgCodeStep").classList.remove("hidden");
        $("tgCode").focus();
      } catch (err) {
        status($("tgAuthStatus"), err.message || "Failed to send code", "error");
      }
    });
  }

  if ($("tgVerifyCodeBtn")) {
    $("tgVerifyCodeBtn").addEventListener("click", async () => {
      const code = $("tgCode").value.trim();
      if (!code) return status($("tgAuthStatus"), "Enter the verification code", "error");
      status($("tgAuthStatus"), "Verifying...", "");
      try {
        await postJson("/api/telegram/setup/verify-code", { code });
        status($("tgAuthStatus"), "Telegram successfully linked!", "ok");
        toast("Telegram account linked successfully!");
        setTimeout(() => {
          $("telegramAuthOverlay").classList.add("hidden");
          if (typeof sendSelectedToTelegram === "function") sendSelectedToTelegram();
        }, 1500);
      } catch (err) {
        status($("tgAuthStatus"), err.message || "Verification failed", "error");
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
  async function init() {
    try {
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const key = localStorage.key(i);
        if (key && key.startsWith("streamly:autoAddedMagnet:")) {
          localStorage.removeItem(key);
        }
      }
    } catch (_) {}

    let initialTab = window.location.hash.replace("#", "") || "search";
    if (initialTab !== "cloud" && initialTab !== "search" && initialTab !== "trailers") initialTab = "search";
    
    // Optimistically show header and search tab immediately
    showApp(null); 
    const hadUrlMagnet = typeof ingestUrlMagnet === "function" && ingestUrlMagnet();
    if (initialTab === "search") {
      setTab("search");
      if (!hadUrlMagnet && typeof ingestClipboardMagnet === "function") ingestClipboardMagnet(true);
    } else if (initialTab === "trailers") {
      if (typeof window.setTrailersTab === "function") {
        window.setTrailersTab();
      } else {
        setTab("search");
      }
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
        if (typeof pollActiveTransfer === "function") pollActiveTransfer();
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
      if (initialTab !== "trailers") {
        setTab("search");
      }
    }
  }

  init();

  // Enable instant touch active states on mobile (iOS/Android Safari/Chrome)
  document.addEventListener("touchstart", () => {}, { passive: true });
  // ============================ TRAILERS MODULE ============================
  // Standalone: expects #trailersView and #trailersContainer in the DOM.
  // Wire a tab button to call setTrailersTab() (see 6-main.js integration).

  const TRAILERS_API = "/api/trailers";
  const TRAILERS_STATUS_API = "/api/trailers/status";
  const TRAILERS_REFRESH_API = "/api/trailers/refresh";

  // Spinner CSS (injected once)
  if (!document.getElementById("trailerSpinnerStyle")) {
    const style = document.createElement("style");
    style.id = "trailerSpinnerStyle";
    style.textContent = "@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }";
    document.head.appendChild(style);
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function _trailers$(id) { return document.getElementById(id); }

  function _trailersFormatDate(iso) {
    const d = new Date(iso + "T00:00:00");
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  }

  function _trailersTimeAgo(ts) {
    if (!ts) return "Never";
    const diff = Math.floor((Date.now() - ts * 1000) / 60000);
    if (diff < 1) return "Just now";
    if (diff < 60) return `${diff}m ago`;
    if (diff < 1440) return `${Math.floor(diff / 60)}h ago`;
    return `${Math.floor(diff / 1440)}d ago`;
  }

  async function _trailersPostJson(url, body) {
    if (typeof postJson === "function") {
      return postJson(url, body);
    }
    const token = (document.querySelector('meta[name="csrf-token"]')?.content || window.csrfToken || "");
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
      credentials: "same-origin",
      body: JSON.stringify(body || {})
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(text || `${res.status}`);
    }
    return res.json();
  }

  async function _trailersFetch() {
    const res = await fetch(TRAILERS_API, { credentials: "same-origin" });
    if (!res.ok) throw new Error(`${res.status}`);
    return res.json();
  }

  async function _trailersFetchStatus() {
    try {
      const res = await fetch(TRAILERS_STATUS_API, { credentials: "same-origin" });
      if (!res.ok) return null;
      return res.json();
    } catch (e) { return null; }
  }

  function _trailersRenderBadge(video) {
    const type = video.type === "teaser" ? "Teaser" : (video.number > 0 ? `Trailer ${video.number}` : "Trailer");
    return `<span class="trailer-badge ${esc(video.type)}" data-vid="${esc(video.id)}">${esc(type)}</span>`;
  }

  function _trailersRenderCard(movie) {
    const main = movie.videos[0];
    if (!main) return "";
    const badges = movie.videos.map(_trailersRenderBadge).join("");
    return `
      <div class="trailer-card" data-title="${esc(movie.title)}">
        <div class="trailer-thumb" data-vid="${esc(main.id)}">
          <img src="${esc(main.thumbnail)}" alt="${esc(movie.title)}" loading="lazy" onerror="this.onerror=null;this.src='https://via.placeholder.com/480x270/161B22/8B949E?text=No+Thumbnail';">
          <div class="trailer-play">▶</div>
        </div>
        <div class="trailer-info">
          <div class="trailer-title" title="${esc(movie.title)}">${esc(movie.title)}</div>
          <div class="trailer-badges">${badges}</div>
          <div class="trailer-meta">
            <span class="trailer-channel">${esc(main.channel)}</span>
            <span class="trailer-when">${esc(_trailersFormatDate(main.published))}</span>
          </div>
        </div>
      </div>
    `;
  }

  function _trailersRender(data) {
    const container = _trailers$("trailersContainer");
    if (!container) return;

    if (!data || !data.items || !data.items.length) {
      container.innerHTML = `<div class="empty">No trailers in the last 30 days. The feed refreshes automatically every 10 minutes.</div>`;
      return;
    }

    const html = data.items.map(day => {
      const cards = day.items.map(_trailersRenderCard).join("");
      return `
        <div class="trailer-day">
          <h3 class="trailer-date">${_trailersFormatDate(day.date)}</h3>
          <div class="trailer-grid">${cards}</div>
        </div>
      `;
    }).join("");

    container.innerHTML = html;

    container.querySelectorAll(".trailer-thumb").forEach(el => {
      el.addEventListener("click", () => {
        const vid = el.dataset.vid;
        if (vid) openTrailerModal(vid, el.closest(".trailer-card")?.dataset.title || "");
      });
    });

    container.querySelectorAll(".trailer-badge").forEach(el => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        const vid = el.dataset.vid;
        if (vid) openTrailerModal(vid, el.closest(".trailer-card")?.dataset.title || "");
      });
    });
  }

  function openTrailerModal(videoId, title) {
    let ov = _trailers$("trailerModalOverlay");
    if (!ov) {
      ov = document.createElement("div");
      ov.id = "trailerModalOverlay";
      ov.className = "overlay";
      ov.innerHTML = `
        <div class="modal-panel" style="max-width: 900px; padding: 0; overflow: hidden;">
          <div class="panel-head" style="border-radius: 14px 14px 0 0;">
            <div style="flex:1; min-width:0;">
              <h2 id="trailerModalTitle" class="truncate" style="font-size:16px;">Trailer</h2>
            </div>
            <button id="closeTrailerModal" class="ghost" type="button" aria-label="Close trailer">✕</button>
          </div>
          <div class="panel-body" style="padding:0;">
            <div id="trailerEmbedContainer" class="trailer-embed-wrap" style="aspect-ratio:16/9; background:#000; position:relative; display:flex; align-items:center; justify-content:center;">
              <!-- iframe injected here dynamically -->
            </div>
          </div>
        </div>
      `;
      document.body.appendChild(ov);
      ov.addEventListener("click", (e) => { if (e.target === ov) closeTrailerModal(); });
      _trailers$("closeTrailerModal")?.addEventListener("click", closeTrailerModal);
    }
    const t = _trailers$("trailerModalTitle");
    if (t) t.textContent = title || "Trailer";

    const container = _trailers$("trailerEmbedContainer");
    if (container) container.innerHTML = "";

    // Show overlay FIRST, then create iframe after it is visible
    ov.classList.remove("hidden");

    requestAnimationFrame(() => {
      if (!container) return;

      // Create iframe dynamically after overlay is visible
      const iframe = document.createElement("iframe");
      iframe.style.cssText = "width:100%; height:100%; border:0; position:absolute; inset:0;";
      iframe.allow = "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture";
      iframe.allowFullscreen = true;
      iframe.src = `https://www.youtube-nocookie.com/embed/${esc(videoId)}?rel=0&modestbranding=1`;
      iframe.title = title || "YouTube video player";
      iframe.loading = "eager";
      container.appendChild(iframe);

      // Add fallback link in case iframe fails
      const fallback = document.createElement("a");
      fallback.href = `https://www.youtube.com/watch?v=${esc(videoId)}`;
      fallback.target = "_blank";
      fallback.rel = "noopener noreferrer";
      fallback.style.cssText = "position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#fff;text-decoration:none;font-size:14px;flex-direction:column;gap:8px;z-index:10;opacity:0;transition:opacity 0.3s;background:rgba(0,0,0,0.6);";
      fallback.innerHTML = `<span style="font-size:24px;">▶</span><span>Watch on YouTube</span>`;
      fallback.addEventListener("mouseenter", () => { fallback.style.opacity = "1"; });
      fallback.addEventListener("mouseleave", () => { fallback.style.opacity = "0"; });
      // Also show on iframe error
      iframe.addEventListener("error", () => { fallback.style.opacity = "1"; });
      container.appendChild(fallback);
    });
  }

  function closeTrailerModal() {
    const ov = _trailers$("trailerModalOverlay");
    if (!ov) return;
    ov.classList.add("hidden");
    const container = _trailers$("trailerEmbedContainer");
    if (container) container.innerHTML = "";
  }

  // --- Refresh / status ---

  function _trailersEnsureHeader() {
    let header = _trailers$("trailerHeader");
    if (!header) {
      const view = _trailers$("trailersView");
      if (!view) return;
      header = document.createElement("div");
      header.id = "trailerHeader";
      header.style.cssText = "display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid rgba(255,255,255,0.08);";
      header.innerHTML = `
        <div>
          <h2 style="font-size:16px;font-weight:700;margin:0;color:#f0f6fc;">Latest Trailers</h2>
          <div id="trailerStatusText" style="font-size:11px;color:#8b949e;margin-top:2px;">Loading status…</div>
        </div>
        <button id="trailersRefreshBtn" class="ghost" type="button" aria-label="Refresh trailers" style="display:flex;align-items:center;gap:6px;">
          <svg id="trailersRefreshIcon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-refresh-cw"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M3 21v-5h5"/></svg>
          <span>Refresh</span>
        </button>
      `;
      view.insertBefore(header, view.firstChild);
      _trailers$("trailersRefreshBtn")?.addEventListener("click", refreshTrailers);
    }
  }

  async function _trailersUpdateStatusText() {
    const statusText = _trailers$("trailerStatusText");
    if (!statusText) return;
    const status = await _trailersFetchStatus();
    if (!status) {
      statusText.textContent = "Status unavailable";
      return;
    }
    if (status.running) {
      statusText.textContent = "Checking for new trailers…";
      return;
    }
    const ago = status.last_crawl ? _trailersTimeAgo(status.last_crawl) : "Never";
    statusText.textContent = `Last updated ${ago}`;
  }

  async function refreshTrailers() {
    const btn = _trailers$("trailersRefreshBtn");
    const icon = _trailers$("trailersRefreshIcon");
    const container = _trailers$("trailersContainer");
    const statusText = _trailers$("trailerStatusText");

    if (btn) btn.disabled = true;
    if (icon) icon.style.animation = "spin 1s linear infinite";
    if (container) container.innerHTML = `<div class="status">Checking for new trailers… This may take up to 2 minutes.</div>`;

    try {
      const data = await _trailersPostJson(TRAILERS_REFRESH_API, {});

      if (data.status === "started" || data.status === "running") {
        if (statusText) statusText.textContent = "Checking for new trailers…";

        let attempts = 0;
        const poll = setInterval(async () => {
          attempts++;
          try {
            const status = await _trailersFetchStatus();
            if (status && !status.running && status.last_crawl) {
              const feed = await _trailersFetch();
              if (feed.items && feed.items.length > 0) {
                clearInterval(poll);
                _trailersRender(feed);
                if (icon) icon.style.animation = "";
                if (btn) btn.disabled = false;
                if (statusText) statusText.textContent = `Last updated ${_trailersTimeAgo(status.last_crawl)}`;
                return;
              }
            }
            const feed = await _trailersFetch();
            if (feed.items && feed.items.length > 0) {
              clearInterval(poll);
              _trailersRender(feed);
              if (icon) icon.style.animation = "";
              if (btn) btn.disabled = false;
              if (statusText) statusText.textContent = `Last updated ${_trailersTimeAgo(status.last_crawl || Date.now()/1000)}`;
              return;
            }
          } catch (e) {}

          if (attempts >= 24) { // 2 minutes
            clearInterval(poll);
            if (container) container.innerHTML = `<div class="status">Refresh timed out. The feed updates automatically every 10 minutes. Please check back later.</div>`;
            if (icon) icon.style.animation = "";
            if (btn) btn.disabled = false;
            _trailersUpdateStatusText();
          }
        }, 5000);
      } else {
        if (container) container.innerHTML = `<div class="status">Refresh failed: ${esc(data.message)}</div>`;
        if (icon) icon.style.animation = "";
        if (btn) btn.disabled = false;
      }
    } catch (e) {
      if (container) container.innerHTML = `<div class="status">Refresh failed: ${esc(e.message)}</div>`;
      if (icon) icon.style.animation = "";
      if (btn) btn.disabled = false;
    }
  }

  async function loadTrailers() {
    const container = _trailers$("trailersContainer");
    if (!container) return;
    container.innerHTML = `<div class="status">Loading latest trailers…</div>`;
    try {
      const [data, status] = await Promise.all([_trailersFetch(), _trailersFetchStatus()]);
      _trailersRender(data);
      const statusText = _trailers$("trailerStatusText");
      if (statusText && status) {
        if (status.running) {
          statusText.textContent = "Checking for new trailers…";
        } else {
          statusText.textContent = `Last updated ${status.last_crawl ? _trailersTimeAgo(status.last_crawl) : "Never"}`;
        }
      }
    } catch (e) {
      container.innerHTML = `<div class="status">Failed to load trailers. <button class="ghost" onclick="loadTrailers()">Retry</button></div>`;
    }
  }

  // Tab helper (call from 6-main.js tab switcher)
  function setTrailersTab() {
    const view = _trailers$("trailersView");
    const cloud = _trailers$("cloudView");
    const search = _trailers$("searchView");
    if (view) view.classList.remove("hidden");
    if (cloud) cloud.classList.add("hidden");
    if (search) search.classList.add("hidden");
    if (typeof window.updateBottomNavHighlight === "function") {
      window.updateBottomNavHighlight(4);
    }
    _trailersEnsureHeader();
    loadTrailers();
  }

  // Expose
  window.loadTrailers = loadTrailers;
  window.setTrailersTab = setTrailersTab;
  window.openTrailerModal = openTrailerModal;
  window.closeTrailerModal = closeTrailerModal;
  window.refreshTrailers = refreshTrailers;
})();
