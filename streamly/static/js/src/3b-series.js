  /* ===== Series Mode v2 + Normal grouped ===== */
  window.seriesMode = false;
  // Holds the last rendered dataset so client-side sorting can re-order without re-fetching.
  window.lastNormalGroups = null;   // [{quality,label,count,rows}]
  window.lastSeriesData = null;     // {packs, encoders, ...}
  // True once the user clicks a column header; until then Series keeps its native
  // S/E order (Normal always uses size-asc default regardless).
  window.userSorted = false;
  window.activeNormalQuality = "";
  window.activeSeriesQuality = "";
  window.activeSeriesSeason = Object.create(null);

  window.isMobileSearchUi = function() {
    return window.matchMedia && window.matchMedia("(max-width: 768px)").matches;
  }

  window.qualityLabel = function(q) {
    return ({ "2160p": "4K", "1080p": "1080p", "720p": "720p", "Other": "Other" })[q] || q || "Other";
  }

  window.qualityBucketFromName = function(name) {
    const m = String(name || "").match(/(?:^|[^0-9])(2160p|1080p|720p)(?:[^0-9]|$)/i);
    return m ? m[1].toLowerCase() : "Other";
  }

  window.normalizeQualityList = function(list) {
    const order = ["2160p", "1080p", "720p", "Other"];
    const set = new Set((list || []).filter(Boolean));
    return order.filter(q => set.has(q));
  }

  window.chooseActiveQuality = function(available, current) {
    const qs = normalizeQualityList(available);
    if (!qs.length) return "";
    if (current && qs.includes(current)) return current;
    const selected = getSelectedQualities();
    for (const q of selected) if (qs.includes(q)) return q;
    if (qs.includes("1080p")) return "1080p";
    return qs[0];
  }

  window.mobileQualityNav = function(available, active, onPick) {
    const qs = normalizeQualityList(available);
    if (!qs.length) return null;
    const nav = document.createElement("div");
    nav.className = "mobile-quality-nav";
    for (const q of qs) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "mobile-quality-tab" + (q === active ? " active" : "");
      btn.textContent = qualityLabel(q);
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (btn.classList.contains("active")) return;
        nav.querySelectorAll(".mobile-quality-tab").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        onPick(q);
      });
      nav.appendChild(btn);
    }
    return nav;
  }

  window.openSectionKeys = function(container) {
    if (!container) return new Set();
    return new Set(Array.from(container.querySelectorAll(":scope > .encoder-section:not(.collapsed)[data-acc-key]")).map(el => el.dataset.accKey));
  }

  window.applyOpenState = function(section, key, openKeys) {
    section.dataset.accKey = key;
    if (openKeys && openKeys.has(key)) section.classList.remove("collapsed");
  }

  window.getSelectedQualities = function() {
    const sel = isMobileSearchUi() ? ".mQualityOpt:checked" : ".qualityOpt:checked";
    const values = Array.from(document.querySelectorAll(sel)).map(c => c.value);
    return values.length ? values : Array.from(document.querySelectorAll(".qualityOpt:checked")).map(c => c.value);
  }
  window.getSelectedEncoders = function() {
    const sel = isMobileSearchUi() ? ".mEncoderOpt:checked" : ".encoderOpt:checked";
    return Array.from(document.querySelectorAll(sel)).map(c => c.value);
  }

  window.updateDropdownLabels = function() {
    const qs = getSelectedQualities();
    const qLabelMap = { "2160p": "4K", "1080p": "1080p", "720p": "720p" };
    const qBtn = $("qualityDdBtn");
    if (qBtn) qBtn.textContent = "Quality: " + (qs.length ? qs.map(x => qLabelMap[x] || x).join(", ") : "none");
    const es = getSelectedEncoders();
    const eBtn = $("encoderDdBtn");
    if (eBtn) eBtn.textContent = "Encoders: " + (es.length ? (es.length <= 2 ? es.join(", ") : es.length + " selected") : "none");
  }

  window.setSeriesMode = function(on) {
    seriesMode = !!on;
    const nBtn = $("modeNormal"), sBtn = $("modeSeries");
    if (nBtn) nBtn.classList.toggle("active", !seriesMode);
    if (sBtn) sBtn.classList.toggle("active", seriesMode);
    // The control row (Quality/Encoder dropdowns) stays visible in BOTH modes.
    // Toggling only changes how the backend processes the next search.
    updateDropdownLabels();
    $("seriesResults").classList.add("hidden");
  }

  // ---- Client-side sort state (re-orders loaded rows; no re-fetch) ----
  window.sortRows = function(rows) {
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
  window.seriesHeaderRow = function() {
    return document.createDocumentFragment();
  }

  window.makeAccordion = function(section, header, container, groupSel) {
    header.addEventListener("click", (e) => {
      if (e.target.closest("button")) return; // ignore Add-all clicks
      const wasCollapsed = section.classList.contains("collapsed");
      container.querySelectorAll(":scope > " + groupSel).forEach((s) => s.classList.add("collapsed"));
      if (wasCollapsed) section.classList.remove("collapsed");
    });
  }

  window.plainRow = function(row) {
    const wrap = document.createElement("div");
    wrap.className = "episode-row";

    const content = document.createElement("div");
    content.className = "row-content";

    const title = document.createElement("div");
    title.className = "row-title";
    title.textContent = row.name || "Untitled";
    title.title = row.name || "";

    const meta = document.createElement("div");
    meta.className = "row-meta";

    const seeds = Number(row.seeds || 0);
    const dotColor = seeds >= 50 ? "seed-green" : (seeds >= 10 ? "seed-amber" : "seed-red");
    const dot = document.createElement("span");
    dot.className = `seed-dot ${dotColor}`;
    dot.textContent = "●";

    const seedsText = document.createElement("span");
    seedsText.className = "meta-seeds";
    seedsText.textContent = `${seeds} seeds`;

    meta.append(dot, seedsText);

    function addSep() {
      const sep = document.createElement("span");
      sep.className = "meta-sep";
      sep.textContent = " · ";
      meta.appendChild(sep);
    }

    if (row.size && row.size !== "-") {
      addSep();
      const s = document.createElement("span");
      s.textContent = row.size;
      meta.appendChild(s);
    }
    if (row.encoder && row.encoder !== "-") {
      addSep();
      const e = document.createElement("span");
      e.textContent = row.encoder;
      meta.appendChild(e);
    }
    if (row.date && row.date !== "-") {
      addSep();
      const d = document.createElement("span");
      d.textContent = row.date;
      meta.appendChild(d);
    }

    content.append(title, meta);

    const action = document.createElement("div");
    action.className = "row-action";
    action.appendChild(makeAddButton(row));

    wrap.append(content, action);
    return wrap;
  }

  // Normal mode: render quality sections. On mobile, quality tabs navigate one
  // quality at a time; desktop keeps the existing accordion sections.
  window.renderNormalGrouped = function(groups) {
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

    const fragment = document.createDocumentFragment();
    fragment.appendChild(seriesHeaderRow());

    const primaryGroups = lastNormalGroups.filter(g => g.quality !== "less_relevant");
    const lessGroup = lastNormalGroups.find(g => g.quality === "less_relevant");
    const available = primaryGroups.map(g => g.quality);
    activeNormalQuality = chooseActiveQuality(available, activeNormalQuality);
    const nav = mobileQualityNav(available, activeNormalQuality, (q) => {
      activeNormalQuality = q;
      setTimeout(() => {
        renderNormalGrouped(lastNormalGroups);
      }, 0);
    });
    if (nav) fragment.appendChild(nav);

    const active = primaryGroups.find(g => g.quality === activeNormalQuality) || primaryGroups[0];
    if (active) {
      for (const r of sortRows(active.rows || [])) {
        fragment.appendChild(plainRow(r));
      }
    }

    if (lessGroup && (lessGroup.rows || []).length) {
      const section = document.createElement("div");
      section.className = "encoder-section collapsed";
      applyOpenState(section, "normal:less_relevant", prevOpen);
      const header = sectionHeader({
        title: lessGroup.label || "Less relevant",
        sub: null,
        count: lessGroup.count + (lessGroup.count === 1 ? " result" : " results"),
      });
      const body = document.createElement("div");
      body.className = "encoder-body";
      for (const r of sortRows(lessGroup.rows || [])) {
        body.appendChild(plainRow(r));
      }
      section.append(header, body);
      fragment.appendChild(section);
    }

    container.appendChild(fragment);

    // Call accordion wiring after container has the elements
    const sections = container.querySelectorAll(".encoder-section");
    sections.forEach(sec => {
      const header = sec.querySelector(".encoder-header");
      if (header) {
        makeAccordion(sec, header, container, ".encoder-section");
      }
    });
  }

  window.seriesEpisodeRow = function(row, labelParts) {
    const wrap = document.createElement("div");
    wrap.className = "episode-row";

    const content = document.createElement("div");
    content.className = "row-content";

    const title = document.createElement("div");
    title.className = "row-title";
    title.textContent = (labelParts || [row.name]).filter(Boolean).join(" · ") || row.name || "Untitled";
    title.title = row.name || "";

    const meta = document.createElement("div");
    meta.className = "row-meta";

    const seeds = Number(row.seeds || 0);
    const dotColor = seeds >= 50 ? "seed-green" : (seeds >= 10 ? "seed-amber" : "seed-red");
    const dot = document.createElement("span");
    dot.className = `seed-dot ${dotColor}`;
    dot.textContent = "●";

    const seedsText = document.createElement("span");
    seedsText.className = "meta-seeds";
    seedsText.textContent = `${seeds} seeds`;

    meta.append(dot, seedsText);

    function addSep() {
      const sep = document.createElement("span");
      sep.className = "meta-sep";
      sep.textContent = " · ";
      meta.appendChild(sep);
    }

    if (row.size && row.size !== "-") {
      addSep();
      const s = document.createElement("span");
      s.textContent = row.size;
      meta.appendChild(s);
    }
    if (row.encoder && row.encoder !== "-") {
      addSep();
      const e = document.createElement("span");
      e.textContent = row.encoder;
      meta.appendChild(e);
    }
    if (row.date && row.date !== "-") {
      addSep();
      const d = document.createElement("span");
      d.textContent = row.date;
      meta.appendChild(d);
    }

    content.append(title, meta);

    const action = document.createElement("div");
    action.className = "row-action";
    action.appendChild(makeAddButton(row));

    wrap.append(content, action);
    return wrap;
  }

  window.sectionHeader = function(opts) {
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

  window.renderSeriesGrouped = function(data) {
    lastSeriesData = data || null;
    const container = $("seriesResults");
    const prevOpen = openSectionKeys(container);
    container.textContent = "";
    if (!data) return;

    const packs = data.packs || [];
    const encoders = data.encoders || [];
    const lessRelevant = data.less_relevant || [];
    const otherRows = data.other || [];

    if (!packs.length && !encoders.length && !lessRelevant.length && !otherRows.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No grouped results. Try different quality/encoder selections.";
      container.appendChild(empty);
      return;
    }

    const fragment = document.createDocumentFragment();
    fragment.appendChild(seriesHeaderRow());

    const mobile = isMobileSearchUi();
    const available = [];
    for (const p of packs) available.push(qualityBucketFromName(p.name));
    for (const enc of encoders) for (const qg of (enc.qualities || [])) available.push(qg.quality);
    activeSeriesQuality = chooseActiveQuality(available, activeSeriesQuality);

    // Renders the global Quality chips on both desktop and mobile
    const nav = mobileQualityNav(available, activeSeriesQuality, (q) => {
      activeSeriesQuality = q;
      setTimeout(() => {
        renderSeriesGrouped(lastSeriesData);
      }, 0);
    });
    if (nav) fragment.appendChild(nav);

    // Both desktop and mobile now filter packs by the active quality chip
    const packsToShow = packs.filter(p => qualityBucketFromName(p.name) === activeSeriesQuality);
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
      const displayPacks = userSorted ? sortRows(packsToShow) : packsToShow;
      for (const p of displayPacks) body.appendChild(seriesEpisodeRow(p, [p.name]));
      section.append(header, body);
      fragment.appendChild(section);
    }

    for (const enc of encoders) {
      // Both desktop and mobile now filter qualities by activeSeriesQuality
      const qualityGroups = (enc.qualities || []).filter(qg => qg.quality === activeSeriesQuality);
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
          const seasons = qg.seasons || [];
          if (seasons.length) {
            const skey = enc.encoder_norm + ":" + qg.quality;
            const availableSeasons = seasons.map(s => s.season);
            if (!activeSeriesSeason[skey] || !availableSeasons.includes(activeSeriesSeason[skey])) {
              activeSeriesSeason[skey] = availableSeasons[0];
            }
            const sNav = document.createElement("div");
            sNav.className = "mobile-season-nav";
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
              sNav.appendChild(btn);
            }
            body.appendChild(sNav);
            const activeSeason = seasons.find(s => s.season === activeSeriesSeason[skey]) || seasons[0];
            const eps = activeSeason ? (activeSeason.episodes || []) : [];
            for (const ep of eps) body.appendChild(seriesEpisodeRow(ep, [ep.se, qg.label || qg.quality]));
          }
          continue;
        }

        // Desktop
        for (const s of (qg.seasons || [])) {
          const slabel = document.createElement("div");
          slabel.className = "season-label";
          slabel.textContent = "Season " + (s.season || "?");
          body.appendChild(slabel);
          const eps = s.episodes || [];
          for (const ep of eps) {
            body.appendChild(seriesEpisodeRow(ep, [ep.series, ep.se, qg.label || qg.quality]));
          }
        }
      }
      section.append(header, body);
      fragment.appendChild(section);
    }

    function appendPlainSeriesSection(key, title, rows) {
      if (!rows || !rows.length) return;
      const section = document.createElement("div");
      section.className = "encoder-section other collapsed";
      applyOpenState(section, key, prevOpen);
      const header = sectionHeader({
        title,
        sub: null,
        count: rows.length + (rows.length === 1 ? " result" : " results"),
      });
      const body = document.createElement("div");
      body.className = "encoder-body";
      for (const row of rows) body.appendChild(plainRow(row));
      section.append(header, body);
      fragment.appendChild(section);
    }

    appendPlainSeriesSection("series:less_relevant", "Less relevant", lessRelevant);
    appendPlainSeriesSection("series:other", "Other / Unparsed", otherRows);

    container.appendChild(fragment);

    // Call accordion wiring after container has the elements
    const sections = container.querySelectorAll(".encoder-section");
    sections.forEach(sec => {
      const header = sec.querySelector(".encoder-header");
      if (header) {
        makeAccordion(sec, header, container, ".encoder-section");
      }

      // Wire internal uploader-group accordion on desktop
      if (!mobile) {
        const uploaderGroups = sec.querySelectorAll(".uploader-group");
        uploaderGroups.forEach(ug => {
          const uLabel = ug.querySelector(".uploader-label");
          if (uLabel) {
            makeAccordion(ug, uLabel, ug.parentNode, ".uploader-group");
          }
        });
      }
    });
  }
