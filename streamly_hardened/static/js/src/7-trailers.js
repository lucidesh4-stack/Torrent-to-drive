  // ============================ TRAILERS MODULE ============================

  const TRAILERS_API = "/api/trailers";
  const TRAILERS_STATUS_API = "/api/trailers/status";
  const TRAILERS_REFRESH_API = "/api/trailers/refresh";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function _trailers$(id) { return document.getElementById(id); }

  function _trailersFormatDate(iso) {
    const datePart = (iso || "").split("T")[0];
    if (!datePart) return "";
    const d = new Date(datePart + "T00:00:00");
    if (isNaN(d.getTime())) return "";
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  }

  function _trailersTimeAgo(ts) {
    if (!ts) return "Never";
    const diff = Math.floor((Date.now() - ts * 1000) / 60000);
    if (diff < 1) return "Just now";
    if (diff < 60) return `${diff}m ago`;
    if (diff < 1440) return `${Math.floor(diff / 60)}h ago`;
    return `${Math.floor(diff / 1440)}d ago`;
  }

  function _isNewVideo(publishedIso) {
    if (!publishedIso) return false;
    const d = new Date(publishedIso);
    if (isNaN(d.getTime())) return false;
    return (Date.now() - d.getTime()) < (24 * 60 * 60 * 1000); // 24 hours
  }

  async function _trailersPostJson(url, body) {
    if (typeof postJson === "function") {
      return postJson(url, body);
    }
    const token = (document.querySelector('meta[name="csrf-token"]')?.content || window.csrfToken || "");
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
      credentials: "same-origin",
      body: JSON.stringify(body || {})
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(text || `${res.status}`);
    }
    return res.json();
  }

  async function _trailersFetch() {
    const res = await fetch(TRAILERS_API, { credentials: "same-origin" });
    if (!res.ok) throw new Error(`${res.status}`);
    return res.json();
  }

  async function _trailersFetchStatus() {
    try {
      const res = await fetch(TRAILERS_STATUS_API, { credentials: "same-origin" });
      if (!res.ok) return null;
      return res.json();
    } catch (e) { return null; }
  }

  function _trailersRenderCard(movie) {
    const main = movie.videos[0];
    if (!main) return "";
    const isNew = _isNewVideo(main.published);
    return `
      <a class="trailer-card" href="${esc(main.url)}" target="_blank" rel="noopener noreferrer" data-title="${esc(movie.title)}">
        <div class="trailer-thumb">
          <img src="${esc(main.thumbnail)}" alt="${esc(movie.title)}" loading="lazy" onerror="this.onerror=null;this.src='https://via.placeholder.com/480x270/161B22/8B949E?text=No+Thumbnail';">
          <div class="trailer-play">▶</div>
          ${isNew ? '<span class="trailer-new">NEW</span>' : ''}
        </div>
        <div class="trailer-info">
          <div class="trailer-title" title="${esc(movie.title)}">${esc(movie.title)}</div>
          <div class="trailer-channel">${esc(main.channel)}</div>
        </div>
      </a>
    `;
  }

  function _trailersRender(data) {
    const container = _trailers$("trailersContainer");
    if (!container) return;

    if (!data || !data.items || !data.items.length) {
      container.innerHTML = `<div class="empty">No trailers in the last 30 days. The feed refreshes automatically every 10 minutes.</div>`;
      return;
    }

    const html = data.items.map(day => {
      const dateStr = _trailersFormatDate(day.date);
      const cards = day.items.map(_trailersRenderCard).join("");
      return dateStr
        ? `<div class="trailer-day"><h3 class="trailer-date">${esc(dateStr)}</h3><div class="trailer-grid">${cards}</div></div>`
        : `<div class="trailer-day"><div class="trailer-grid">${cards}</div></div>`;
    }).join("");

    container.innerHTML = html;
  }

  // --- Refresh / status ---

  function _trailersEnsureHeader() {
    let header = _trailers$("trailerHeader");
    if (!header) {
      const view = _trailers$("trailersView");
      if (!view) return;
      header = document.createElement("div");
      header.id = "trailerHeader";
      header.style.cssText = "display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid rgba(255,255,255,0.08);";
      header.innerHTML = `
        <div>
          <h2 style="font-size:16px;font-weight:700;margin:0;color:#f0f6fc;">Latest Trailers</h2>
          <div id="trailerStatusText" style="font-size:11px;color:#8b949e;margin-top:2px;">Loading status…</div>
        </div>
        <button id="trailersRefreshBtn" class="ghost" type="button" aria-label="Refresh trailers" style="display:flex;align-items:center;gap:6px;">
          <svg id="trailersRefreshIcon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-refresh-cw"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M3 21v-5h5"/></svg>
          <span>Refresh</span>
        </button>
      `;
      view.insertBefore(header, view.firstChild);
      _trailers$("trailersRefreshBtn")?.addEventListener("click", refreshTrailers);
    }
  }

  async function _trailersUpdateStatusText() {
    const statusText = _trailers$("trailerStatusText");
    if (!statusText) return;
    const status = await _trailersFetchStatus();
    if (!status) {
      statusText.textContent = "Status unavailable";
      return;
    }
    if (status.running) {
      statusText.textContent = "Checking for new trailers…";
      return;
    }
    const ago = status.last_crawl ? _trailersTimeAgo(status.last_crawl) : "Never";
    statusText.textContent = `Last updated ${ago}`;
  }

  async function refreshTrailers() {
    const btn = _trailers$("trailersRefreshBtn");
    const icon = _trailers$("trailersRefreshIcon");
    const container = _trailers$("trailersContainer");
    const statusText = _trailers$("trailerStatusText");

    if (btn) btn.disabled = true;
    if (icon) icon.style.animation = "spin 1s linear infinite";
    if (container) container.innerHTML = `<div class="status">Checking for new trailers… This may take up to 2 minutes.</div>`;

    try {
      const data = await _trailersPostJson(TRAILERS_REFRESH_API, {});

      if (data.status === "started" || data.status === "running") {
        if (statusText) statusText.textContent = "Checking for new trailers…";

        let attempts = 0;
        const poll = setInterval(async () => {
          attempts++;
          try {
            const status = await _trailersFetchStatus();
            if (status && !status.running && status.last_crawl) {
              const feed = await _trailersFetch();
              if (feed.items && feed.items.length > 0) {
                clearInterval(poll);
                _trailersRender(feed);
                if (icon) icon.style.animation = "";
                if (btn) btn.disabled = false;
                if (statusText) statusText.textContent = `Last updated ${_trailersTimeAgo(status.last_crawl)}`;
                return;
              }
            }
            const feed = await _trailersFetch();
            if (feed.items && feed.items.length > 0) {
              clearInterval(poll);
              _trailersRender(feed);
              if (icon) icon.style.animation = "";
              if (btn) btn.disabled = false;
              if (statusText) statusText.textContent = `Last updated ${_trailersTimeAgo(status.last_crawl || Date.now()/1000)}`;
              return;
            }
          } catch (e) {}

          if (attempts >= 24) { // 2 minutes
            clearInterval(poll);
            if (container) container.innerHTML = `<div class="status">Refresh timed out. The feed updates automatically every 10 minutes. Please check back later.</div>`;
            if (icon) icon.style.animation = "";
            if (btn) btn.disabled = false;
            _trailersUpdateStatusText();
          }
        }, 5000);
      } else {
        if (container) container.innerHTML = `<div class="status">Refresh failed: ${esc(data.message)}</div>`;
        if (icon) icon.style.animation = "";
        if (btn) btn.disabled = false;
      }
    } catch (e) {
      if (container) container.innerHTML = `<div class="status">Refresh failed: ${esc(e.message)}</div>`;
      if (icon) icon.style.animation = "";
      if (btn) btn.disabled = false;
    }
  }

  async function loadTrailers() {
    const container = _trailers$("trailersContainer");
    if (!container) return;
    container.innerHTML = `<div class="status">Loading latest trailers…</div>`;
    try {
      const [data, status] = await Promise.all([_trailersFetch(), _trailersFetchStatus()]);
      _trailersRender(data);
      const statusText = _trailers$("trailerStatusText");
      if (statusText && status) {
        if (status.running) {
          statusText.textContent = "Checking for new trailers…";
        } else {
          statusText.textContent = `Last updated ${status.last_crawl ? _trailersTimeAgo(status.last_crawl) : "Never"}`;
          // Auto-trigger refresh if feed is stale (>2 hours old)
          if (status.is_stale) {
            refreshTrailers();
          }
        }
      }
    } catch (e) {
      container.innerHTML = `<div class="status">Failed to load trailers. <button class="ghost" onclick="loadTrailers()">Retry</button></div>`;
    }
  }

  // Tab helper (call from 6-main.js tab switcher)
  function setTrailersTab() {
    const view = _trailers$("trailersView");
    const cloud = _trailers$("cloudView");
    const search = _trailers$("searchView");
    if (view) view.classList.remove("hidden");
    if (cloud) cloud.classList.add("hidden");
    if (search) search.classList.add("hidden");
    if (typeof window.updateBottomNavHighlight === "function") {
      window.updateBottomNavHighlight(4);
    }
    _trailersEnsureHeader();
    loadTrailers();
  }

  // Expose
  window.loadTrailers = loadTrailers;
  window.setTrailersTab = setTrailersTab;
  window.refreshTrailers = refreshTrailers;
