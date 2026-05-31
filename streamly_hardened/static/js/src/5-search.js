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
    $("pagination").classList.add("hidden");
    $("pagination").textContent = "";
    try {
      const params = new URLSearchParams();
      params.set("q", q);
      params.set("category", $("category").value || "");
      params.set("sort", currentSort);
      params.set("order", currentOrder);
      params.set("page", String(currentPage));
      const dedupEl = $("dedupToggle");
      params.set("dedup", dedupEl && !dedupEl.checked ? "0" : "1");
      if (typeof seriesMode !== "undefined" && seriesMode) {
        params.set("mode", "series");
        params.set("quality", getSelectedQualities().join(","));
        params.set("encoders", getSelectedEncoders().join(","));
      }
      const data = await parseResponse(await fetch("/api/search?" + params.toString(), { credentials: "same-origin" }));

      if (data && data.mode === "series") {
        $("results").classList.add("hidden");
        $("seriesResults").classList.remove("hidden");
        renderSeriesGrouped(data);
        const packs = (data.packs || []).length;
        const eps = (data.encoders || []).reduce((a, e) => a + (e.episode_count || 0), 0);
        if ($("resultCount")) $("resultCount").textContent =
          packs + " pack(s), " + eps + " episode(s) \u00b7 " + (data.requests_used || 0) + " request(s) used";
        status($("searchStatus"), "Found " + (packs + eps) + " result(s)", "ok");
        return;
      }

      const results = Array.isArray(data.results) ? data.results : [];
      $("seriesResults").classList.add("hidden");
      $("results").classList.remove("hidden");
      renderSearchTable(results);
      renderPagination(data.pagination, data.took, data.results ? data.results.length : 0);
      if ($("resultCount")) $("resultCount").textContent = "";
      status($("searchStatus"), "Found " + results.length + " result(s)", "ok");
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
    add.textContent = "Add";
    add.addEventListener("click", async () => {
      saveToHistory(result.magnet, result.name);
      add.disabled = true;
      add.dataset.state = "adding";
      add.textContent = "Adding...";
      try {
        await postJson("/api/add", { magnet: result.magnet, size: result.size_bytes || 0 });
        toast("Added to Seedr: " + (result.name || "torrent"));
        add.dataset.state = "done";
        add.textContent = "\u2713 Added";
      } catch (err) {
        toast(err.message || "Failed to add");
        add.dataset.state = "idle";
        add.textContent = "Add";
        add.disabled = false;
      }
    });
    return add;
  }
