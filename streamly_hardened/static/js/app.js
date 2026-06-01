
(() => {
  let csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  let currentFolder = 0;
  let parentFolder = 0;
  let selectedKeys = new Set();
  let lastClickedKey = null;
  // Backwards-compat shim: code reading "selected" expects single item.
  // We expose a getter that returns the first selected item or null.
  let selected = null;
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

  function renderCloud() {
    const body = $("cloudBody");
    body.textContent = "";
    const pathLabel = $("pathLabel");
    if (pathLabel) pathLabel.textContent = `Folder ID: ${currentFolder}`;
    $("upBtn").disabled = currentFolder === 0;
    $("cloudEmpty").classList.toggle("hidden", items.length !== 0);
    selectedKeys.clear();
    lastClickedKey = null;
    updateSelection();

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
      icon.textContent = item.type === "folder" ? "\u{1F4C1}" : "\u{1F3AC}";
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
    if (cnt) cnt.textContent = `${items.length} item${items.length === 1 ? "" : "s"}`;
    const empty = $("cloudMobileEmpty");
    if (empty) empty.classList.toggle("hidden", items.length !== 0);
    $("cmUpBtn").disabled = currentFolder === 0;

    for (const item of items) {
      const row = document.createElement("div");
      row.className = "cm-row";
      row.dataset.key = item.key;

      const tick = document.createElement("div");
      tick.className = "cm-tick";
      tick.textContent = "✓";

      const ic = document.createElement("div");
      ic.className = "cm-ic";
      ic.textContent = item.type === "folder" ? "\u{1F4C1}" : "\u{1F3AC}";

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
      kebab.textContent = "\u22EE";
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

  async function loadFolder(id) {
    status($("cloudStatus"), "Loading folder...", "");
    try {
      const data = await parseResponse(await fetch(`/fs/folder/${encodeURIComponent(id)}/items`, { credentials: "same-origin" }));
      currentFolder = Number(id);
      parentFolder = Number(data.parent || 0);
      items = [];
      for (const folder of data.folders || []) items.push({ ...folder, type: "folder", key: `folder:${folder.id}` });
      for (const file of data.files || []) items.push({ ...file, type: "file", key: `file:${file.id}` });
      updateStorage(data.used || 0, data.max || 1);
      renderCloud();
      status($("cloudStatus"), `Loaded ${items.length} item(s).`, "ok");
    } catch (err) {
      if ((err.message || "").toLowerCase().includes("login")) showLogin();
      status($("cloudStatus"), err.message || "Failed to load folder", "error");
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

  function getSelectedQualities() {
    return Array.from(document.querySelectorAll(".qualityOpt:checked")).map(c => c.value);
  }
  function getSelectedEncoders() {
    return Array.from(document.querySelectorAll(".encoderOpt:checked")).map(c => c.value);
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
    $("results").classList.add("hidden");
    $("pagination").classList.add("hidden");
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
      if (c.key) el.addEventListener("click", () => cycleSort(c.key));
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
    const se = document.createElement("span"); se.className = "se"; se.textContent = row.seeds || 0;
    const time = document.createElement("span"); time.className = "time"; time.textContent = row.date || "-";
    const size = document.createElement("span"); size.className = "size"; size.textContent = row.size || "-";
    const add = document.createElement("span"); add.className = "add"; add.appendChild(makeAddButton(row));
    wrap.append(name, se, time, size, add);
    return wrap;
  }

  // Normal mode: render quality sections (4K/1080p/720p/Other), rows sorted by current sort.
  function renderNormalGrouped(groups) {
    lastNormalGroups = groups || [];
    const container = $("seriesResults");
    container.textContent = "";
    if (!lastNormalGroups.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No results.";
      container.appendChild(empty);
      return;
    }
    syncSortControls();
    container.appendChild(seriesHeaderRow());
    for (const g of lastNormalGroups) {
      const section = document.createElement("div");
      section.className = "encoder-section collapsed"; // accordion: closed by default
      const header = sectionHeader({
        title: g.label,
        sub: null,
        count: g.count + (g.count === 1 ? " result" : " results"),
      });
      const body = document.createElement("div");
      body.className = "encoder-body";
      for (const r of sortRows(g.rows)) body.appendChild(plainRow(r));
      section.append(header, body);
      container.appendChild(section);
      makeAccordion(section, header, container, ".encoder-section");
    }
  }

  function seriesEpisodeRow(row, labelParts) {
    const wrap = document.createElement("div");
    wrap.className = "episode-row";

    const name = document.createElement("span");
    name.className = "name truncate";
    name.textContent = (labelParts || [row.name]).filter(Boolean).join(" · ");
    name.title = row.name || "";

    const se = document.createElement("span");
    se.className = "se";
    se.textContent = row.seeds || 0;

    const time = document.createElement("span");
    time.className = "time";
    time.textContent = row.date || "-";

    const size = document.createElement("span");
    size.className = "size";
    size.textContent = row.size || "-";

    const add = document.createElement("span");
    add.className = "add";
    add.appendChild(makeAddButton(row));

    wrap.append(name, se, time, size, add);
    return wrap;
  }

  // "Add all N": add ONLY the first episode to Seedr, save ALL episodes to History.
  async function addAllEpisodes(episodes, btn) {
    if (!episodes || !episodes.length) return;
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = "\u2026";
    try {
      for (const ep of episodes) saveToHistory(ep.magnet, ep.name);
      const first = episodes[0];
      await postJson("/api/add", { magnet: first.magnet, size: first.size_bytes || 0 });
      toast("Added " + (first.se || "episode 1") + " to Seedr \u00b7 " + episodes.length + " saved to History");
      btn.textContent = "\u2713";
    } catch (err) {
      toast(err.message || "Failed to add to Seedr (all saved to History)");
      btn.textContent = original;
      btn.disabled = false;
    }
  }

  function sectionHeader(opts) {
    // opts: {title, sub, count, episodes?}
    const header = document.createElement("div");
    header.className = "encoder-header";
    const titleWrap = document.createElement("div");
    titleWrap.className = "encoder-title";
    const chevron = document.createElement("span");
    chevron.className = "chevron";
    chevron.textContent = "\u25BC";
    const nameEl = document.createElement("span");
    nameEl.className = "encoder-name";
    nameEl.textContent = opts.title;
    titleWrap.append(chevron, nameEl);
    if (opts.sub) {
      const q = document.createElement("span");
      q.className = "encoder-quality";
      q.textContent = "\u2014 " + opts.sub;
      titleWrap.appendChild(q);
    }
    if (opts.count != null) {
      const countEl = document.createElement("span");
      countEl.className = "encoder-count";
      countEl.textContent = opts.count;
      titleWrap.appendChild(countEl);
    }
    header.appendChild(titleWrap);
    if (opts.episodes && opts.episodes.length) {
      const addAll = document.createElement("button");
      addAll.type = "button";
      addAll.className = "section-add";
      addAll.textContent = "+ " + opts.episodes.length;
      addAll.addEventListener("click", (e) => { e.stopPropagation(); addAllEpisodes(opts.episodes, addAll); });
      header.appendChild(addAll);
    }
    return header;
  }

  function renderSeriesGrouped(data) {
    lastSeriesData = data || null;
    const container = $("seriesResults");
    container.textContent = "";
    if (!data) return;

    const packs = data.packs || [];
    const encoders = data.encoders || [];

    if (!packs.length && !encoders.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No grouped results. Try different quality/encoder selections.";
      container.appendChild(empty);
      return;
    }

    syncSortControls();
    container.appendChild(seriesHeaderRow());

    // --- Season Packs on top (smallest-first); shown with ORIGINAL torrent name ---
    if (packs.length) {
      const section = document.createElement("div");
      section.className = "encoder-section packs collapsed"; // accordion: closed by default
      const header = sectionHeader({
        title: "\uD83D\uDCE6 Season Packs",
        sub: "complete seasons \u00b7 smallest first",
        count: packs.length + (packs.length === 1 ? " pack" : " packs"),
      });
      const body = document.createElement("div");
      body.className = "encoder-body";
      for (const p of packs) body.appendChild(seriesEpisodeRow(p, [p.name]));
      section.append(header, body);
      container.appendChild(section);
      makeAccordion(section, header, container, ".encoder-section");
    }

    // --- Encoder → Quality → Season → Episode (uploader level removed) ---
    for (const enc of encoders) {
      const section = document.createElement("div");
      section.className = "encoder-section collapsed"; // accordion: closed by default
      const allEps = (enc.qualities || []).flatMap(qg => qg.seasons.flatMap(s => s.episodes));
      const header = sectionHeader({
        title: enc.name,
        sub: (enc.qualities || []).length + " quality group(s)",
        count: enc.episode_count + (enc.episode_count === 1 ? " episode" : " episodes"),
        episodes: allEps,
      });
      const body = document.createElement("div");
      body.className = "encoder-body";

      for (const qg of enc.qualities || []) {
        // Each quality is its own collapsible accordion group within this encoder.
        const qGroup = document.createElement("div");
        qGroup.className = "uploader-group collapsed";
        const qlabel = document.createElement("div");
        qlabel.className = "uploader-label";
        const qEps = qg.seasons.flatMap(s => s.episodes);
        const chev = document.createElement("span");
        chev.className = "u-chevron";
        chev.textContent = "\u25BC";
        const txt = document.createElement("span");
        txt.style.flex = "1";
        txt.style.minWidth = "0";
        txt.textContent = (qg.label || qg.quality) + " (" + qg.episode_count + ")";
        qlabel.append(chev, txt);
        const addAllQ = document.createElement("button");
        addAllQ.type = "button";
        addAllQ.className = "section-add sm";
        addAllQ.textContent = "+ " + qEps.length;
        addAllQ.addEventListener("click", (e) => { e.stopPropagation(); addAllEpisodes(qEps, addAllQ); });
        qlabel.appendChild(addAllQ);
        qGroup.appendChild(qlabel);
        const qBody = document.createElement("div");
        qBody.className = "uploader-body";

        for (const s of qg.seasons) {
          const slabel = document.createElement("div");
          slabel.className = "season-label";
          slabel.textContent = "Season " + (s.season || "?");
          qBody.appendChild(slabel);
          // Episodes come pre-sorted in sequence; header clicks re-sort on demand.
          const eps = userSorted ? sortRows(s.episodes) : s.episodes;
          for (const ep of eps) {
            qBody.appendChild(seriesEpisodeRow(ep, [ep.series, ep.se, enc.name, qg.label || qg.quality]));
          }
        }
        qGroup.appendChild(qBody);
        body.appendChild(qGroup);
        // Quality-level accordion: one quality group open at a time within this encoder.
        makeAccordion(qGroup, qlabel, body, ".uploader-group");
      }
      section.append(header, body);
      container.appendChild(section);
      makeAccordion(section, header, container, ".encoder-section");
    }
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
        copyBtn.textContent = "📋";
        copyBtn.title = "Copy magnet link";
        copyBtn.onclick = async () => {
          try {
            await navigator.clipboard.writeText(item.magnet);
            copyBtn.textContent = "✓";
            toast("Magnet copied");
            setTimeout(() => { copyBtn.textContent = "📋"; }, 1500);
          } catch (e) {
            toast("Copy failed");
          }
        };

        const addBtn = document.createElement("button");
        addBtn.className = "hist-icon";
        addBtn.textContent = "+";
        addBtn.title = "Add to Destination";
        addBtn.onclick = async () => {
          addBtn.disabled = true;
          addBtn.textContent = "\u2026";
          try {
            await postJson("/api/add", { magnet: item.magnet });
            toast("Added from history: " + item.title);
            await saveToHistory(item.magnet, item.title); // Update timestamp
            addBtn.textContent = "✓";
          } catch (e) {
            toast("Failed: " + e.message);
            addBtn.disabled = false;
            addBtn.textContent = "+";
          }
        };
        
        const delBtn = document.createElement("button");
        delBtn.className = "danger ghost";
        delBtn.textContent = "✕";
        delBtn.style.padding = "6px 10px";
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
    renderHistory();
    $("historyOverlay").classList.remove("hidden");
  });

  $("closeHistoryBtn").addEventListener("click", () => {
    $("historyOverlay").classList.add("hidden");
  });

  $("clearHistoryBtn").addEventListener("click", async () => {
    if (confirm("Clear global magnet history?")) {
      await postJson("/api/history/clear", {});
      renderHistory();
    }
  });

  async function search(keepPage, page) {
    const q = $("searchQuery").value.trim();
    if (!q) return status($("searchStatus"), "Enter a search query", "error");

    clearTimeout(suggestTimer);
    $("suggestBox").classList.add("hidden");
    $("suggestBox").textContent = "";

    if (/^magnet:\?xt=urn:btih:/i.test(q)) {
      let magnetName = "Unknown Magnet";
      const dnMatch = q.match(/[?&]dn=([^&]+)/);
      if (dnMatch) {
        try { magnetName = decodeURIComponent(dnMatch[1].replace(/\+/g, " ")); } catch (_) {}
      }
      saveToHistory(q, magnetName);
      status($("searchStatus"), "Adding magnet to Seedr...", "");
      try {
        await postJson("/api/add", { magnet: q });
        status($("searchStatus"), "\u2713 Added: " + magnetName, "ok");
        $("searchQuery").value = "";
      } catch (err) {
        status($("searchStatus"), err.message || "Failed to add magnet", "error");
      }
      return;
    }

    if (!keepPage) currentPage = page || 1;
    status($("searchStatus"), "Searching...", "");
    if ($("resultCount")) $("resultCount").textContent = "";
    $("pagination").classList.add("hidden");
    $("pagination").textContent = "";
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
        $("results").classList.add("hidden");
        $("seriesResults").classList.remove("hidden");
        renderSeriesGrouped(data);
        const packs = (data.packs || []).length;
        const eps = (data.encoders || []).reduce((a, e) => a + (e.episode_count || 0), 0);
        if ($("resultCount")) $("resultCount").textContent = "";
        status($("searchStatus"), "Found " + packs + " pack(s) + " + eps + " episode(s) \u00b7 " + (data.requests_used || 0) + " request(s)", "ok");
        return;
      }

      // Normal mode = quality-grouped sections
      const groups = Array.isArray(data.quality_groups) ? data.quality_groups : [];
      $("results").classList.add("hidden");
      $("seriesResults").classList.remove("hidden");
      renderNormalGrouped(groups);
      const total = groups.reduce((a, g) => a + (g.count || 0), 0);
      if ($("resultCount")) $("resultCount").textContent = "";
      status($("searchStatus"), "Found " + total + " result(s) across " + groups.length + " quality group(s)", "ok");
    } catch (err) {
      if ($("resultCount")) $("resultCount").textContent = "";
      status($("searchStatus"), err.message || "Search failed", "error");
    }
  }

  function renderPagination(pagination, took, count) {
    const box = $("pagination");
    box.textContent = "";
    if (!pagination || (!pagination.total && !count)) return;
    const page = Number(pagination.page) || 1;
    const totalPages = Number(pagination.totalPages) || 1;
    const total = Number(pagination.total) || 0;
    const isNarrow = window.innerWidth < 500;

    function addButton(label, num, disabled, active) {
      const btn = document.createElement("button");
      btn.className = "page-btn" + (active ? " active" : "");
      btn.textContent = label;
      btn.disabled = disabled;
      btn.addEventListener("click", () => {
        currentPage = num;
        search(true, num);
        window.scrollTo({ top: ($("searchView").offsetTop || 200) - 100, behavior: "smooth" });
      });
      box.appendChild(btn);
    }

    addButton("\u2039", Math.max(1, page - 1), page <= 1, false);

    if (isNarrow) {
      addButton(String(page), page, false, true);
    } else {
      const pages = new Set([1, totalPages, page, page - 1, page + 1]);
      for (let i = 1; i <= Math.min(totalPages, 3); i++) pages.add(i);
      for (let i = Math.max(1, totalPages - 2); i <= totalPages; i++) pages.add(i);
      const ordered = [...pages].filter(n => n >= 1 && n <= totalPages).sort((a, b) => a - b);
      let last = 0;
      for (const n of ordered) {
        if (last && n > last + 1) {
          const gap = document.createElement("span");
          gap.className = "muted";
          gap.textContent = "...";
          box.appendChild(gap);
        }
        addButton(String(n), n, false, n === page);
        last = n;
      }
    }

    addButton("\u203A", Math.min(totalPages, page + 1), page >= totalPages, false);

    const info = document.createElement("div");
    info.className = "page-info";
    const tookText = typeof took === "number" ? " in " + took + "ms" : "";
    const perPage = Number(pagination.perPage || 50);
    info.textContent = total ? "Page " + page + " of " + totalPages + " (" + total + " results, " + perPage + "/page" + tookText + ")" : "Page " + page + " of " + totalPages;
    box.appendChild(info);
    box.classList.remove("hidden");
  }

  function renderSearchTable(results) {
    const body = $("torrentBody");
    const mobile = $("mobileResults");
    body.textContent = "";
    mobile.textContent = "";
    syncSortControls();
    for (const result of results) {
      const tr = document.createElement("tr");

      const name = document.createElement("td");
      name.className = "torrent-name";
      const nameText = document.createElement("div");
      nameText.className = "truncate";
      nameText.title = result.name || "";
      nameText.textContent = result.name || "Untitled";
      name.appendChild(nameText);

      const seeds = document.createElement("td");
      seeds.className = "num seed";
      seeds.textContent = result.seeds || 0;

      const date = document.createElement("td");
      date.className = "muted";
      date.textContent = result.date || "-";

      const size = document.createElement("td");
      size.className = "num muted";
      size.textContent = result.size || "-";

      const addTd = document.createElement("td");
      addTd.appendChild(makeAddButton(result));
      tr.append(name, seeds, date, size, addTd);
      body.appendChild(tr);

      const card = document.createElement("div");
      card.className = "mobile-result";
      const cardTitle = document.createElement("div");
      cardTitle.className = "result-title";
      cardTitle.textContent = result.name || "Untitled";
      const meta = document.createElement("div");
      meta.className = "mobile-meta";
      for (const part of ["Seeds: " + (result.seeds || 0), "Leeches: " + (result.leeches || 0), "Size: " + (result.size || "?"), "Date: " + (result.date || "?"), result.category || "Other"]) {
        const span = document.createElement("span");
        span.textContent = part;
        meta.appendChild(span);
      }
      const actions = document.createElement("div");
      actions.style.marginTop = "10px";
      actions.appendChild(makeAddButton(result));
      card.append(cardTitle, meta, actions);
      mobile.appendChild(card);
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
  if ($("copyLinkBtn")) $("copyLinkBtn").addEventListener("click", copySelectedLink);
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
})();
