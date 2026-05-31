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
    // Client-side only: re-order the already-loaded results (no new bitsearch call).
    // Normal mode = quality sections; Series mode keeps its own structural order.
    if (typeof lastNormalGroups !== "undefined" && lastNormalGroups) {
      renderNormalGrouped(lastNormalGroups);
    }
  }

  // History Management (Redis Backend)
