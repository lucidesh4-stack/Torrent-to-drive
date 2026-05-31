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
      { label: "Add", key: null, cls: "h-add" },
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
    btn.textContent = "Adding...";
    try {
      for (const ep of episodes) saveToHistory(ep.magnet, ep.name);
      const first = episodes[0];
      await postJson("/api/add", { magnet: first.magnet, size: first.size_bytes || 0 });
      toast("Added " + (first.se || "episode 1") + " to Seedr \u00b7 " + episodes.length + " saved to History");
      btn.textContent = "\u2713 Done";
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
      addAll.textContent = "+ Add all " + opts.episodes.length;
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
        addAllQ.textContent = "+ Add all " + qEps.length;
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
