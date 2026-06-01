  /* ===== Series Mode v2 + Normal grouped ===== */
  let seriesMode = false;
  // Holds the last rendered dataset so client-side sorting can re-order without re-fetching.
  let lastNormalGroups = null;   // [{quality,label,count,rows}]
  let lastSeriesData = null;     // {packs, encoders, ...}
  // True once the user clicks a column header; until then Series keeps its native
  // S/E order (Normal always uses size-asc default regardless).
  let userSorted = false;
  let activeNormalQuality = "";
  let activeSeriesQuality = "";
  const activeSeriesSeason = Object.create(null);

  function isMobileSearchUi() {
    return window.matchMedia && window.matchMedia("(max-width: 768px)").matches;
  }

  function qualityLabel(q) {
    return ({ "2160p": "4K", "1080p": "1080p", "720p": "720p", "Other": "Other" })[q] || q || "Other";
  }

  function qualityBucketFromName(name) {
    const m = String(name || "").match(/(?:^|[^0-9])(2160p|1080p|720p)(?:[^0-9]|$)/i);
    return m ? m[1].toLowerCase() : "Other";
  }

  function normalizeQualityList(list) {
    const order = ["2160p", "1080p", "720p", "Other"];
    const set = new Set((list || []).filter(Boolean));
    return order.filter(q => set.has(q));
  }

  function chooseActiveQuality(available, current) {
    const qs = normalizeQualityList(available);
    if (!qs.length) return "";
    if (current && qs.includes(current)) return current;
    const selected = getSelectedQualities();
    for (const q of selected) if (qs.includes(q)) return q;
    if (qs.includes("1080p")) return "1080p";
    return qs[0];
  }

  function mobileQualityNav(available, active, onPick) {
    const qs = normalizeQualityList(available).filter(q => q !== "Other");
    if (!qs.length) return null;
    const nav = document.createElement("div");
    nav.className = "mobile-quality-nav";
    for (const q of qs) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "mobile-quality-tab" + (q === active ? " active" : "");
      btn.textContent = qualityLabel(q);
      btn.addEventListener("click", (e) => { e.stopPropagation(); onPick(q); });
      nav.appendChild(btn);
    }
    return nav;
  }

  function openSectionKeys(container) {
    if (!container) return new Set();
    return new Set(Array.from(container.querySelectorAll(":scope > .encoder-section:not(.collapsed)[data-acc-key]")).map(el => el.dataset.accKey));
  }

  function applyOpenState(section, key, openKeys) {
    section.dataset.accKey = key;
    if (openKeys && openKeys.has(key)) section.classList.remove("collapsed");
  }

  function getSelectedQualities() {
    const sel = isMobileSearchUi() ? ".mQualityOpt:checked" : ".qualityOpt:checked";
    const values = Array.from(document.querySelectorAll(sel)).map(c => c.value);
    return values.length ? values : Array.from(document.querySelectorAll(".qualityOpt:checked")).map(c => c.value);
  }
  function getSelectedEncoders() {
    const sel = isMobileSearchUi() ? ".mEncoderOpt:checked" : ".encoderOpt:checked";
    return Array.from(document.querySelectorAll(sel)).map(c => c.value);
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
      if (c.key) el.addEventListener("click", (e) => { e.stopPropagation(); cycleSort(c.key); });
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

  // Normal mode: render quality sections. On mobile, quality tabs navigate one
  // quality at a time; desktop keeps the existing accordion sections.
  function renderNormalGrouped(groups) {
    lastNormalGroups = groups || [];
    const container = $("seriesResults");
    const prevOpen = openSectionKeys(container);
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

    if (isMobileSearchUi()) {
      const available = lastNormalGroups.map(g => g.quality);
      activeNormalQuality = chooseActiveQuality(available, activeNormalQuality);
      const nav = mobileQualityNav(available, activeNormalQuality, (q) => {
        activeNormalQuality = q;
        renderNormalGrouped(lastNormalGroups);
      });
      if (nav) container.appendChild(nav);
      const active = lastNormalGroups.find(g => g.quality === activeNormalQuality) || lastNormalGroups[0];
      for (const r of sortRows(active.rows || [])) container.appendChild(plainRow(r));
      return;
    }

    for (const g of lastNormalGroups) {
      const section = document.createElement("div");
      section.className = "encoder-section collapsed";
      applyOpenState(section, "normal:" + g.quality, prevOpen);
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
    // opts: {title, sub, count}. Bulk Add-all buttons intentionally removed.
    const header = document.createElement("div");
    header.className = "encoder-header";
    const titleWrap = document.createElement("div");
    titleWrap.className = "encoder-title";
    const chevron = document.createElement("span");
    chevron.className = "chevron";
    chevron.textContent = "▼";
    const nameEl = document.createElement("span");
    nameEl.className = "encoder-name";
    nameEl.textContent = opts.title;
    titleWrap.append(chevron, nameEl);
    if (opts.sub) {
      const q = document.createElement("span");
      q.className = "encoder-quality";
      q.textContent = "— " + opts.sub;
      titleWrap.appendChild(q);
    }
    if (opts.count != null) {
      const countEl = document.createElement("span");
      countEl.className = "encoder-count";
      countEl.textContent = opts.count;
      titleWrap.appendChild(countEl);
    }
    header.appendChild(titleWrap);
    return header;
  }

  function renderSeriesGrouped(data) {
    lastSeriesData = data || null;
    const container = $("seriesResults");
    const prevOpen = openSectionKeys(container);
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

    const mobile = isMobileSearchUi();
    const available = [];
    for (const p of packs) available.push(qualityBucketFromName(p.name));
    for (const enc of encoders) for (const qg of (enc.qualities || [])) available.push(qg.quality);
    activeSeriesQuality = chooseActiveQuality(available, activeSeriesQuality);
    if (mobile) {
      const nav = mobileQualityNav(available, activeSeriesQuality, (q) => {
        activeSeriesQuality = q;
        renderSeriesGrouped(lastSeriesData);
      });
      if (nav) container.appendChild(nav);
    }

    const packsToShow = mobile ? packs.filter(p => qualityBucketFromName(p.name) === activeSeriesQuality) : packs;
    if (packsToShow.length) {
      const section = document.createElement("div");
      section.className = "encoder-section packs collapsed";
      applyOpenState(section, mobile ? "packs" : "packs:all", prevOpen);
      const header = sectionHeader({
        title: "📦 Season Packs",
        sub: mobile ? null : "complete seasons · smallest first",
        count: packsToShow.length + (packsToShow.length === 1 ? " pack" : " packs"),
      });
      const body = document.createElement("div");
      body.className = "encoder-body";
      for (const p of packsToShow) body.appendChild(seriesEpisodeRow(p, [p.name]));
      section.append(header, body);
      container.appendChild(section);
      makeAccordion(section, header, container, ".encoder-section");
    }

    for (const enc of encoders) {
      const qualityGroups = mobile
        ? (enc.qualities || []).filter(qg => qg.quality === activeSeriesQuality)
        : (enc.qualities || []);
      if (!qualityGroups.length) continue;
      const visibleCount = qualityGroups.reduce((a, qg) => a + (qg.episode_count || 0), 0);
      if (!visibleCount) continue;

      const section = document.createElement("div");
      section.className = "encoder-section collapsed";
      applyOpenState(section, "enc:" + enc.encoder_norm, prevOpen);
      const header = sectionHeader({
        title: enc.name,
        sub: mobile ? null : qualityGroups.length + " quality group(s)",
        count: visibleCount + (visibleCount === 1 ? " episode" : " episodes"),
      });
      const body = document.createElement("div");
      body.className = "encoder-body";

      for (const qg of qualityGroups) {
        if (mobile) {
          const title = document.createElement("div");
          title.className = "mobile-encoder-title";
          const strong = document.createElement("strong");
          strong.textContent = enc.name;
          const badge = document.createElement("span");
          badge.className = "encoder-count";
          badge.textContent = qg.label || qualityLabel(qg.quality);
          title.append(strong, badge);
          body.appendChild(title);

          const seasons = qg.seasons || [];
          if (seasons.length) {
            const skey = enc.encoder_norm + ":" + qg.quality;
            const availableSeasons = seasons.map(s => s.season);
            if (!activeSeriesSeason[skey] || !availableSeasons.includes(activeSeriesSeason[skey])) {
              activeSeriesSeason[skey] = availableSeasons[0];
            }
            const nav = document.createElement("div");
            nav.className = "mobile-season-nav";
            for (const season of availableSeasons) {
              const btn = document.createElement("button");
              btn.type = "button";
              btn.className = "mobile-season-tab" + (season === activeSeriesSeason[skey] ? " active" : "");
              btn.textContent = "S" + season;
              btn.addEventListener("click", (e) => {
                e.stopPropagation();
                activeSeriesSeason[skey] = season;
                renderSeriesGrouped(lastSeriesData);
              });
              nav.appendChild(btn);
            }
            body.appendChild(nav);
            const activeSeason = seasons.find(s => s.season === activeSeriesSeason[skey]) || seasons[0];
            const eps = userSorted ? sortRows(activeSeason.episodes || []) : (activeSeason.episodes || []);
            for (const ep of eps) body.appendChild(seriesEpisodeRow(ep, [ep.se, enc.name, qg.label || qg.quality]));
          }
          continue;
        }

        const qGroup = document.createElement("div");
        qGroup.className = "uploader-group collapsed";
        const qlabel = document.createElement("div");
        qlabel.className = "uploader-label";
        const chev = document.createElement("span");
        chev.className = "u-chevron";
        chev.textContent = "▼";
        const txt = document.createElement("span");
        txt.style.flex = "1";
        txt.style.minWidth = "0";
        txt.textContent = (qg.label || qg.quality) + " (" + qg.episode_count + ")";
        qlabel.append(chev, txt);
        qGroup.appendChild(qlabel);
        const qBody = document.createElement("div");
        qBody.className = "uploader-body";

        for (const s of qg.seasons) {
          const slabel = document.createElement("div");
          slabel.className = "season-label";
          slabel.textContent = "Season " + (s.season || "?");
          qBody.appendChild(slabel);
          const eps = userSorted ? sortRows(s.episodes) : s.episodes;
          for (const ep of eps) {
            qBody.appendChild(seriesEpisodeRow(ep, [ep.series, ep.se, enc.name, qg.label || qg.quality]));
          }
        }
        qGroup.appendChild(qBody);
        body.appendChild(qGroup);
        makeAccordion(qGroup, qlabel, body, ".uploader-group");
      }
      section.append(header, body);
      container.appendChild(section);
      makeAccordion(section, header, container, ".encoder-section");
    }
  }
