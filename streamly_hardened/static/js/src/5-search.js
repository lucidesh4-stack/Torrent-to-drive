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
    try {
      const params = new URLSearchParams();
      params.set("q", q);
      params.set("category", $("category").value || "");
      params.set("sort", currentSort);
      params.set("order", currentOrder);
      params.set("dedup", "1"); // dedup is always on (checkbox removed)
      // Quality applies to both modes (Normal groups by quality; Series uses it per-query).
      params.set("quality", getSelectedQualities().join(","));
      if (typeof seriesMode !== "undefined" && seriesMode) {
        params.set("mode", "series");
        params.set("encoders", getSelectedEncoders().join(","));
      }
      const data = await parseResponse(await fetch("/api/search?" + params.toString(), { credentials: "same-origin" }));

      if (data && data.mode === "series") {
        $("seriesResults").classList.remove("hidden");
        renderSeriesGrouped(data);
        const packs = (data.packs || []).length;
        const eps = (data.encoders || []).reduce((a, e) => a + (e.episode_count || 0), 0);
        const partialNote = data.partial ? " \u00b7 partial (time limit reached)" : "";
        if ($("resultCount")) $("resultCount").textContent =
          packs + " pack(s), " + eps + " episode(s) \u00b7 " + (data.requests_used || 0) + " request(s) used" + partialNote;
        status($("searchStatus"), "Found " + (packs + eps) + " result(s)" + (data.partial ? " (partial)" : ""), "ok");
        return;
      }

      // Normal mode = quality-grouped sections
      const groups = Array.isArray(data.quality_groups) ? data.quality_groups : [];
      $("seriesResults").classList.remove("hidden");
      renderNormalGrouped(groups);
      const total = groups.reduce((a, g) => a + (g.count || 0), 0);
      if ($("resultCount")) $("resultCount").textContent = total + " result(s) in " + groups.length + " quality group(s)";
      status($("searchStatus"), "Found " + total + " result(s)", "ok");
    } catch (err) {
      status($("searchStatus"), err.message || "Search failed", "error");
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
