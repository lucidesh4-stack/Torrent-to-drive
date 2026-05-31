  /* ===== Series Mode ===== */
  let seriesMode = false;

  function setSeriesMode(on) {
    seriesMode = !!on;
    const nBtn = $("modeNormal"), sBtn = $("modeSeries");
    if (nBtn) nBtn.classList.toggle("active", !seriesMode);
    if (sBtn) sBtn.classList.toggle("active", seriesMode);
    // Re-run only if a query is present and results are already shown.
    if ($("searchQuery").value.trim() && !$("results").classList.contains("hidden") || (seriesMode && $("searchQuery").value.trim())) {
      search(false, 1);
    } else {
      // Just swap which container is visible.
      $("results").classList.toggle("hidden", seriesMode);
      $("seriesResults").classList.toggle("hidden", !seriesMode);
    }
  }

  function seriesEpisodeRow(row) {
    const wrap = document.createElement("div");
    wrap.className = "episode-row";

    const name = document.createElement("span");
    name.className = "name truncate";
    const label = [row.series, row.se, row._encoder, row._quality].filter(Boolean).join(" · ");
    name.textContent = label || row.name || "Untitled";
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

  function buildSection(opts) {
    // opts: {title, quality, count, rows?, seasons?, extraClass, withAddAll}
    const section = document.createElement("div");
    section.className = "encoder-section" + (opts.extraClass ? " " + opts.extraClass : "");

    const header = document.createElement("div");
    header.className = "encoder-header";
    header.addEventListener("click", () => section.classList.toggle("collapsed"));

    const titleWrap = document.createElement("div");
    titleWrap.className = "encoder-title";
    const chevron = document.createElement("span");
    chevron.className = "chevron";
    chevron.textContent = "\u25BC";
    const nameEl = document.createElement("span");
    nameEl.className = "encoder-name";
    nameEl.textContent = opts.title;
    titleWrap.append(chevron, nameEl);
    if (opts.quality) {
      const q = document.createElement("span");
      q.className = "encoder-quality";
      q.textContent = "\u2014 " + opts.quality;
      titleWrap.appendChild(q);
    }
    const countEl = document.createElement("span");
    countEl.className = "encoder-count";
    countEl.textContent = opts.count;
    titleWrap.appendChild(countEl);
    header.appendChild(titleWrap);

    if (opts.withAddAll && opts.episodes && opts.episodes.length) {
      const addAll = document.createElement("button");
      addAll.type = "button";
      addAll.className = "section-add";
      addAll.textContent = "+ Add all " + opts.episodes.length;
      addAll.addEventListener("click", (e) => {
        e.stopPropagation();
        addAllEpisodes(opts.episodes, addAll);
      });
      header.appendChild(addAll);
    }

    const body = document.createElement("div");
    body.className = "encoder-body";

    if (opts.seasons) {
      for (const season of opts.seasons) {
        const slabel = document.createElement("div");
        slabel.className = "season-label";
        slabel.textContent = "Season " + (season.season || "?");
        body.appendChild(slabel);
        for (const ep of season.episodes) body.appendChild(seriesEpisodeRow(ep));
      }
    } else if (opts.rows) {
      for (const r of opts.rows) body.appendChild(seriesEpisodeRow(r));
    }

    section.append(header, body);
    return section;
  }

  // "Add all N": add ONLY the first episode to Seedr, save ALL episodes to History.
  async function addAllEpisodes(episodes, btn) {
    if (!episodes || !episodes.length) return;
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = "Adding...";
    try {
      for (const ep of episodes) {
        saveToHistory(ep.magnet, ep.name); // every episode goes to history
      }
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

  function renderSeriesGrouped(groups) {
    const container = $("seriesResults");
    container.textContent = "";
    if (!groups) return;

    const stats = groups.stats || {};
    const total = (stats.parsed || 0) + (stats.packs || 0) + (stats.other || 0);
    if (!total) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No results to group.";
      container.appendChild(empty);
      return;
    }

    // Encoder sections (with Add all)
    for (const enc of groups.encoders || []) {
      // stamp encoder/quality onto each episode for the row label
      for (const s of enc.seasons || []) {
        for (const ep of s.episodes) { ep._encoder = enc.name; ep._quality = enc.quality; }
      }
      const episodes = (enc.seasons || []).flatMap(s => s.episodes);
      container.appendChild(buildSection({
        title: enc.name,
        quality: enc.quality,
        count: enc.episode_count + (enc.episode_count === 1 ? " episode" : " episodes"),
        seasons: enc.seasons,
        episodes: episodes,
        withAddAll: true,
      }));
    }

    // Season Packs (per-row Add only)
    if ((groups.packs || []).length) {
      container.appendChild(buildSection({
        title: "\uD83D\uDCE6 Season Packs",
        quality: "complete seasons",
        count: groups.packs.length + (groups.packs.length === 1 ? " pack" : " packs"),
        rows: groups.packs,
        extraClass: "packs",
        withAddAll: false,
      }));
    }

    // Other (per-row Add only)
    if ((groups.other || []).length) {
      container.appendChild(buildSection({
        title: "\uD83D\uDDC2\uFE0F Other",
        quality: "couldn't parse encoder/episode",
        count: groups.other.length + " result" + (groups.other.length === 1 ? "" : "s"),
        rows: groups.other,
        extraClass: "other",
        withAddAll: false,
      }));
    }
  }
