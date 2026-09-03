  window.storageSnapshotLoading = false;
  window.storageSnapshotLoaded = false;
  window.seedrQueue = [];
  window.lastRequestedFolderId = 0;
  // Navigation stack of folder IDs we descended through. Bottom is always root (0),
  // so going "up" from a top-level folder reliably returns to 0 (where queue +
  // loading-transfers are shown) regardless of Seedr's non-zero root parent_id.
  window.folderStack = [];
  // Expose a deterministic "go up" the Up buttons can call.
  window.cloudGoUp = function () {
    if (window.driveProvider === "temp") {
      if (!window.tempCloudCurrentFolder) return;
      const parts = window.tempCloudCurrentFolder.replace(/\\/g, "/").split("/").filter(Boolean);
      parts.pop();
      window.tempCloudCurrentFolder = parts.length ? parts.join("/") : null;
      const subtitle = $("driveProviderSubtitle");
      if (subtitle) {
        subtitle.textContent = window.tempCloudCurrentFolder ? ("Folder: " + parts[parts.length - 1]) : "Temp Cloud (High-Speed Local Storage)";
      }
      const upBtn = $("upBtn");
      const cmUpBtn = $("cmUpBtn");
      if (upBtn) upBtn.disabled = !window.tempCloudCurrentFolder;
      if (cmUpBtn) cmUpBtn.disabled = !window.tempCloudCurrentFolder;
      loadTempCloudList();
      loadTempCloudListMobile();
      return;
    }
    if (window.driveProvider === "offcloud") {
      setDriveProvider("offcloud");
      return;
    }
    if (currentFolder === 0) return;
    const target = folderStack.length ? folderStack.pop() : 0;
    loadFolder(target, { _fromStack: true });
  };
  // Expose folder-open that records the stack.
  window.cloudOpenFolder = function (id) {
    folderStack.push(currentFolder || 0);
    loadFolder(id);
  };

  window.cloudRefresh = async function() {
    if (window.driveProvider === "temp") {
      await updateTempCloudStorageHeader();
      await loadTempCloudList();
      await loadTempCloudListMobile();
      return;
    }
    if (window.driveProvider === "offcloud") {
      if (window.offcloudCurrentFolder) {
        let folderName = "Folder";
        const subtitle = $("driveProviderSubtitle");
        if (subtitle && subtitle.textContent.startsWith("Folder: ")) {
          folderName = subtitle.textContent.replace("Folder: ", "");
        }
        updateStatus($("cloudStatus"), "Refreshing archive...", "");
        try {
          const res = await fetch(`/api/offcloud/explore/${window.offcloudCurrentFolder}`, { credentials: "same-origin" });
          const data = await res.json();
          if (!data.success) throw new Error(data.detail || "Failed to explore folder");
          renderOffcloudFolder(window.offcloudCurrentFolder, folderName, data.files || []);
        } catch (e) {
          toast("Error: " + e.message);
        } finally {
          updateStatus($("cloudStatus"), "", "");
        }
      } else {
        await loadOffcloudList();
        await loadOffcloudListMobile();
      }
    } else {
      await loadFolder(currentFolder || 0);
    }
  };

  window.updateSelection = function() {
    refreshSelectedShim();
    const currentItems = (window.items && window.items.length) ? window.items : (typeof items !== "undefined" ? items : []);
    const count = selectedKeys.size;
    const heading = $("selectionHeading");
    const clearBtn = $("clearSelBtn");
    const firstKey = count ? selectedKeys.values().next().value : null;
    const currentSelected = window.selected || (firstKey ? currentItems.find(it => it.key === firstKey) : null);

    if (count === 0) {
      heading.textContent = "Selected Item";
      $("selName").textContent = "None";
      $("selType").textContent = "-";
      $("selSize").textContent = "-";
      clearBtn.style.display = "none";
    } else if (count === 1) {
      const item = currentSelected;
      heading.textContent = "Selected Item";
      $("selName").textContent = item ? item.name : "None";
      $("selType").textContent = item ? (item.type || (item.is_video ? "video" : "file")) : "-";
      $("selSize").textContent = item ? (item.size_str || "-") : "-";
      clearBtn.style.display = "";
    } else {
      // Multi-select: aggregate
      const selectedItems = currentItems.filter(it => selectedKeys.has(it.key));
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
      else if (count === currentItems.length) { allCb.checked = true; allCb.indeterminate = false; }
      else { allCb.checked = false; allCb.indeterminate = true; }
    }

    // Open: enabled ONLY when exactly 1 item is selected
    $("openBtn").disabled = count !== 1;
    const renameBtn = $("renameBtn");
    const canRename = (window.driveProvider === "temp" && count === 1);
    if (renameBtn) renameBtn.disabled = !canRename;

    const copyBtn = $("copyLinkBtn");
    if (copyBtn) copyBtn.disabled = count === 0;
    const dlBtn = $("downloadBtn");
    if (dlBtn) dlBtn.disabled = count === 0;

    const selectedFiles = Array.from(selectedKeys).map(k => currentItems.find(x => x.key === k)).filter(x => x && (x.type === "file" || x.type === "archive" || x.is_video || x.download_url));
    const hasFolder = Array.from(selectedKeys).map(k => currentItems.find(x => x.key === k)).some(x => x && x.type === "folder");
    const telegramBtn = $("telegramBtn");
    if (telegramBtn) telegramBtn.disabled = selectedFiles.length === 0 || hasFolder;
    // Offcloud has no delete API -> disable/grey the Delete button in Offcloud mode
    // (same treatment as the Up button at root). Enabled normally for Seedr.
    const deleteBtn = $("deleteBtn");
    if (deleteBtn) deleteBtn.disabled = (window.driveProvider === "offcloud") || count === 0;

    // Archive Action (Zip folder / Unzip archive)
    const archiveBtn = $("archiveActionBtn");
    const cmArchiveBtn = $("cmBulkArchive");
    const cmRenameBtn = $("cmBulkRename");
    let isZipCandidate = false;
    let isUnzipCandidate = false;
    let archiveItem = null;

    if (window.driveProvider === "temp" && count === 1 && currentSelected) {
      archiveItem = currentSelected;
      if (archiveItem.type === "folder") {
        isZipCandidate = true;
      } else {
        const lowerName = (archiveItem.name || "").toLowerCase();
        if (lowerName.endsWith(".zip") || lowerName.endsWith(".rar") || lowerName.endsWith(".7z") || lowerName.endsWith(".tar.gz") || lowerName.endsWith(".tgz") || lowerName.endsWith(".tar")) {
          isUnzipCandidate = true;
        }
      }
    }

    if (archiveBtn) {
      if (isZipCandidate) {
        archiveBtn.classList.remove("hidden");
        archiveBtn.textContent = "📦 Zip Folder";
        archiveBtn.title = "Compress this folder into a .zip archive";
        archiveBtn.disabled = false;
      } else if (isUnzipCandidate) {
        archiveBtn.classList.remove("hidden");
        archiveBtn.textContent = "📂 Unzip Archive";
        archiveBtn.title = "Extract this archive in-place";
        archiveBtn.disabled = false;
      } else {
        archiveBtn.classList.add("hidden");
      }
    }

    // GPU Compress candidate (video files)
    const gpuBtn = $("gpuCompressBtn");
    const cmGpuBtn = $("cmBulkGpuCompress");
    let isVideoCandidate = false;
    if (count === 1 && currentSelected) {
      const isVid = (
        currentSelected.type === "video" ||
        currentSelected.is_video === true ||
        /\.(mp4|mkv|webm|avi|mov|flv|m4v|ts)$/i.test(currentSelected.name || "")
      );
      if (isVid) {
        isVideoCandidate = true;
      }
    }
    if (gpuBtn) {
      gpuBtn.classList.toggle("hidden", !isVideoCandidate);
      gpuBtn.disabled = !isVideoCandidate;
    }
    if (cmGpuBtn) {
      cmGpuBtn.classList.toggle("hidden", !isVideoCandidate);
      cmGpuBtn.disabled = !isVideoCandidate;
    }

    // ----- Mobile selection sync -----
    document.querySelectorAll("#cloudMobileList .cm-row").forEach((row) => {
      row.classList.toggle("sel", selectedKeys.has(row.dataset.key));
    });
    const bulk = $("cloudBulkBar");
    if (bulk) {
      bulk.classList.toggle("hidden", count === 0);
    }
    const cmCount = $("cmBulkCount");
    if (cmCount) {
      cmCount.textContent = count;
    }
    const mobileCloud = $("cloudMobile");
    if (mobileCloud) {
      mobileCloud.classList.toggle("cm-bulk-active", count > 0);
    }
    if (cmRenameBtn) {
      cmRenameBtn.classList.toggle("hidden", !canRename);
      cmRenameBtn.disabled = !canRename;
    }
    const cmDlBtn = $("cmBulkDownload");
    if (cmDlBtn) cmDlBtn.disabled = count === 0;
    const cmCpBtn = $("cmBulkCopy");
    if (cmCpBtn) cmCpBtn.disabled = count === 0;
    const tgBtn = $("cmBulkTelegram");
    if (tgBtn) tgBtn.disabled = selectedFiles.length === 0 || hasFolder;
    if (cmArchiveBtn) {
      if (isZipCandidate || isUnzipCandidate) {
        cmArchiveBtn.classList.remove("hidden");
        cmArchiveBtn.title = isZipCandidate ? "Zip Folder" : "Unzip Archive";
        cmArchiveBtn.disabled = false;
      } else {
        cmArchiveBtn.classList.add("hidden");
      }
    }
    const cmDelBtn = $("cmBulkDelete");
    if (cmDelBtn) cmDelBtn.disabled = (window.driveProvider === "offcloud") || count === 0;
    // Mobile select-all checkbox state
    const cmAll = $("cmSelectAll");
    if (cmAll) {
      if (count === 0) { cmAll.checked = false; cmAll.indeterminate = false; }
      else if (count === items.length) { cmAll.checked = true; cmAll.indeterminate = false; }
      else { cmAll.checked = false; cmAll.indeterminate = true; }
    }
  }

  window.toggleKey = function(key, additive, range) {
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

  window.transferPct = function(t) {
    const n = Number(t && t.progress);
    if (!isFinite(n)) return 0;
    return Math.max(0, Math.min(100, n));
  }

  window.transferMeta = function(t) {
    const parts = [];
    const pct = transferPct(t).toFixed(1).replace(/\.0$/, "");
    parts.push(pct + "%");
    if (t && t.status) parts.push(t.status);
    if (t && t.download_rate_str && t.download_rate > 0) parts.push(t.download_rate_str);
    if (t && t.seeders) parts.push(t.seeders + " seeders");
    return parts.join(" · ");
  }

  window.transferBar = function(t) {
    const bar = document.createElement("div");
    bar.className = "transfer-bar";
    const fill = document.createElement("div");
    fill.style.width = transferPct(t).toFixed(1) + "%";
    bar.appendChild(fill);
    return bar;
  }

  window.renderTransferRow = function(t) {
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
    cancelBtn.addEventListener("click", () => cancelSeedrTransfer(t));
    dateTd.appendChild(cancelBtn);
    tr.append(iconTd, nameTd, typeTd, sizeTd, dateTd);
    return tr;
  }

  window.renderQueuedRow = function(q) {
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

  window.syncCloudAutoRefresh = function() {
    clearTimeout(cloudAutoRefreshTimer);
    cloudAutoRefreshTimer = null;
    if (window.driveProvider === "offcloud") return;
    const cloudVisible = $("cloudView") && !$("cloudView").classList.contains("hidden");
    if (isAuthenticated && cloudVisible && (transfers.length > 0 || seedrQueue.length > 0)) {
      cloudAutoRefreshTimer = setTimeout(() => loadFolder(currentFolder || 0, { silent: true }), CLOUD_TRANSFER_REFRESH_MS);
    }
  }

  window.renderCloud = function() {
    const body = $("cloudBody");
    body.textContent = "";
    $("upBtn").classList.remove("hidden");
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

  window.cmTapTimer = null; // distinguishes single-tap (select) from double-tap (open)

  window.renderCloudMobile = function() {
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
    $("cmUpBtn").classList.remove("hidden");
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
      if (item.expiry_str) {
        s2.innerHTML = `<span style="color:#f59e0b; font-weight:500;">⏱️ ${escapeHtml(item.expiry_str)}</span>`;
      } else {
        s2.textContent = fmtDate(item.last_update);
      }
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
        cancel.addEventListener("click", (e) => { e.stopPropagation(); cancelSeedrTransfer(t); });
        
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



  window.updateStorage = function(used, max) {
    storageSnapshotLoaded = true;
    const pct = max > 0 ? Math.min(100, Math.max(0, (used / max) * 100)) : 0;

    const uGB = used / (1024 ** 3);
    const mGB = max / (1024 ** 3);
    const uText = uGB.toFixed(1);
    const mText = (mGB % 1 === 0) ? mGB.toFixed(0) : mGB.toFixed(1);
    const usedTotalLabel = `${uText} / ${mText} GB · ${pct.toFixed(0)}%`;

    const topMeter = $("topStorageMeter");
    const topText = $("topStorageText");
    const meterWrap = $("topStorageMeterWrap");

    if (topMeter) {
      topMeter.style.width = pct.toFixed(1) + "%";
      topMeter.style.backgroundImage = "none";
      topMeter.style.backgroundColor = pct >= 95 ? "#ef4444" : (pct >= 80 ? "#f59e0b" : "#2f9cf0");
      topMeter.style.boxShadow = pct >= 95 ? "0 0 8px rgba(239, 68, 68, 0.65)" : (pct >= 80 ? "0 0 8px rgba(245, 158, 11, 0.65)" : "0 0 8px rgba(47, 156, 240, 0.65)");
    }
    if (topText) topText.textContent = usedTotalLabel;
    if (meterWrap) meterWrap.title = `${used.toLocaleString()} / ${max.toLocaleString()} bytes`;
  }

  window.bytes = function(n) {
    n = Number(n || 0);
    if (n >= 1024 ** 4) return (n / 1024 ** 4).toFixed(2) + " TB";
    if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(2) + " GB";
    if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
  }

  window.refreshStorageSnapshot = async function(force = false) {
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

  window.loadFolder = async function(id, opts = {}) {
    const silent = !!(opts && opts.silent);
    const folderId = Number(id) || 0;
    
    // Loading root directly (initial load, cloud-tab click, or refresh) clears the
    // navigation stack so "up" history stays consistent. _fromStack skips this.
    if (folderId === 0 && !opts._fromStack && !silent) folderStack = [];
    
    // Track requested folder to prevent race conditions on slow connections
    lastRequestedFolderId = folderId;
    
    if (!silent) updateStatus($("cloudStatus"), "Loading folder...", "");
    try {
      // Fetch Seedr folder contents (backend already embeds queue in root response via data.queue)
      const folderRes = await fetch(`/fs/folder/${encodeURIComponent(folderId)}/items`, { credentials: "same-origin", cache: "no-store" });
      const data = await parseResponse(folderRes);
      
      // If the user has navigated to another folder or switched provider in the meantime, ignore this stale response
      if (lastRequestedFolderId !== folderId || window.driveProvider !== "seedr") return;
      
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
      if (!silent) updateStatus($("cloudStatus"), `Loaded ${items.length} item(s)` + (transfers.length ? ` · ${transfers.length} loading` : "") + (seedrQueue.length ? ` · ${seedrQueue.length} queued` : "") + ".", "ok");
      syncCloudAutoRefresh();
    } catch (err) {
      if (lastRequestedFolderId !== folderId) return;
      if ((err.message || "").toLowerCase().includes("login")) showLogin();
      if (!silent) updateStatus($("cloudStatus"), err.message || "Failed to load folder", "error");
      syncCloudAutoRefresh();
    }
  }

  // Renamed from window.cancelTransfer -- that name was ALSO used by
  // 4b-telegram-transfers.js for an unrelated function (cancelling a Telegram
  // upload, not a Seedr download). Since both files attach to the same shared
  // window object, whichever loaded last silently overwrote the other, so
  // clicking "cancel" on an active Seedr download here was actually invoking the
  // Telegram-cancel function with the wrong argument (a whole object instead of a
  // task-id string) -- it never actually cancelled the Seedr transfer. This
  // rename fixes that: the two functions can no longer collide.
  window.cancelSeedrTransfer = async function(t) {
    if (!t || !t.id) return toast("Transfer id unavailable");
    if (!confirm(`Cancel transfer: ${t.name || "loading torrent"}?`)) return;
    updateStatus($("cloudStatus"), "Cancelling transfer...", "");
    try {
      await postJson("/api/transfer/cancel", { id: t.id });
      toast("Transfer cancelled");
      await loadFolder(currentFolder || 0, { silent: true });
      updateStatus($("cloudStatus"), "Transfer cancelled.", "ok");
    } catch (err) {
      const message = err.message || "Cancel failed";
      toast(message);
      updateStatus($("cloudStatus"), message, "error");
    }
  }

  window.cancelQueuedItem = async function(q) {
    if (!q || !q.task_id) return toast("Task ID unavailable");
    if (!confirm(`Remove from queue: ${q.name || "queued torrent"}?`)) return;
    updateStatus($("cloudStatus"), "Cancelling queued item...", "");
    try {
      await postJson("/api/queue/cancel", { task_id: q.task_id });
      toast("Item removed from queue");
      await loadFolder(currentFolder || 0, { silent: true });
      updateStatus($("cloudStatus"), "Queue item removed.", "ok");
    } catch (err) {
      const message = err.message || "Failed to cancel queue item";
      toast(message);
      updateStatus($("cloudStatus"), message, "error");
    }
  }

  window.getFileUrl = async function(item) {
    if (!item || item.type !== "file") throw new Error("Select a file first");
    if (item.download_url) return item.download_url;
    if (typeof item.id === "string" && (item.id.startsWith("http://") || item.id.startsWith("https://"))) {
      return item.id;
    }
    if (window.driveProvider === "temp" || (item.key && item.key.startsWith("temp:"))) {
      return `/api/temp_cloud/stream?file_id=${encodeURIComponent(item.id)}`;
    }
    const data = await parseResponse(await fetch(`/api/url?file_id=${encodeURIComponent(item.id)}`, { credentials: "same-origin" }));
    if (!data.url) throw new Error("No download/stream URL returned");
    return data.url;
  }

  window._copyToClipboard = async function(text) {
    if (!text) return false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_) {}
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch (_) {
      return false;
    }
  };

  window.copySelectedLink = async function () {
    if (selectedKeys.size === 0) return toast("Select item(s) first");

    const selectedItems = items.filter((it) => selectedKeys.has(it.key));
    if (selectedItems.length === 0) return toast("Select item(s) first");

    const files = selectedItems.filter((it) => it.type === "file" || it.download_url);
    const folderCount = selectedItems.length - files.length;

    if (files.length === 0) {
      return toast("No files selected. Folders don't have a direct link to copy.");
    }

    updateStatus(
      $("cloudStatus"),
      files.length === 1 ? "Preparing file link..." : `Preparing ${files.length} file links...`,
      ""
    );

    try {
      const settled = await Promise.allSettled(files.map((f) => getFileUrl(f)));

      const urls = [];
      const failed = [];
      settled.forEach((res, i) => {
        if (res.status === "fulfilled" && res.value) urls.push(res.value);
        else failed.push(files[i].name || "Unnamed");
      });

      if (urls.length === 0) throw new Error("Could not resolve any file links");

      const text = urls.join("\n");
      const copied = await window._copyToClipboard(text);
      if (!copied) throw new Error("Could not copy link(s) to clipboard");

      let msg = `Copied ${urls.length} link${urls.length === 1 ? "" : "s"} to clipboard`;
      const extras = [];
      if (folderCount > 0) extras.push(`${folderCount} folder(s) skipped`);
      if (failed.length > 0) extras.push(`${failed.length} failed`);
      if (extras.length) msg += ` (${extras.join(", ")})`;

      toast(msg);
      updateStatus($("cloudStatus"), msg + ".", failed.length ? "error" : "ok");
    } catch (err) {
      const message = err.message || "Could not copy link(s)";
      toast(message);
      updateStatus($("cloudStatus"), message, "error");
    }
  }


  window.openItem = async function(item = selected) {
    if (!item) return toast("Select an item first");
    if (item.type === "folder") {
      if (window.driveProvider === "temp") {
        return window.openTempCloudItem(item);
      }
      return window.cloudOpenFolder(item.id);
    }
    try {
      const url = await getFileUrl(item);
      const ext = String(item.name || "").split(".").pop().toLowerCase();
      const currentProv = window.driveProvider || (item.key && item.key.startsWith("temp:") ? "temp" : "seedr");
      if (["mp4", "webm", "mov", "m4v", "mkv", "avi", "ts"].includes(ext)) {
        if (typeof window.openVideoPlayerModal === "function") {
          window.openVideoPlayerModal(currentProv, item.id, item.name || "Video Stream", formatBytes(item.size || 0));
          return;
        }
        window.open(url, "_blank", "noopener,noreferrer");
        return;
      } else {
        window.open(url, "_blank", "noopener,noreferrer");
      }
    } catch (err) {
      toast(err.message || "Could not open item");
    }
  }

  window._downloadFileDirect = function (url, name) {
    if (!url) return;
    try {
      const iframe = document.createElement("iframe");
      iframe.style.display = "none";
      iframe.style.width = "0";
      iframe.style.height = "0";
      iframe.style.border = "none";
      iframe.src = url;
      document.body.appendChild(iframe);
      setTimeout(() => {
        try { document.body.removeChild(iframe); } catch (_) {}
      }, 60000);
    } catch (_) {
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.download = name || "";
      document.body.appendChild(a);
      a.click();
      setTimeout(() => {
        try { document.body.removeChild(a); } catch (_) {}
      }, 5000);
    }
  };

  window.downloadSelected = async function () {
    if (selectedKeys.size === 0) return toast("Select item(s) first");
    const selectedItems = items.filter((it) => selectedKeys.has(it.key));
    if (selectedItems.length === 0) return toast("Select item(s) first");

    const folders = selectedItems.filter((it) => it.type === "folder");
    const files = selectedItems.filter((it) => it.type === "file" || it.download_url);

    // Folders can't be direct-downloaded — route them through zip.
    if (folders.length > 0 && files.length === 0) return zipSelected();
    if (folders.length > 0) {
      if (!confirm(`Selection has ${folders.length} folder(s). Folders will be zipped together with files. Continue?`)) return;
      return zipSelected();
    }

    if (files.length === 0) return toast("No files selected");

    // ---- 1. Resolve every URL FIRST (parallel), before any download fires. ----
    updateStatus($("cloudStatus"), `Preparing ${files.length} file(s)...`, "");
    const settled = await Promise.allSettled(files.map((f) => getFileUrl(f)));

    const resolved = [];
    const failed = [];
    settled.forEach((res, i) => {
      if (res.status === "fulfilled" && res.value) resolved.push({ url: res.value, name: files[i].name });
      else failed.push(files[i].name || "Unnamed");
    });

    if (resolved.length === 0) {
      const msg = "Could not resolve any download links";
      toast(msg);
      return updateStatus($("cloudStatus"), msg, "error");
    }

    // ---- 2. Fire each download with a small stagger (no await in between). ----
    updateStatus($("cloudStatus"), `Starting ${resolved.length} download(s)...`, "");
    resolved.forEach((item, i) => {
      setTimeout(() => {
        try {
          window._downloadFileDirect(item.url, item.name);
        } catch (err) {
          toast(`Failed: ${item.name} — ${err.message}`);
        }
        // Update progress on the last one.
        if (i === resolved.length - 1) {
          let msg = `Started ${resolved.length} download(s)`;
          if (failed.length) msg += ` (${failed.length} failed to resolve)`;
          updateStatus($("cloudStatus"), msg + ".", failed.length ? "error" : "ok");
          if (resolved.length > 1) {
            toast("If Chrome blocked extra files, tap the popup icon in your URL bar -> Allow multiple downloads.");
          }
        }
      }, i * 850); // 850ms stagger prevents browser anti-spam blocking
    });
  };

  window.zipSelected = async function() {
    if (selectedKeys.size === 0) return toast("Select item(s) first");
    const payload = items
      .filter(it => selectedKeys.has(it.key))
      .map(it => ({ type: it.type, id: it.id }));
    updateStatus($("cloudStatus"), `Preparing zip of ${payload.length} item(s)...`, "");
    try {
      const endpoint = payload.length === 1 ? "/api/zip" : "/api/zip/bulk";
      const body = payload.length === 1 ? { type: payload[0].type, id: payload[0].id } : { items: payload };
      const data = await postJson(endpoint, body);
      if (!data.url) throw new Error("Zip URL was not returned");
      window.open(data.url, "_blank", "noopener,noreferrer");
      updateStatus($("cloudStatus"), "Zip link opened.", "ok");
    } catch (err) {
      updateStatus($("cloudStatus"), err.message || "Zip failed", "error");
    }
  }

  window.archiveSelectedAction = async function() {
    if (selectedKeys.size === 0) return toast("Select an item first");
    const item = selected || items.find(x => selectedKeys.has(x.key));
    if (!item) return;

    if (window.driveProvider !== "temp") {
      return toast("Zip / Unzip is supported on Temp Cloud storage");
    }

    if (item.type === "folder") {
      // ZIP FOLDER
      updateStatus($("cloudStatus"), `Zipping folder "${item.name}"...`, "");
      toast(`Compressing folder "${item.name}" into .zip...`);
      try {
        const res = await postJson("/api/temp_cloud/zip", { item_id: item.id || item.name });
        if (res && res.success) {
          toast(res.message || `Folder zipped to ${res.zip_file}`);
          updateStatus($("cloudStatus"), res.message, "ok");
          await window.cloudRefresh();
        } else {
          throw new Error((res && res.detail) || "Zip operation failed");
        }
      } catch (err) {
        toast("Zip failed: " + (err.message || err));
        updateStatus($("cloudStatus"), err.message || "Zip failed", "error");
      }
    } else if (item.type === "file") {
      // UNZIP ARCHIVE
      updateStatus($("cloudStatus"), `Extracting archive "${item.name}"...`, "");
      toast(`Extracting archive "${item.name}"...`);
      try {
        const res = await postJson("/api/temp_cloud/unzip", { item_id: item.id || item.name });
        if (res && res.success) {
          toast(res.message || `Archive extracted to ${res.dest_folder}`);
          updateStatus($("cloudStatus"), res.message, "ok");
          await window.cloudRefresh();
        } else {
          throw new Error((res && res.detail) || "Unzip operation failed");
        }
      } catch (err) {
        toast("Unzip failed: " + (err.message || err));
        updateStatus($("cloudStatus"), err.message || "Unzip failed", "error");
      }
    }
  };

  window.createTempFolder = async function() {
    if (window.driveProvider !== "temp") {
      return toast("Folder creation is supported in Temp Cloud");
    }
    const name = prompt("Enter new folder name:", "New Folder");
    if (!name || !name.trim()) return;
    const cleanName = name.trim();
    updateStatus($("cloudStatus"), `Creating folder "${cleanName}"...`, "");
    toast(`Creating folder "${cleanName}"...`);
    try {
      const res = await postJson("/api/temp_cloud/folder/create", {
        name: cleanName,
        folder_id: window.tempCloudCurrentFolder || ""
      });
      if (res && res.success) {
        toast(res.message || `Folder "${cleanName}" created`);
        updateStatus($("cloudStatus"), res.message, "ok");
        await window.cloudRefresh();
      } else {
        throw new Error((res && res.detail) || "Create folder failed");
      }
    } catch (err) {
      toast("Create folder failed: " + (err.message || err));
      updateStatus($("cloudStatus"), err.message || "Create folder failed", "error");
    }
  };

  window.renameSelectedItem = async function() {
    if (selectedKeys.size !== 1) return toast("Select exactly one item to rename");
    const item = selected || items.find(x => selectedKeys.has(x.key));
    if (!item) return;

    if (window.driveProvider !== "temp") {
      return toast("Rename is supported on Temp Cloud storage");
    }

    const newName = prompt(`Rename "${item.name}" to:`, item.name);
    if (!newName || !newName.trim() || newName.trim() === item.name) return;
    const cleanNewName = newName.trim();

    updateStatus($("cloudStatus"), `Renaming to "${cleanNewName}"...`, "");
    toast(`Renaming to "${cleanNewName}"...`);
    try {
      const res = await postJson("/api/temp_cloud/rename", {
        item_id: item.id || item.name,
        new_name: cleanNewName
      });
      if (res && res.success) {
        toast(res.message || `Renamed to "${cleanNewName}"`);
        updateStatus($("cloudStatus"), res.message, "ok");
        await window.cloudRefresh();
      } else {
        throw new Error((res && res.detail) || "Rename failed");
      }
    } catch (err) {
      toast("Rename failed: " + (err.message || err));
      updateStatus($("cloudStatus"), err.message || "Rename failed", "error");
    }
  };

  window.deleteSelected = async function() {
    if (selectedKeys.size === 0) return toast("Select item(s) first");
    
    // Offcloud has no delete API; the delete button is disabled in Offcloud mode
    // (see updateSelection). Guard here too in case it's ever invoked directly.
    if (window.driveProvider === "offcloud") {
      return toast("Delete isn't supported for Offcloud downloads.");
    }

    const payload = items
      .filter(it => selectedKeys.has(it.key))
      .map(it => ({ type: it.type, id: it.id }));
    const msg = payload.length === 1
      ? `Delete item: ${items.find(it => selectedKeys.has(it.key))?.name || 'file'}?`
      : `Delete ${payload.length} items? This cannot be undone.`;
    if (!confirm(msg)) return;
    updateStatus($("cloudStatus"), `Deleting ${payload.length} item(s)...`, "");
    try {
      if (window.driveProvider === "temp") {
        for (const item of payload) {
          await postJson("/api/temp_cloud/delete", { item_id: item.id });
        }
        toast(`Deleted ${payload.length} item(s) from Temp Cloud`);
        loadTempCloudList();
        loadTempCloudListMobile();
        updateTempCloudStorageHeader();
        return;
      }
      if (payload.length === 1) {
        await postJson("/api/delete", { type: payload[0].type, id: payload[0].id });
      } else {
        await postJson("/api/delete/bulk", { items: payload });
      }
      toast(`Deleted ${payload.length} item(s)`);
      await loadFolder(currentFolder);
    } catch (err) {
      updateStatus($("cloudStatus"), err.message || "Delete failed", "error");
    }
  }

  window.sendSelectedToTelegram = async function() {
    if (selectedKeys.size === 0) return toast("Select a file first");
    
    const filesToSend = [];
    for (const key of selectedKeys) {
      const it = items.find(x => x.key === key);
      if (it && (it.type === "file" || it.type === "archive" || it.download_url)) {
        filesToSend.push(it);
      }
    }
    
    if (filesToSend.length === 0) {
      return toast("Select at least one file. Folders cannot be sent directly.");
    }
    
    // Keep filesToSend in exact user selection insertion order (from selectedKeys)
    
    // Check sizes
    for (const item of filesToSend) {
      if (item.size > 2097152000) {
        toast(`File "${item.name}" exceeds 1.95 GB limit.`);
        return updateStatus($("cloudStatus"), `File "${item.name}" exceeds 1.95 GB limit`, "error");
      }
    }
    
    updateStatus($("cloudStatus"), `Preparing transfer for ${filesToSend.length} file(s)...`, "");
    
    const isOffcloud = window.driveProvider === "offcloud";
    const batchPayloads = [];

    for (const item of filesToSend) {
      try {
        let payload;
        if (window.driveProvider === "temp") {
          payload = {
            file_id: item.id,
            provider: "temp",
            file_name: item.name,
            file_size: item.size,
            download_url: item.id
          };
        } else if (isOffcloud) {
          let dlUrl = item.download_url;
          if (!dlUrl) {
            const res = await fetch(`/api/offcloud/explore/${item.id}`, { credentials: "same-origin" });
            const data = await res.json();
            if (data.success && data.files && data.files.length > 0) {
              dlUrl = data.files[0].download_url;
            }
          }
          if (!dlUrl) {
            throw new Error("Download URL not found");
          }
          payload = {
            file_id: item.id,
            provider: "offcloud",
            file_name: item.name,
            file_size: item.size,
            download_url: dlUrl
          };
        } else {
          payload = { file_id: item.id, provider: "seedr" };
          const rawSz = item.size ?? item.size_bytes ?? item.file_size;
          const _sz = Number(rawSz);
          if (item.name) {
            payload.file_name = item.name;
          }
          if (Number.isFinite(_sz) && _sz > 0) {
            payload.file_size = Math.floor(_sz);
          }
        }
        batchPayloads.push(payload);
      } catch (err) {
        console.warn("Payload prep failed for item:", err);
      }
    }
    
    let successCount = 0;
    if (batchPayloads.length > 0) {
      try {
        if (batchPayloads.length === 1) {
          const data = await postJson("/api/telegram/send", batchPayloads[0]);
          if (data.success) successCount = 1;
        } else {
          const data = await postJson("/api/telegram/send_batch", { items: batchPayloads });
          if (data.success && data.task_ids) {
            successCount = data.task_ids.length;
          }
        }
      } catch (bErr) {
        console.warn("Telegram batch send failed:", bErr);
      }
    }

    if (successCount === filesToSend.length) {
      toast(`Queued ${successCount} file(s) for Telegram upload`);
      updateStatus($("cloudStatus"), `Queued ${successCount} file(s) for Telegram upload`, "ok");
    } else {
      toast(`Queued ${successCount} of ${filesToSend.length} file(s) for Telegram upload`);
      updateStatus($("cloudStatus"), `Queued ${successCount} of ${filesToSend.length} file(s) for Telegram upload`, "error");
    }
    
    if (successCount > 0) {
      if (typeof window.triggerQueuePolling === "function") {
        window.triggerQueuePolling();
      }
    }
  }

  window.openTelegramSettings = async function() {
    $("telegramAuthOverlay").classList.remove("hidden");
    updateStatus($("tgAuthStatus"), "Checking status...", "");
    
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
      updateStatus($("tgAuthStatus"), "", "");
    } catch (err) {
      console.error("Error loading Telegram status:", err);
      updateStatus($("tgAuthStatus"), "Failed to load connection status", "error");
    }
  }

  window.showTelegramAuthModal = function() {
    openTelegramSettings();
  }

  // =================== OFFCLOUD INTEGRATION: Drive provider pill ===================
  window.driveProvider = "seedr"; // "seedr" | "offcloud"
  window.offcloudCurrentFolder = null; // Stores request_id if exploring an Offcloud folder

  window.tempCloudCurrentFolder = null;

  const MAGNET_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24"><path d="M5 14V8a7 7 0 0 1 14 0v6h-4V8a3 3 0 0 0-6 0v6H5z" fill="#f43f5e"/><rect x="5" y="14" width="4" height="4" rx="0.5" fill="#cbd5e1"/><rect x="15" y="14" width="4" height="4" rx="0.5" fill="#cbd5e1"/></svg>`;
  const LINK_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-link" style="color: var(--accent);"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>`;

  window.setDriveProvider = async function(provider) {
    if (provider === "temp") {
      window.driveProvider = "temp";
    } else if (provider === "offcloud") {
      window.driveProvider = "offcloud";
    } else {
      window.driveProvider = "seedr";
    }
    try {
      localStorage.setItem("streamly:drive_provider", window.driveProvider);
    } catch (_) {}
    
    const isSeedr = window.driveProvider === "seedr";
    const isOffcloud = window.driveProvider === "offcloud";
    const isTemp = window.driveProvider === "temp";
    
    window.offcloudCurrentFolder = null;
    window.tempCloudCurrentFolder = null;

    const seedrBtn = $("driveProviderSeedr");
    const offcloudBtn = $("driveProviderOffcloud");
    const tempBtn = $("driveProviderTemp");
    const seedrBtnMobile = $("driveProviderSeedrMobile");
    const offcloudBtnMobile = $("driveProviderOffcloudMobile");
    const tempBtnMobile = $("driveProviderTempMobile");

    if (seedrBtn) seedrBtn.classList.toggle("active", isSeedr);
    if (offcloudBtn) offcloudBtn.classList.toggle("active", isOffcloud);
    if (tempBtn) tempBtn.classList.toggle("active", isTemp);
    if (seedrBtnMobile) seedrBtnMobile.classList.toggle("active", isSeedr);
    if (offcloudBtnMobile) offcloudBtnMobile.classList.toggle("active", isOffcloud);
    if (tempBtnMobile) tempBtnMobile.classList.toggle("active", isTemp);

    // Update Desktop and Mobile Magnet/Link Action Icons
    const dIcon = $("desktopActionIcon") || ($("pasteMagnetBtn") ? $("pasteMagnetBtn").querySelector("svg") : null);
    const mIcon = $("mobileActionIcon") || ($("cmMagnetBtn") ? $("cmMagnetBtn").querySelector("svg") : null);
    const pBtn = $("pasteMagnetBtn");
    const cmBtn = $("cmMagnetBtn");

    if (isTemp) {
      if (pBtn) { pBtn.innerHTML = LINK_SVG; pBtn.title = "Download direct link / archive to Temp Cloud"; }
      if (cmBtn) { cmBtn.innerHTML = LINK_SVG; cmBtn.title = "Download direct link / archive to Temp Cloud"; }
    } else {
      if (pBtn) { pBtn.innerHTML = MAGNET_SVG; pBtn.title = "Paste & add magnet link from clipboard"; }
      if (cmBtn) { cmBtn.innerHTML = MAGNET_SVG; cmBtn.title = "Paste magnet from clipboard"; }
    }

    const upBtn = $("upBtn");
    const cmUpBtn = $("cmUpBtn");
    const subtitle = $("driveProviderSubtitle");
    const nfBtn = $("newFolderBtn");
    const cmNfBtn = $("cmNewFolderBtn");

    if (isTemp) {
      if (upBtn) { upBtn.classList.remove("hidden"); upBtn.disabled = (window.tempCloudCurrentFolder == null); }
      if (cmUpBtn) { cmUpBtn.classList.remove("hidden"); cmUpBtn.disabled = (window.tempCloudCurrentFolder == null); }
      if (nfBtn) nfBtn.classList.remove("hidden");
      if (cmNfBtn) cmNfBtn.classList.remove("hidden");
      if (subtitle) subtitle.textContent = "Temp Cloud (High-Speed Local Storage)";
      await updateTempCloudStorageHeader();
      loadTempCloudList();
      loadTempCloudListMobile();
    } else if (isOffcloud) {
      if (upBtn) { upBtn.classList.remove("hidden"); upBtn.disabled = (window.offcloudCurrentFolder == null); }
      if (cmUpBtn) { cmUpBtn.classList.remove("hidden"); cmUpBtn.disabled = (window.offcloudCurrentFolder == null); }
      if (nfBtn) nfBtn.classList.add("hidden");
      if (cmNfBtn) cmNfBtn.classList.add("hidden");
      if (subtitle) subtitle.textContent = "Files sent via Offcloud (large-file overflow)";
      if (typeof window.renderStorage === "function") window.renderStorage();
      loadOffcloudList();
      loadOffcloudListMobile();
    } else {
      if (upBtn) { upBtn.classList.remove("hidden"); upBtn.disabled = (currentFolder || 0) == 0; }
      if (cmUpBtn) { cmUpBtn.classList.remove("hidden"); cmUpBtn.disabled = (currentFolder || 0) == 0; }
      if (nfBtn) nfBtn.classList.add("hidden");
      if (cmNfBtn) cmNfBtn.classList.add("hidden");
      if (subtitle) subtitle.textContent = "Browse your saved files and folders";
      if (typeof window.renderStorage === "function") window.renderStorage();
      loadFolder(currentFolder || 0);
    }
  };

  async function updateTempCloudStorageHeader(retryCount = 0) {
    try {
      let res = await fetch("/api/temp_cloud/storage", { credentials: "same-origin" });
      if (res.status === 401 && retryCount === 0) {
        const relogged = await window.attemptSilentRelogin();
        if (relogged) {
          return updateTempCloudStorageHeader(1);
        }
      }
      if (!res.ok) return;
      const data = await res.json();
      const topText = $("topStorageText");
      const accLabel = $("accountLabel");
      const meter = $("topStorageMeter");
      if (topText) topText.textContent = data.storage_metrics || "Unknown";
      if (accLabel) accLabel.textContent = data.storage_subtext || "Auto-expires 24h";
      if (meter) {
        meter.style.width = `${data.percent || 0}%`;
        meter.style.background = "linear-gradient(90deg, #06b6d4, #3b82f6)";
      }
    } catch (_) {}
  }

  function offcloudStatusLabel(status) {
    switch (status) {
      case "downloaded": return "Ready";
      case "error": return "Error";
      case "created": return "Downloading…";
      default: return status || "Unknown";
    }
  }

  function isOffcloudFolder(name) {
    if (!name) return false;
    const archiveExtensions = new Set(["zip", "rar", "tar", "gz", "7z"]);
    const knownFileExtensions = new Set([
      "mp4", "mkv", "avi", "mov", "m4v", "webm", "flv", "ts", "wmv", "mpg", "mpeg",
      "mp3", "wav", "m4a", "flac", "ogg", "wma", "iso",
      "pdf", "txt", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
      "jpg", "jpeg", "png", "gif", "bmp", "svg", "webp"
    ]);
    const parts = name.split(".");
    if (parts.length <= 1) return true; // No dot -> folder
    const ext = parts.pop().toLowerCase();
    
    if (archiveExtensions.has(ext)) return true;
    return !knownFileExtensions.has(ext);
  }

  window.handleOffcloudRowClick = async function(item) {
    if (item.status !== "downloaded") {
      toast("Download is still in progress or has failed (" + item.status + ")");
      return;
    }
    updateStatus($("cloudStatus"), "Exploring archive...", "");
    try {
      const res = await fetch(`/api/offcloud/explore/${item.request_id}`, { credentials: "same-origin" });
      const data = await res.json();
      if (!data.success) throw new Error(data.detail || "Failed to explore folder");
      
      const files = data.files || [];
      if (files.length === 0) {
        toast("No files found in this archive.");
        return;
      }
      
      if (files.length === 1) {
        await playOrDownloadOffcloudFile(files[0]);
      } else {
        renderOffcloudFolder(item.request_id, item.file_name, files);
      }
    } catch (e) {
      toast("Error: " + e.message);
    } finally {
      updateStatus($("cloudStatus"), "", "");
    }
  }

  window.playOrDownloadOffcloudFile = async function(file) {
    const ext = String(file.name || "").split(".").pop().toLowerCase();
    if (["mp4", "webm", "mov", "m4v", "mkv", "avi", "ts"].includes(ext)) {
      if (typeof window.openVideoPlayerModal === "function") {
        const itemKey = file.download_url || file.request_id;
        window.openVideoPlayerModal("offcloud", itemKey, file.name || "Offcloud Video", file.size_str || "");
        return;
      }
      window.open(file.download_url, "_blank", "noopener,noreferrer");
    } else {
      window.open(file.download_url, "_blank", "noopener,noreferrer");
    }
  }

  async function fetchOffcloudListItems() {
    const res = await fetch("/api/offcloud/list", { credentials: "same-origin", cache: "no-store" });
    const data = await parseResponse(res);
    return data.items || [];
  }

  window.reloadCloudView = function(opts = {}) {
    if (window.driveProvider === "temp") {
      loadTempCloudList();
      loadTempCloudListMobile();
    } else if (window.driveProvider === "offcloud") {
      loadOffcloudList();
      loadOffcloudListMobile();
    } else {
      loadFolder(currentFolder || 0, opts);
    }
  };

  window.loadOffcloudList = async function() {
    const body = $("cloudBody");
    const empty = $("cloudEmpty");
    if (!body) return;
    updateStatus($("cloudStatus"), "Loading Offcloud list...", "");
    try {
      const listItems = await fetchOffcloudListItems();
      if (window.driveProvider !== "offcloud") return;
      body.textContent = "";

      // Normalize Offcloud items to match Seedr item schema
      window.items = listItems.map(item => {
        const type = isOffcloudFolder(item.file_name) ? "folder" : "file";
        const key = `offcloud:${item.request_id}`;
        return {
          ...item,
          key: key,
          id: item.request_id,
          name: item.file_name || "Unnamed",
          type: type,
          size: item.size_bytes || 0,
          size_str: item.size_bytes ? bytes(item.size_bytes) : "-",
          last_update: item.created_at || Math.floor(Date.now() / 1000)
        };
      });

      window.items.sort((a, b) => {
        if (a.type !== b.type) return a.type === "folder" ? -1 : 1;
        return a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });
      });

      if (empty) empty.classList.toggle("hidden", window.items.length !== 0);
      selectedKeys.clear();
      lastClickedKey = null;
      updateSelection();

      for (const item of window.items) {
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
        name.textContent = item.name;
        nameCell.append(icon, name);
        nameTd.appendChild(nameCell);

        const statusTd = document.createElement("td");
        statusTd.className = "muted";
        statusTd.textContent = offcloudStatusLabel(item.status);

        const sizeTd = document.createElement("td");
        sizeTd.className = "muted";
        sizeTd.textContent = item.size_str;

        const dateTd = document.createElement("td");
        dateTd.className = "muted";
        dateTd.textContent = item.created_at ? fmtDate(item.created_at * 1000) : "-";

        tr.append(checkTd, nameTd, statusTd, sizeTd, dateTd);
        tr.style.cursor = "pointer";
        tr.addEventListener("click", (e) => {
          if (e.target.closest(".row-check")) return;
          toggleKey(item.key, e.ctrlKey || e.metaKey, e.shiftKey);
        });
        tr.addEventListener("dblclick", () => handleOffcloudRowClick(item));
        body.appendChild(tr);
      }
      updateStatus($("cloudStatus"), "", "");
    } catch (err) {
      if (window.driveProvider !== "offcloud") return;
      updateStatus($("cloudStatus"), err.message || "Failed to load Offcloud list", "error");
    }
  }

  window.loadOffcloudListMobile = async function() {
    const list = $("cloudMobileList");
    if (!list) return;
    const cnt = $("cmCount");
    const empty = $("cloudMobileEmpty");
    try {
      const listItems = await fetchOffcloudListItems();
      if (window.driveProvider !== "offcloud") return;
      list.textContent = "";

      // Normalize Offcloud items to match Seedr item schema
      window.items = listItems.map(item => {
        const type = isOffcloudFolder(item.file_name) ? "folder" : "file";
        const key = `offcloud:${item.request_id}`;
        return {
          ...item,
          key: key,
          id: item.request_id,
          name: item.file_name || "Unnamed",
          type: type,
          size: item.size_bytes || 0,
          size_str: item.size_bytes ? bytes(item.size_bytes) : "-",
          last_update: item.created_at || Math.floor(Date.now() / 1000)
        };
      });

      if (cnt) cnt.textContent = `${window.items.length} item${window.items.length === 1 ? "" : "s"}`;
      if (empty) empty.classList.toggle("hidden", window.items.length !== 0);

      for (const item of window.items) {
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
        fn.textContent = item.name;
        const meta = document.createElement("div");
        meta.className = "cm-meta";
        const s1 = document.createElement("span");
        s1.textContent = offcloudStatusLabel(item.status);
        const s2 = document.createElement("span");
        s2.textContent = item.size_str;
        meta.append(s1, s2);
        info.append(fn, meta);

        row.append(tick, ic, info);
        row.addEventListener("click", (e) => {
          if (cmTapTimer) {
            clearTimeout(cmTapTimer);
            cmTapTimer = null;
            handleOffcloudRowClick(item);
            return;
          }
          cmTapTimer = setTimeout(() => {
            cmTapTimer = null;
            toggleKey(item.key, true, false);
          }, 250);
        });
        list.appendChild(row);
      }
    } catch (err) {
      updateStatus($("cloudStatus"), err.message || "Failed to load Offcloud list", "error");
    }
  }

  window.renderOffcloudFolder = function(requestId, folderName, files) {
    window.offcloudCurrentFolder = requestId;
    
    const upBtn = $("upBtn");
    const cmUpBtn = $("cmUpBtn");
    if (upBtn) {
      upBtn.classList.remove("hidden");
      upBtn.disabled = false;
    }
    if (cmUpBtn) {
      cmUpBtn.classList.remove("hidden");
      cmUpBtn.disabled = false;
    }

    const subtitle = $("driveProviderSubtitle");
    if (subtitle) subtitle.textContent = "Folder: " + folderName;

    // Normalize explored files to match Seedr item schema
    window.items = files.map((file, index) => {
      const key = `offcloud_file:${requestId}:${index}`;
      return {
        ...file,
        key: key,
        id: file.download_url,
        name: file.name || "Unnamed",
        type: "file",
        size: file.size || 0,
        size_str: file.size ? bytes(file.size) : "-",
        last_update: Math.floor(Date.now() / 1000)
      };
    });

    window.items.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" }));

    const body = $("cloudBody");
    const empty = $("cloudEmpty");
    if (body) {
      body.textContent = "";
      if (empty) empty.classList.add("hidden");
      
      window.items.forEach((file) => {
        const tr = document.createElement("tr");
        tr.dataset.key = file.key;
        
        const checkTd = document.createElement("td");
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.className = "row-check";
        cb.addEventListener("click", (e) => { e.stopPropagation(); toggleKey(file.key, true, false); });
        checkTd.appendChild(cb);
        
        const nameTd = document.createElement("td");
        const nameCell = document.createElement("div");
        nameCell.className = "name-cell";
        const icon = document.createElement("span");
        icon.className = "icon";
        icon.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-video"><path d="m22 8-6 4 6 4V8Z"/><rect width="14" height="12" x="2" y="6" rx="2" ry="2"/></svg>`;
        const name = document.createElement("span");
        name.className = "truncate";
        name.textContent = file.name;
        nameCell.append(icon, name);
        nameTd.appendChild(nameCell);

        const statusTd = document.createElement("td");
        statusTd.className = "muted";
        statusTd.textContent = "Ready";

        const sizeTd = document.createElement("td");
        sizeTd.className = "muted";
        sizeTd.textContent = "-";

        const dateTd = document.createElement("td");
        dateTd.className = "muted";
        dateTd.textContent = "-";

        tr.append(checkTd, nameTd, statusTd, sizeTd, dateTd);
        tr.style.cursor = "pointer";
        tr.addEventListener("click", (e) => {
          if (e.target.closest(".row-check")) return;
          toggleKey(file.key, e.ctrlKey || e.metaKey, e.shiftKey);
        });
        tr.addEventListener("dblclick", () => playOrDownloadOffcloudFile(file));
        body.appendChild(tr);
      });
    }

    const list = $("cloudMobileList");
    const mEmpty = $("cloudMobileEmpty");
    const cnt = $("cmCount");
    if (list) {
      list.textContent = "";
      if (mEmpty) mEmpty.classList.add("hidden");
      if (cnt) cnt.textContent = `${window.items.length} item${window.items.length === 1 ? "" : "s"}`;

      window.items.forEach((file) => {
        const row = document.createElement("div");
        row.className = "cm-row";
        row.dataset.key = file.key;

        const tick = document.createElement("div");
        tick.className = "cm-tick";
        tick.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-check"><polyline points="20 6 9 17 4 12"/></svg>`;

        const ic = document.createElement("div");
        ic.className = "cm-ic";
        ic.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-video"><path d="m22 8-6 4 6 4V8Z"/><rect width="14" height="12" x="2" y="6" rx="2" ry="2"/></svg>`;

        const info = document.createElement("div");
        info.className = "cm-info";
        const fn = document.createElement("div");
        fn.className = "cm-fn";
        fn.textContent = file.name;
        const meta = document.createElement("div");
        meta.className = "cm-meta";
        const s1 = document.createElement("span");
        s1.textContent = "Ready";
        meta.append(s1);
        info.append(fn, meta);

        row.append(tick, ic, info);
        row.addEventListener("click", (e) => {
          if (cmTapTimer) {
            clearTimeout(cmTapTimer);
            cmTapTimer = null;
            playOrDownloadOffcloudFile(file);
            return;
          }
          cmTapTimer = setTimeout(() => {
            cmTapTimer = null;
            toggleKey(file.key, true, false);
          }, 250);
        });
        list.appendChild(row);
      });
    }
  };

  window.addMagnetFromClipboard = async function() {
    let clipText = "";
    try {
      if (navigator.clipboard && navigator.clipboard.readText) {
        clipText = await navigator.clipboard.readText();
      }
    } catch (_) {}

    clipText = (clipText || "").trim();
    
    const isMagnet = clipText.toLowerCase().startsWith("magnet:?") || clipText.toLowerCase().includes("xt=urn:btih:");

    if (!isMagnet) {
      const userPrompt = prompt(
        clipText 
          ? "Clipboard does not contain a valid magnet link. Paste magnet link manually:" 
          : "Paste magnet link to add to cloud:", 
        clipText && clipText.length < 300 && clipText.length > 5 ? clipText : ""
      );
      if (!userPrompt) return;
      clipText = userPrompt.trim();
    }

    if (!clipText.toLowerCase().startsWith("magnet:?") && !clipText.toLowerCase().includes("xt=urn:btih:")) {
      toast("Invalid magnet link format");
      return updateStatus($("cloudStatus"), "Invalid magnet link format", "error");
    }

    let magnetName = "Unknown Magnet";
    const dnMatch = clipText.match(/[?&]dn=([^&]+)/i);
    if (dnMatch) {
      try { magnetName = decodeURIComponent(dnMatch[1].replace(/\+/g, " ")); } catch (_) {}
    }

    if (typeof window.saveToHistory === "function") {
      window.saveToHistory(clipText, magnetName);
    }

    updateStatus($("cloudStatus"), "Adding magnet to cloud...", "");
    try {
      const res = await postJson("/api/add", {
        magnet: clipText,
        provider: window.driveProvider || "auto"
      });

      let msg = "Added: " + magnetName;
      if (res && res.queued) {
        msg = "Added to queue: " + magnetName;
      } else if (res && res.provider === "offcloud") {
        msg = "Added to Offcloud: " + magnetName;
      } else {
        msg = "Added to Seedr: " + magnetName;
      }
      
      toast(msg);
      updateStatus($("cloudStatus"), "✓ " + msg, "ok");

      if (typeof window.cloudRefresh === "function") {
        window.cloudRefresh();
      }
    } catch (err) {
      const errMsg = err.message || "Failed to add magnet link";
      toast(errMsg);
      updateStatus($("cloudStatus"), errMsg, "error");
    }
  };

  // ===== Temp Cloud (24h Ephemeral Drive) Loaders =====
  window.loadTempCloudList = async function(retryCount = 0) {
    const body = $("cloudBody");
    const empty = $("cloudEmpty");
    if (!body) return;
    updateStatus($("cloudStatus"), "Loading Temp Cloud...", "");
    try {
      const folderParam = window.tempCloudCurrentFolder ? `?folder_id=${encodeURIComponent(window.tempCloudCurrentFolder)}` : "";
      let res = await fetch(`/api/temp_cloud/list${folderParam}`, { credentials: "same-origin", cache: "no-store" });
      if (res.status === 401 && retryCount === 0) {
        const relogged = await window.attemptSilentRelogin();
        if (relogged) {
          return await window.loadTempCloudList(1);
        }
      }
      const data = await parseResponse(res);
      if (window.driveProvider !== "temp") return;

      body.textContent = "";
      const allItems = [...(data.folders || []), ...(data.files || [])];
      
      window.items = allItems.map(item => ({
        ...item,
        key: `temp:${item.id}`,
        last_update: item.created_at || Math.floor(Date.now() / 1000)
      }));

      if (empty) empty.classList.toggle("hidden", window.items.length !== 0);
      selectedKeys.clear();
      lastClickedKey = null;
      updateSelection();

      for (const item of window.items) {
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
          icon.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#06b6d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-file"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/></svg>`;
        }
        const name = document.createElement("span");
        name.className = "truncate";
        name.textContent = item.name;
        nameCell.append(icon, name);
        nameTd.appendChild(nameCell);

        const statusTd = document.createElement("td");
        statusTd.className = "muted";
        statusTd.textContent = item.type === "folder" ? `${item.items_count || 0} items` : "Ready";

        const sizeTd = document.createElement("td");
        sizeTd.className = "muted";
        sizeTd.textContent = item.size_str || "-";

        const dateTd = document.createElement("td");
        dateTd.className = "muted";
        dateTd.innerHTML = item.expiry_str ? `<span style="color:#f59e0b; font-weight:500;">⏱️ ${escapeHtml(item.expiry_str)}</span>` : fmtDate(item.last_update);

        tr.append(checkTd, nameTd, statusTd, sizeTd, dateTd);
        tr.style.cursor = "pointer";
        tr.addEventListener("click", (e) => {
          if (e.target.closest(".row-check")) return;
          toggleKey(item.key, e.ctrlKey || e.metaKey, e.shiftKey);
        });
        tr.addEventListener("dblclick", () => openTempCloudItem(item));
        body.appendChild(tr);
      }
      updateStatus($("cloudStatus"), "", "");
    } catch (err) {
      if (window.driveProvider !== "temp") return;
      updateStatus($("cloudStatus"), err.message || "Failed to load Temp Cloud", "error");
    }
  };

  window.loadTempCloudListMobile = async function(retryCount = 0) {
    const list = $("cloudMobileList");
    if (!list) return;
    const cnt = $("cmCount");
    const empty = $("cloudMobileEmpty");
    try {
      const folderParam = window.tempCloudCurrentFolder ? `?folder_id=${encodeURIComponent(window.tempCloudCurrentFolder)}` : "";
      let res = await fetch(`/api/temp_cloud/list${folderParam}`, { credentials: "same-origin", cache: "no-store" });
      if (res.status === 401 && retryCount === 0) {
        const relogged = await window.attemptSilentRelogin();
        if (relogged) {
          return await window.loadTempCloudListMobile(1);
        }
      }
      const data = await parseResponse(res);
      if (window.driveProvider !== "temp") return;

      list.textContent = "";
      const allItems = [...(data.folders || []), ...(data.files || [])];
      window.items = allItems.map(item => ({
        ...item,
        key: `temp:${item.id}`,
        last_update: item.created_at || Math.floor(Date.now() / 1000)
      }));

      if (cnt) cnt.textContent = `${window.items.length} item${window.items.length === 1 ? "" : "s"}`;
      if (empty) empty.classList.toggle("hidden", window.items.length !== 0);

      for (const item of window.items) {
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
          ic.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#06b6d4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-video"><path d="m22 8-6 4 6 4V8Z"/><rect width="14" height="12" x="2" y="6" rx="2" ry="2"/></svg>`;
        }

        const info = document.createElement("div");
        info.className = "cm-info";
        const fn = document.createElement("div");
        fn.className = "cm-fn";
        fn.textContent = item.name;
        const meta = document.createElement("div");
        meta.className = "cm-meta";
        const s1 = document.createElement("span");
        s1.textContent = item.size_str || "-";
        const s2 = document.createElement("span");
        if (item.expiry_str) {
          s2.innerHTML = `<span style="color:#f59e0b; font-weight:500;">⏱️ ${escapeHtml(item.expiry_str)}</span>`;
        } else {
          s2.textContent = fmtDate(item.last_update);
        }
        meta.append(s1, s2);
        info.append(fn, meta);

        row.append(tick, ic, info);
        row.addEventListener("click", (e) => {
          if (cmTapTimer) {
            clearTimeout(cmTapTimer);
            cmTapTimer = null;
            openTempCloudItem(item);
            return;
          }
          cmTapTimer = setTimeout(() => {
            cmTapTimer = null;
            toggleKey(item.key, true, false);
          }, 250);
        });
        list.appendChild(row);
      }
    } catch (err) {
      if (window.driveProvider !== "temp") return;
      updateStatus($("cloudStatus"), err.message || "Failed to load Temp Cloud", "error");
    }
  };

  window.openTempCloudItem = function(item) {
    if (!item) return;
    if (item.type === "folder") {
      window.tempCloudCurrentFolder = item.id;
      const subtitle = $("driveProviderSubtitle");
      if (subtitle) subtitle.textContent = "Folder: " + item.name;
      const upBtn = $("upBtn");
      const cmUpBtn = $("cmUpBtn");
      if (upBtn) upBtn.disabled = false;
      if (cmUpBtn) cmUpBtn.disabled = false;
      loadTempCloudList();
      loadTempCloudListMobile();
      return;
    }

    const ext = String(item.name || "").split(".").pop().toLowerCase();
    if (["mp4", "webm", "mov", "m4v", "mkv", "avi", "ts", "mp3", "m4a", "aac"].includes(ext) || item.is_video) {
      if (typeof window.openVideoPlayerModal === "function") {
        window.openVideoPlayerModal("temp", item.id, item.name || "Temp Video", item.size_str || (item.size ? formatBytes(item.size) : ""));
        return;
      }
    }
    window.open(`/api/temp_cloud/stream?file_id=${encodeURIComponent(item.id)}`, "_blank", "noopener,noreferrer");
  };

  // Hook provider buttons
  document.addEventListener("DOMContentLoaded", () => {
    const tempBtn = $("driveProviderTemp");
    const tempBtnM = $("driveProviderTempMobile");
    if (tempBtn) tempBtn.addEventListener("click", () => setDriveProvider("temp"));
    if (tempBtnM) tempBtnM.addEventListener("click", () => setDriveProvider("temp"));

    // Restore saved provider on load
    try {
      const savedProvider = localStorage.getItem("streamly:drive_provider");
      if (savedProvider && ["temp", "offcloud"].includes(savedProvider)) {
        setDriveProvider(savedProvider);
      }
    } catch (_) {}

    // Intercept magnet buttons when in Temp Cloud mode to open Unified Cloud Downloader Modal
    const handleActionBtnClick = (e) => {
      if (window.driveProvider === "temp") {
        e.preventDefault();
        e.stopImmediatePropagation();
        if (window.openCloudDownloadsModal) {
          window.openCloudDownloadsModal();
        }
      }
    };

    const pBtn = $("pasteMagnetBtn");
    const cmBtn = $("cmMagnetBtn");
    if (pBtn) pBtn.addEventListener("click", handleActionBtnClick, true);
    if (cmBtn) cmBtn.addEventListener("click", handleActionBtnClick, true);
  });

  // ============================================================================
  // ⚡ Colab GPU Compression Handlers
  // ============================================================================
  window.openGpuCompressModal = function() {
    const currentItems = (window.items && window.items.length) ? window.items : (typeof items !== "undefined" ? items : []);
    const firstKey = selectedKeys.size ? selectedKeys.values().next().value : null;
    const selectedItem = window.selected || (firstKey ? currentItems.find(it => it.key === firstKey) : null);
    if (!selectedItem) {
      if (window.toast) window.toast("Please select a video first");
      return;
    }

    window._targetGpuItem = selectedItem;
    const modal = $("gpuCompressModal");
    if (modal) modal.classList.remove("hidden");

    const nameEl = $("gpuTargetFileName");
    if (nameEl) nameEl.textContent = selectedItem.name;
    const sizeEl = $("gpuTargetFileSize");
    if (sizeEl) sizeEl.textContent = "Size: " + (selectedItem.size_str || (selectedItem.size ? (Math.round(selectedItem.size / (1024*1024)*10)/10 + " MB") : "-"));

    window.fetchGpuWorkerStatus();
  };

  window.closeGpuCompressModal = function() {
    const modal = $("gpuCompressModal");
    if (modal) modal.classList.add("hidden");
  };

  window.fetchGpuWorkerStatus = async function() {
    try {
      const res = await fetch("/api/gpu/status");
      const data = await res.json();
      const dot = $("gpuModalStatusDot");
      const text = $("gpuModalStatusText");
      if (data && data.online) {
        if (dot) { dot.style.background = "#10b981"; dot.style.boxShadow = "0 0 8px #10b981"; }
        if (text) { text.textContent = "Colab GPU: " + (data.gpu_name || "NVIDIA T4") + " (Online)"; text.style.color = "#34d399"; }
      } else {
        if (dot) { dot.style.background = "#64748b"; dot.style.boxShadow = "none"; }
        if (text) { text.textContent = "Colab GPU: Offline (will queue until Colab runs)"; text.style.color = "#94a3b8"; }
      }
    } catch (e) {}
  };

  window.startGpuCompress = async function() {
    const item = window._targetGpuItem;
    if (!item) return;

    const mode = $("gpuCompressMode") ? $("gpuCompressMode").value : "VBR";
    const bitrate = $("gpuTargetBitrate") ? parseInt($("gpuTargetBitrate").value) : 1500;

    window.closeGpuCompressModal();
    if (window.toast) window.toast("⚡ Queuing video for Colab GPU compression...");

    try {
      const res = await window.postJson("/api/gpu/compress", {
        file_id: item.id || item.file_id || item.key,
        filename: item.name,
        source_type: window.driveProvider === "temp" ? "temp_cloud" : "seedr",
        source_url: item.download_url || null,
        mode: mode,
        target_bitrate_k: bitrate
      });
      if (res && res.success) {
        if (window.toast) window.toast("✅ Queued for Colab GPU! Opening tasks drawer...");
        if (typeof window.openCloudDownloadsModal === "function") window.openCloudDownloadsModal();
      } else {
        if (window.toast) window.toast("Failed: " + (res?.error || "Error queuing"));
      }
    } catch (e) {
      if (window.toast) window.toast("Compression request failed: " + e.message);
    }
  };
})();
