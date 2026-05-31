  /* ===== Series Mode v2 ===== */
  let seriesMode = false;

  function getSelectedQualities() {
    return Array.from(document.querySelectorAll(".qualityOpt:checked")).map(c => c.value);
  }
  function getSelectedEncoders() {
    return Array.from(document.querySelectorAll(".encoderOpt:checked")).map(c => c.value);
  }

  // Quota guard mirror (must match backend: packs=2/quality, encoders=N*Q, cap 12).
  const SERIES_MAX_REQUESTS = 12;
  function updateQuotaBadge() {
    const badge = $("quotaBadge");
    if (!badge) return;
    const q = getSelectedQualities().length || 1;
    const n = getSelectedEncoders().length;
    const planned = (2 * q) + (n * q);
    badge.textContent = "This search uses " + planned + " request(s)";
    badge.classList.toggle("over", planned > SERIES_MAX_REQUESTS);
  }

  function setSeriesMode(on) {
    seriesMode = !!on;
    const nBtn = $("modeNormal"), sBtn = $("modeSeries");
    if (nBtn) nBtn.classList.toggle("active", !seriesMode);
    if (sBtn) sBtn.classList.toggle("active", seriesMode);
    const ctrls = $("seriesControls");
    if (ctrls) ctrls.classList.toggle("hidden", !seriesMode);
    updateQuotaBadge();
    // Just swap visible container; do NOT auto-fetch (series fetch costs quota).
    $("results").classList.toggle("hidden", seriesMode);
    $("seriesResults").classList.toggle("hidden", !seriesMode);
    $("pagination").classList.add("hidden");
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
    // opts: {title, sub, count, episodes?, extraClass}
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

    // --- Season Packs on top (smallest-first) ---
    if (packs.length) {
      const section = document.createElement("div");
      section.className = "encoder-section packs";
      const header = sectionHeader({
        title: "\uD83D\uDCE6 Season Packs",
        sub: "complete seasons \u00b7 smallest first",
        count: packs.length + (packs.length === 1 ? " pack" : " packs"),
      });
      header.addEventListener("click", () => section.classList.toggle("collapsed"));
      const body = document.createElement("div");
      body.className = "encoder-body";
      for (const p of packs) body.appendChild(seriesEpisodeRow(p, [p.pack_label || p.name, p.uploader]));
      section.append(header, body);
      container.appendChild(section);
    }

    // --- Encoder → Uploader → Quality → Season → Episode ---
    for (const enc of encoders) {
      const section = document.createElement("div");
      section.className = "encoder-section";
      const allEps = (enc.uploaders || []).flatMap(u => u.seasons.flatMap(s => s.episodes));
      const header = sectionHeader({
        title: enc.name,
        sub: (enc.uploaders || []).length + " uploader(s)",
        count: enc.episode_count + (enc.episode_count === 1 ? " episode" : " episodes"),
        episodes: allEps,
      });
      header.addEventListener("click", () => section.classList.toggle("collapsed"));
      const body = document.createElement("div");
      body.className = "encoder-body";

      for (const up of enc.uploaders || []) {
        const ulabel = document.createElement("div");
        ulabel.className = "uploader-label";
        const upEps = up.seasons.flatMap(s => s.episodes);
        ulabel.innerHTML = "";
        const txt = document.createElement("span");
        txt.textContent = "\u21B3 " + up.name + " \u00b7 " + up.quality + " (" + up.episode_count + ")";
        ulabel.appendChild(txt);
        const addAllUp = document.createElement("button");
        addAllUp.type = "button";
        addAllUp.className = "section-add sm";
        addAllUp.textContent = "+ Add all " + upEps.length;
        addAllUp.addEventListener("click", (e) => { e.stopPropagation(); addAllEpisodes(upEps, addAllUp); });
        ulabel.appendChild(addAllUp);
        body.appendChild(ulabel);

        for (const s of up.seasons) {
          const slabel = document.createElement("div");
          slabel.className = "season-label";
          slabel.textContent = "Season " + (s.season || "?");
          body.appendChild(slabel);
          for (const ep of s.episodes) {
            body.appendChild(seriesEpisodeRow(ep, [ep.series, ep.se, enc.name, up.quality]));
          }
        }
      }
      section.append(header, body);
      container.appendChild(section);
    }
  }
