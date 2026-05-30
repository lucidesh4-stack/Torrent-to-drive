
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
  let currentSort = "seeders";
  let currentOrder = "desc";
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
    $("userPill").classList.toggle("hidden", !isAuthenticated);
    $("userPill").textContent = username ? username : "Guest";
    $("accountLabel").textContent = username ? `Connected as ${username}` : "Guest Mode";
  }

  function showLogin() {
    $("loginScreen").classList.remove("hidden");
  }

  function fmtDate(value) {
    if (!value) return "-";
    const d = new Date(value);
    return isNaN(d.getTime()) ? String(value).slice(0, 19) : d.toLocaleString();
  }

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

  // Kept for any leftover callers
  function updateSelected(item) {
    selectedKeys.clear();
    if (item) selectedKeys.add(item.key);
    lastClickedKey = item ? item.key : null;
    updateSelection();
  }

  function renderCloud() {
    const body = $("cloudBody");
    body.textContent = "";
    $("pathLabel").textContent = `Folder ID: ${currentFolder}`;
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
  }

  function updateStorage(used, max) {
    const pct = max > 0 ? Math.min(100, Math.max(0, (used / max) * 100)) : 0;
    $("storageMeter").style.width = pct.toFixed(1) + "%";
    $("storageText").textContent = `${bytes(used)} / ${bytes(max)} used (${pct.toFixed(1)}%)`;
  }

  function bytes(n) {
    n = Number(n || 0);
    if (n >= 1024 ** 4) return (n / 1024 ** 4).toFixed(2) + " TB";
    if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(2) + " GB";
    if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
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
    search(false, 1);
  }

  // History Management (Redis Backend)
  async function saveToHistory(magnet, title) {
    try {
      await postJson("/api/history/add", { magnet: magnet, name: title || "Unknown Magnet" });
    } catch (e) {
      console.warn("Failed to save history", e);
    }
  }

  async function renderHistory() {
    const tbody = $("historyBody");
    tbody.innerHTML = "<tr><td colspan='3' class='muted' style='text-align:center;'>Loading...</td></tr>";
    
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
        const magDiv = document.createElement("div");
        magDiv.className = "truncate muted";
        magDiv.style.fontSize = "12px";
        magDiv.textContent = item.magnet;
        nameTd.append(titleDiv, magDiv);
        
        const timeTd = document.createElement("td");
        timeTd.className = "muted";
        timeTd.textContent = item.time;
        
        const actionTd = document.createElement("td");
        actionTd.style.textAlign = "right";
        const btnGroup = document.createElement("div");
        btnGroup.style.display = "inline-flex";
        btnGroup.style.gap = "4px";
        
        const addBtn = document.createElement("button");
        addBtn.textContent = "Add";
        addBtn.style.padding = "6px 10px";
        addBtn.title = "Add to Destination";
        addBtn.onclick = async () => {
          addBtn.disabled = true;
          addBtn.textContent = "...";
          try {
            const target = $("searchAddTarget") ? $("searchAddTarget").value : "seedr";
            if (target === "webtor") {
              const infohashMatch = item.magnet.match(/urn:btih:([a-zA-Z0-9]+)/i);
              if (infohashMatch) {
                window.open("https://webtor.io/" + infohashMatch[1].toLowerCase(), "_blank", "noopener,noreferrer");
                toast("Opened from history on Webtor");
                addBtn.textContent = "✓";
                saveToHistory(item.magnet, item.title); // Update timestamp
              } else {
                toast("Invalid magnet link for Webtor");
                addBtn.textContent = "Add";
                addBtn.disabled = false;
              }
            } else {
              await postJson("/api/add", { magnet: item.magnet });
              toast("Added from history: " + item.title);
              saveToHistory(item.magnet, item.title); // Update timestamp
              addBtn.textContent = "✓";
            }
          } catch (e) {
            toast("Failed: " + e.message);
            addBtn.disabled = false;
            addBtn.textContent = "Add";
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
        
        btnGroup.append(addBtn, delBtn);
        actionTd.appendChild(btnGroup);
        
        tr.append(nameTd, timeTd, actionTd);
        tbody.appendChild(tr);
      });
    } catch(e) {
      tbody.innerHTML = "<tr><td colspan='3' class='error' style='text-align:center;'>Failed to load history</td></tr>";
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

    // If user presses enter with a magnet link in search bar, handle it
    if (/^magnet:\?xt=urn:btih:/i.test(q)) {
      // Extract name from magnet
      let magnetName = "Unknown Magnet";
      const dnMatch = q.match(/[?&]dn=([^&]+)/);
      if (dnMatch) {
        try { magnetName = decodeURIComponent(dnMatch[1].replace(/\+/g, " ")); } catch (_) {}
      }
      
      // Save immediately to history before performing actions
      saveToHistory(q, magnetName);
      
      status($("searchStatus"), "Adding magnet to Seedr...", "");
      try {
        await postJson("/api/add", { magnet: q });
        status($("searchStatus"), "✓ Added: " + magnetName, "ok");
        $("searchQuery").value = "";
      } catch (err) {
        status($("searchStatus"), err.message || "Failed to add magnet", "error");
      }
      return;
    }

    if (!keepPage) currentPage = page || 1;
    status($("searchStatus"), "Searching...", "");
    $("pagination").classList.add("hidden");
    $("pagination").textContent = "";
    try {
      const params = new URLSearchParams();
      params.set("q", q);
      params.set("category", $("category").value || "");
      params.set("sort", currentSort);
      params.set("order", currentOrder);
      params.set("page", String(currentPage));
      const data = await parseResponse(await fetch("/api/search?" + params.toString(), { credentials: "same-origin" }));
      const results = Array.isArray(data.results) ? data.results : [];
      
      // Make the table and toolbar visible only after searching
      $("results").classList.remove("hidden");
      
      renderSearchTable(results);
      renderPagination(data.pagination, data.took, data.results ? data.results.length : 0);
      status($("searchStatus"), `Found ${results.length} result(s)`, "ok");
    } catch (err) {
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

    addButton("\u2039 Prev", Math.max(1, page - 1), page <= 1, false);
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
    addButton("Next \u203A", Math.min(totalPages, page + 1), page >= totalPages, false);

    const info = document.createElement("div");
    info.className = "page-info";
    const tookText = typeof took === "number" ? ` in ${took}ms` : "";
    const perPage = Number(pagination.perPage || 50);
    info.textContent = total ? `Showing page ${page} of ${totalPages} (${total} total results, ${perPage} per page${tookText})` : `Showing page ${page} of ${totalPages}`;
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

      const leeches = document.createElement("td");
      leeches.className = "num leech";
      leeches.textContent = result.leeches || 0;

      const date = document.createElement("td");
      date.className = "muted";
      date.textContent = result.date || "-";

      const size = document.createElement("td");
      size.className = "num muted";
      size.textContent = result.size || "-";

      const category = document.createElement("td");
      category.className = "muted";
      category.textContent = result.category || "Other";

      const addTd = document.createElement("td");
      const add = makeAddButton(result);
      addTd.appendChild(add);
      tr.append(name, seeds, leeches, date, size, category, addTd);
      body.appendChild(tr);

      const card = document.createElement("div");
      card.className = "mobile-result";
      const cardTitle = document.createElement("div");
      cardTitle.className = "result-title";
      cardTitle.textContent = result.name || "Untitled";
      const meta = document.createElement("div");
      meta.className = "mobile-meta";
      for (const part of [`Seeds: ${result.seeds || 0}`, `Leeches: ${result.leeches || 0}`, `Size: ${result.size || "?"}`, `Date: ${result.date || "?"}`, result.category || "Other"]) {
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
    add.textContent = "Add";
    add.addEventListener("click", async () => {
      // Save to history immediately when the button is pressed
      saveToHistory(result.magnet, result.name);

      add.disabled = true;
      add.textContent = "Adding...";
      try {
        await postJson("/api/add", { magnet: result.magnet });
        toast("Added to Seedr: " + (result.name || "torrent"));
        add.textContent = "✓ Added";
      } catch (err) {
        toast(err.message || "Failed to add");
        add.textContent = "Add";
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

    $("clearSearchBtn").addEventListener("click", () => { $("searchQuery").value = ""; $("suggestBox").classList.add("hidden"); $("torrentBody").textContent = ""; $("mobileResults").textContent = ""; $("pagination").classList.add("hidden"); $("pagination").textContent = ""; currentPage = 1; status($("searchStatus"), "", ""); });
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
})();
