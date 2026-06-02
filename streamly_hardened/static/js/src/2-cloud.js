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

