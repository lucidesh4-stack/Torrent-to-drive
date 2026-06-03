
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
  const CLOUD_TRANSFER_REFRESH_MS = 5000;
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
  const AUTO_ADD_MAGNET_TTL_MS = 24 * 60 * 60 * 1000;
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
    if (accountLabel) accountLabel.textContent = username ? `Connected to ${username}` : "Guest Mode";
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

  let storageSnapshotLoading = false;
  let storageSnapshotLoaded = false;

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
    const telegramBtn = $("telegramBtn");
    if (telegramBtn) telegramBtn.disabled = count !== 1 || (selected && selected.type === "folder");

    // ----- Mobile selection sync -----
    document.querySelectorAll("#cloudMobileList .cm-row").forEach((row) => {
      row.classList.toggle("sel", selectedKeys.has(row.dataset.key));
    });
    const bulk = $("cloudBulkBar");
    if (bulk) {
      bulk.classList.toggle("hidden", count === 0);
      const bc = $("cmBulkCount");
      if (bc) bc.textContent = `${count} selected`;
    }
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
    cancelBtn.className = "danger transfer-cancel-btn";
    cancelBtn.textContent = "Cancel";
    cancelBtn.addEventListener("click", () => cancelTransfer(t));
    dateTd.appendChild(cancelBtn);
    tr.append(iconTd, nameTd, typeTd, sizeTd, dateTd);
    return tr;
  }

  function syncCloudAutoRefresh() {
    clearTimeout(cloudAutoRefreshTimer);
    cloudAutoRefreshTimer = null;
    const cloudVisible = $("cloudView") && !$("cloudView").classList.contains("hidden");
    if (isAuthenticated && cloudVisible && transfers.length > 0) {
      cloudAutoRefreshTimer = setTimeout(() => loadFolder(currentFolder || 0, { silent: true }), CLOUD_TRANSFER_REFRESH_MS);
    }
  }

  function renderCloud() {
    const body = $("cloudBody");
    body.textContent = "";
    const pathLabel = $("pathLabel");
    if (pathLabel) pathLabel.textContent = `Folder ID: ${currentFolder}`;
    $("upBtn").disabled = currentFolder === 0;
    $("cloudEmpty").classList.toggle("hidden", items.length + transfers.length !== 0);
    selectedKeys.clear();
    lastClickedKey = null;
    updateSelection();

    for (const t of transfers) body.appendChild(renderTransferRow(t));

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

    renderCloudMobile();
  }

  let cmTapTimer = null; // distinguishes single-tap (select) from double-tap (open)

  function renderCloudMobile() {
    const list = $("cloudMobileList");
    if (!list) return;
    list.textContent = "";
    const cnt = $("cmCount");
    if (cnt) cnt.textContent = `${items.length} item${items.length === 1 ? "" : "s"}` + (transfers.length ? ` · ${transfers.length} loading` : "");
    const empty = $("cloudMobileEmpty");
    if (empty) empty.classList.toggle("hidden", items.length + transfers.length !== 0);
    $("cmUpBtn").disabled = currentFolder === 0;

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
      cancel.className = "danger cm-transfer-cancel";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", (e) => { e.stopPropagation(); cancelTransfer(t); });
      info.append(fn, transferBar(t), meta, cancel);
      row.append(ic, info);
      list.appendChild(row);
    }

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

      const kebab = document.createElement("button");
      kebab.type = "button";
      kebab.className = "cm-kebab";
      kebab.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-more-vertical"><circle cx="12" cy="12" r="1"/><circle cx="12" cy="5" r="1"/><circle cx="12" cy="19" r="1"/></svg>`;
      kebab.addEventListener("click", (e) => {
        e.stopPropagation();
        openCtxMenu(item, kebab);
      });

      row.append(tick, ic, info, kebab);

      // tap = select/unselect ; double-tap = open
      row.addEventListener("click", (e) => {
        if (e.target.closest(".cm-kebab")) return;
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
  }

  let ctxItem = null;
  function openCtxMenu(item, anchor) {
    ctxItem = item;
    const menu = $("cloudCtxMenu");
    if (!menu) return;
    menu.classList.remove("hidden");
    const r = anchor.getBoundingClientRect();
    const mw = 210;
    let left = r.right - mw + window.scrollX;
    if (left < 8) left = 8;
    menu.style.left = left + "px";
    menu.style.top = (r.bottom + 6 + window.scrollY) + "px";
  }
  function closeCtxMenu() {
    const menu = $("cloudCtxMenu");
    if (menu) menu.classList.add("hidden");
    ctxItem = null;
  }

  async function ctxAction(act) {
    const item = ctxItem;
    closeCtxMenu();
    if (!item) return;
    // operate on this single item
    selectedKeys.clear();
    selectedKeys.add(item.key);
    lastClickedKey = item.key;
    updateSelection();
    if (act === "download") return downloadSelected();
    if (act === "delete") return deleteSelected();
    if (act === "telegram") return sendSelectedToTelegram();
    if (act === "copy") {
      try {
        if (item.type !== "file") {
          // folder: produce a zip link to copy
          const data = await postJson("/api/zip", { type: item.type, id: item.id });
          if (!data.url) throw new Error("No link");
          await navigator.clipboard.writeText(data.url);
        } else {
          const url = await getFileUrl(item);
          await navigator.clipboard.writeText(url);
        }
        toast("Link copied to clipboard");
      } catch (err) {
        toast(err.message || "Could not copy link");
      }
    }
  }

  function updateStorage(used, max) {
    storageSnapshotLoaded = true;
    const pct = max > 0 ? Math.min(100, Math.max(0, (used / max) * 100)) : 0;
    const label = `${bytes(used)} / ${bytes(max)} used (${pct.toFixed(1)}%)`;
    const compactLabel = `${bytes(used)} / ${bytes(max)} · ${pct.toFixed(1)}%`;

    const storageMeter = $("storageMeter");
    const storageText = $("storageText");
    if (storageMeter) storageMeter.style.width = pct.toFixed(1) + "%";
    if (storageText) storageText.textContent = label;

    const topMeter = $("topStorageMeter");
    const topText = $("topStorageText");
    if (topMeter) topMeter.style.width = pct.toFixed(1) + "%";
    if (topText) topText.textContent = label;

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
      const data = await parseResponse(await fetch("/fs/folder/0/items", { credentials: "same-origin" }));
      updateStorage(data.used || 0, data.max || 1);
    } catch (_) {
      // Silent by design: topbar storage should not interrupt Search/Guest flows.
    } finally {
      storageSnapshotLoading = false;
    }
  }

  async function loadFolder(id, opts = {}) {
    const silent = !!(opts && opts.silent);
    if (!silent) status($("cloudStatus"), "Loading folder...", "");
    try {
      const data = await parseResponse(await fetch(`/fs/folder/${encodeURIComponent(id)}/items`, { credentials: "same-origin" }));
      currentFolder = Number(id);
      parentFolder = Number(data.parent || 0);
      items = [];
      transfers = [];
      for (const transfer of data.transfers || []) transfers.push({ ...transfer, type: "transfer", key: `transfer:${transfer.id}` });
      for (const folder of data.folders || []) items.push({ ...folder, type: "folder", key: `folder:${folder.id}` });
      for (const file of data.files || []) items.push({ ...file, type: "file", key: `file:${file.id}` });
      updateStorage(data.used || 0, data.max || 1);
      renderCloud();
      if (!silent) status($("cloudStatus"), `Loaded ${items.length} item(s)` + (transfers.length ? ` · ${transfers.length} loading` : "") + ".", "ok");
      syncCloudAutoRefresh();
    } catch (err) {
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
    if (item.type === "folder") return loadFolder(item.id);
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
    refreshSelectedShim();
    const item = selected;
    if (!item) return toast("Select a file first");
    if (item.type === "folder") return toast("Folders cannot be sent to Telegram directly; download them as a zip first.");
    if (item.size >= 2 * 1024 * 1024 * 1024) {
      toast("Telegram uploads are capped at 2 GB.");
      return status($("cloudStatus"), "File exceeds 2 GB limit", "error");
    }
    
    status($("cloudStatus"), "Preparing Telegram transfer...", "");
    
    try {
      const data = await postJson("/api/telegram/send", { file_id: item.id });
      if (data.success) {
        toast("Telegram transfer started!");
        if (data.warning) {
          toast(`Warning: ${data.warning}`);
          status($("cloudStatus"), `Warning: ${data.warning}`, "error");
        }
        pollActiveTransfer();
        if (typeof window.triggerQueuePolling === "function") {
          window.triggerQueuePolling();
        }
      }
    } catch (err) {
      if ((err.message || "").includes("Telegram is not authenticated") || (err.message || "").includes("telegram_not_authenticated")) {
        status($("cloudStatus"), "Telegram authentication required", "error");
        showTelegramAuthModal();
      } else {
        toast(err.message || "Failed to send to Telegram");
        status($("cloudStatus"), err.message || "Telegram transfer failed", "error");
      }
    }
  }

  let telegramPollTimer = null;

  async function pollActiveTransfer() {
    if (telegramPollTimer) clearTimeout(telegramPollTimer);
    
    try {
      const response = await fetch("/api/transfer/status", { credentials: "same-origin" });
      if (response.ok) {
        const data = await response.json();
        
        if (data.status === "QUEUED" || data.status === "UPLOADING") {
          status($("cloudStatus"), "", "");
          telegramPollTimer = setTimeout(pollActiveTransfer, 5000);
        } else if (data.status === "COMPLETED") {
          status($("cloudStatus"), "", "");
          toast(`Sent to Telegram: ${data.filename}`);
        } else if (data.status === "FAILED") {
          status($("cloudStatus"), `Telegram upload failed: ${data.error || "unknown error"}`, "error");
          toast(`Telegram upload failed: ${data.error || "unknown error"}`);
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

  function syncSortControls() {
    for (const key of ["seeders", "leechers", "date", "size"]) {
      const mark = $("sortMark-" + key);
      if (mark) mark.textContent = key === currentSort ? (currentOrder === "desc" ? "\u25BC" : "\u25B2") : "";
    }
  }

  async function getSuggestions() {
    const q = $("searchQuery").value.trim();
    const box = $("suggestBox");
    clearTimeout(suggestTimer);
    
    // Don't suggest if it looks like a magnet link
    if (q.length < 3 || /^magnet:\?xt=urn:btih:/i.test(q)) {
      box.classList.add("hidden");
      box.textContent = "";
      return;
    }
    suggestTimer = setTimeout(async () => {
      try {
        // Race guard: skip if user kept typing after this timer fired
        if ($("searchQuery").value.trim() !== q) return;
        const data = await parseResponse(await fetch("/api/suggest?q=" + encodeURIComponent(q), { credentials: "same-origin" }));
        box.textContent = "";
        const rows = Array.isArray(data) ? data : [];
        if (!rows.length) {
          box.classList.add("hidden");
          return;
        }
        for (const item of rows) {
          const row = document.createElement("div");
          row.className = "suggest-item";
          const img = document.createElement("img");
          img.className = "suggest-poster";
          img.alt = "";
          img.referrerPolicy = "no-referrer";
          img.src = item.poster || "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='42' height='58'%3E%3Crect width='100%25' height='100%25' fill='%23111827'/%3E%3Ctext x='50%25' y='52%25' fill='%2394a3b8' text-anchor='middle' font-size='16'%3E?%3C/text%3E%3C/svg%3E";
          img.onerror = () => { img.removeAttribute("src"); };
          const meta = document.createElement("div");
          const title = document.createElement("div");
          title.className = "suggest-title";
          title.textContent = item.title || "Untitled";
          const year = document.createElement("div");
          year.className = "muted";
          year.textContent = item.year || "N/A";
          meta.append(title, year);
          row.append(img, meta);
          row.addEventListener("click", () => {
            $("searchQuery").value = item.title || "";
            $("suggestBox").classList.add("hidden");
            $("searchQuery").focus();
            // Do NOT auto-search — user clicks Search button to spend a quota hit
          });
          box.appendChild(row);
        }
        box.classList.remove("hidden");
      } catch (_) {
        box.classList.add("hidden");
      }
    }, 350);
  }

  function cycleSort(field) {
    if (currentSort === field) {
      currentOrder = currentOrder === "desc" ? "asc" : "desc";
    } else {
      currentSort = field;
      currentOrder = "desc";
    }
    syncSortControls();
    if (typeof userSorted !== "undefined") userSorted = true;
    // Client-side only: re-order the already-loaded results (no new bitsearch call).
    if (typeof seriesMode !== "undefined" && seriesMode) {
      if (typeof lastSeriesData !== "undefined" && lastSeriesData) renderSeriesGrouped(lastSeriesData);
    } else if (typeof lastNormalGroups !== "undefined" && lastNormalGroups) {
      renderNormalGrouped(lastNormalGroups);
    }
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
    const head = document.createElement("div");
    head.className = "sec-head";
    const cols = [
      { label: "Name", key: null, cls: "h-name" },
      { label: "Encoder", key: null, cls: "h-encoder" },
      { label: "SE", key: "seeders", cls: "h-se" },
      { label: "Time", key: "date", cls: "h-time" },
      { label: "Size", key: "size", cls: "h-size" },
      { label: "+", key: null, cls: "h-add" },
    ];
    for (const c of cols) {
      const el = document.createElement("span");
      el.className = "sec-h " + c.cls + (c.key ? " sortable" : "");
      const mark = c.key && currentSort === c.key ? (currentOrder === "desc" ? " \u25BC" : " \u25B2") : "";
      el.textContent = c.label + mark;
      if (c.key) el.addEventListener("click", (e) => { e.stopPropagation(); cycleSort(c.key); });
      head.appendChild(el);
    }
    return head;
  }

  // Accordion: clicking a section header closes its siblings and toggles itself.
  // `groupSel` scopes "siblings" (e.g. only sections in the same container, or
  // only uploaders within the same encoder body).
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
    const name = document.createElement("span");
    name.className = "name truncate";
    name.textContent = row.name || "Untitled";
    name.title = row.name || "";
    
    const encoder = document.createElement("span");
    encoder.className = "encoder truncate";
    encoder.textContent = row.encoder || "-";
    encoder.title = row.encoder || "";
    
    const se = document.createElement("span"); se.className = "se"; se.textContent = row.seeds || 0;
    const time = document.createElement("span");
    time.className = "time";
    time.textContent = row.date || "-";
    if (!row.date || row.date === "-") {
      time.classList.add("hidden");
    }
    const size = document.createElement("span"); size.className = "size"; size.textContent = row.size || "-";
    const add = document.createElement("span"); add.className = "add"; add.appendChild(makeAddButton(row));
    wrap.append(name, encoder, se, time, size, add);
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
    syncSortControls();

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

    const name = document.createElement("span");
    name.className = "name truncate";
    name.textContent = (labelParts || [row.name]).filter(Boolean).join(" · ");
    name.title = row.name || "";

    const encoder = document.createElement("span");
    encoder.className = "encoder truncate";
    encoder.textContent = row.encoder || "-";
    encoder.title = row.encoder || "";

    const se = document.createElement("span");
    se.className = "se";
    se.textContent = row.seeds || 0;

    const time = document.createElement("span");
    time.className = "time";
    time.textContent = row.date || "-";
    if (!row.date || row.date === "-") {
      time.classList.add("hidden");
    }

    const size = document.createElement("span");
    size.className = "size";
    size.textContent = row.size || "-";

    const add = document.createElement("span");
    add.className = "add";
    add.appendChild(makeAddButton(row));

    wrap.append(name, encoder, se, time, size, add);
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

    syncSortControls();

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
          const title = document.createElement("div");
          title.className = "mobile-encoder-title";
          const badge = document.createElement("span");
          badge.className = "encoder-count";
          badge.textContent = qg.label || qualityLabel(qg.quality);
          title.append(badge);
          body.appendChild(title);

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
            for (const ep of eps) body.appendChild(seriesEpisodeRow(ep, [ep.se, qg.label || qg.quality]));
          }
          continue;
        }

        // Desktop
        const qGroup = document.createElement("div");
        qGroup.className = "uploader-group"; // Expanded by default
        const qlabel = document.createElement("div");
        qlabel.className = "uploader-label";
        const chev = document.createElement("span");
        chev.className = "u-chevron";
        chev.textContent = "▼";
        const txt = document.createElement("span");
        txt.style.flex = "1";
        txt.style.minWidth = "0";
        txt.textContent = (qg.label || qg.quality) + " (" + qg.episode_count + ")";
        qlabel.append(chev, txt);
        qGroup.appendChild(qlabel);
        const qBody = document.createElement("div");
        qBody.className = "uploader-body";

        for (const s of qg.seasons) {
          const slabel = document.createElement("div");
          slabel.className = "season-label";
          slabel.textContent = "Season " + (s.season || "?");
          qBody.appendChild(slabel);
          const eps = s.episodes;
          for (const ep of eps) {
            qBody.appendChild(seriesEpisodeRow(ep, [ep.series, ep.se, qg.label || qg.quality]));
          }
        }
        qGroup.appendChild(qBody);
        body.appendChild(qGroup);
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
  async function saveToHistory(magnet, title) {
    try {
      await postJson("/api/history/add", { magnet: magnet, name: title || "Unknown Magnet" });
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
            await saveToHistory(item.magnet, item.title); // Update timestamp
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
    const limit = Number(data.bandwidth_limit_gb || 99.0);
    $("tgTransfersLimitText").textContent = `${usage.toFixed(2)} GB / ${limit.toFixed(1)} GB`;
    
    const pct = Math.min(100, (usage / limit) * 100);
    $("tgTransfersLimitBar").style.width = `${pct}%`;
    $("tgTransfersTargetText").textContent = data.destination || "me";

    // 2. Render Active Transfer
    const activeCard = $("tgActiveTransferCard");
    if (data.active) {
      const active = data.active;
      const progress = active.progress !== undefined ? Number(active.progress).toFixed(1) : "0.0";
      const speed = active.speed_mb !== undefined ? `${active.speed_mb.toFixed(2)} MB/s` : "0.00 MB/s";
      
      activeCard.innerHTML = `
        <div style="display: flex; justify-content: space-between; align-items: start; gap: 12px;">
          <div style="flex: 1; min-width: 0;">
            <strong style="display: block; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--text);" title="${active.filename || 'file'}">${active.filename || 'file'}</strong>
            <span class="muted" style="font-size: 12px;">Status: <span style="color: var(--accent); font-weight: 600;">${active.status || 'UPLOADING'}</span></span>
          </div>
          <button class="tg-cancel-btn danger ghost" data-task-id="${active.task_id}" style="padding: 6px 12px; font-size: 12px;">Cancel</button>
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
          pollTimer = setTimeout(refreshQueueStatus, 5000);
        }
      }
    } catch (e) {
      console.error("Error refreshing Telegram queue status:", e);
      if (isOverlayOpen) {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(refreshQueueStatus, 8000);
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

    if (source === "clipboard" || source === "url") {
      $("searchQuery").value = magnet;
      setMagnetUiState(magnet);
      toast("Magnet detected! Tap [+] to add to Seedr.");
      return true;
    }

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
      const data = await parseResponse(await fetch("/api/search?" + params.toString(), { credentials: "same-origin" }));

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
        saveToHistory(result.magnet, result.name);
        try {
          await postJson("/api/add", { magnet: result.magnet, size: result.size_bytes || 0 });
          toast("Added to Seedr: " + (result.name || "torrent"));
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
    window.updateBottomNavHighlight(isCloud ? 0 : 1);
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

  $("cloudTab").addEventListener("click", () => setTab("cloud"));
  $("searchTab").addEventListener("click", async () => {
    await setTab("search");
    if (typeof scheduleClipboardMagnetCheck === "function") scheduleClipboardMagnetCheck("tab");
  });
  $("refreshBtn").addEventListener("click", () => loadFolder(currentFolder));
  $("upBtn").addEventListener("click", () => { if (currentFolder !== 0) loadFolder(parentFolder || 0); });
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
  document.querySelectorAll(".qualityOpt, .encoderOpt").forEach((el) =>
    el.addEventListener("change", () => {
      if (typeof updateDropdownLabels === "function") updateDropdownLabels();
      if (typeof search === "function" && $("searchQuery").value.trim()) {
        search(false, 1);
      }
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
      $("searchBtn").classList.remove("hidden");
      $("addMagnetBtn").classList.add("hidden");
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
  $("searchQuery").addEventListener("keydown", (e) => { if (e.key === "Enter") search(false, 1); });
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

  window.addEventListener("focus", () => {
    if (typeof scheduleClipboardMagnetCheck === "function") scheduleClipboardMagnetCheck("focus");
  });

  // Initialization Sequence
  async function init() {
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
      setTab("search");
    }
  }

  init();

  // Enable instant touch active states on mobile (iOS/Android Safari/Chrome)
  document.addEventListener("touchstart", () => {}, { passive: true });
})();
