  function syncSortControls() {
    for (const key of ["seeders", "leechers", "date", "size"]) {
      const mark = $("sortMark-" + key);
      if (mark) mark.textContent = key === currentSort ? (currentOrder === "desc" ? "\u25BC" : "\u25B2") : "";
    }
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
