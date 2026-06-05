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

        // Apply mobile-specific fixed positioning to escape clipping
        if (isMobileSearchUi()) {
          const rect = $("searchQuery").getBoundingClientRect();
          box.style.position = "fixed";
          box.style.top = (rect.bottom + 6) + "px";
          box.style.left = rect.left + "px";
          box.style.width = rect.width + "px";
          box.style.zIndex = "9900";
        } else {
          // Reset desktop/absolute inline styles
          box.style.position = "";
          box.style.top = "";
          box.style.left = "";
          box.style.width = "";
          box.style.zIndex = "";
        }

        for (const item of rows) {
          const row = document.createElement("div");
          row.className = "suggest-item";

          const isTv = String(item.year || "").includes("-") || String(item.year || "").includes("–");
          const typeIcon = isTv ? "📺" : "🎬";
          const typeName = isTv ? "TV" : "Movie";

          const iconSpan = document.createElement("span");
          iconSpan.className = "suggest-type-icon";
          iconSpan.textContent = typeIcon;

          const content = document.createElement("div");
          content.className = "suggest-content";

          const title = document.createElement("span");
          title.className = "suggest-title";
          title.textContent = item.title || "Untitled";

          const year = document.createElement("span");
          year.className = "suggest-year muted";
          year.textContent = ` · ${item.year || "N/A"}`;

          content.append(title, year);

          const typeSpan = document.createElement("span");
          typeSpan.className = "suggest-type-name muted";
          typeSpan.textContent = typeName;

          row.append(iconSpan, content, typeSpan);

          row.addEventListener("click", () => {
            $("searchQuery").value = item.title || "";
            box.classList.add("hidden");
            if (typeof search === "function") {
              search(false, 1);
            }
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
