  // ============================ TRAILERS MODULE ============================

  const TRAILERS_API = "/api/trailers";
  const TRAILERS_STATUS_API = "/api/trailers/status";
  const TRAILERS_REFRESH_API = "/api/trailers/refresh";

  // Inject styles (spinner + mobile horizontal layout)
  if (!document.getElementById("trailerSpinnerStyle")) {
    const style = document.createElement("style");
    style.id = "trailerSpinnerStyle";
    style.textContent = `
      @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
      @media (max-width: 640px) {
        .trailer-grid { display: flex !important; flex-direction: column !important; gap: 12px !important; }
        .trailer-card { display: flex !important; flex-direction: row !important; gap: 12px !important; align-items: flex-start !important; background: rgba(22,27,34,0.6) !important; border-radius: 12px !important; border: 1px solid rgba(255,255,255,0.08) !important; overflow: hidden !important; padding: 0 !important; }
        .trailer-thumb { width: 120px !important; min-width: 120px !important; height: auto !important; aspect-ratio: 16/9 !important; border-radius: 0 !important; overflow: hidden !important; position: relative !important; flex-shrink: 0 !important; }
        .trailer-thumb img { width: 100% !important; height: 100% !important; object-fit: cover !important; display: block !important; }
        .trailer-play { position: absolute !important; inset: 0 !important; display: flex !important; align-items: center !important; justify-content: center !important; background: rgba(0,0,0,0.4) !important; color: #fff !important; font-size: 24px !important; pointer-events: none !important; }
        .trailer-info { flex: 1 !important; min-width: 0 !important; display: flex !important; flex-direction: column !important; justify-content: center !important; padding: 8px 12px 8px 0 !important; }
        .trailer-title { font-size: 13px !important; font-weight: 600 !important; color: #f0f6fc !important; line-height: 1.35 !important; overflow: hidden !important; display: -webkit-box !important; -webkit-line-clamp: 2 !important; -webkit-box-orient: vertical !important; }
        .trailer-channel { font-size: 11px !important; color: #8b949e !important; margin-top: 4px !important; }
      }
    `;
    document.head.appendChild(style);
  }

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
    // iso may be "2026-06-17" or "2026-06-17T10:00:00+00:00"
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
    return `
      <div class="trailer-card" data-title="${esc(movie.title)}" data-vid="${esc(main.id)}">
        <div class="trailer-thumb" data-vid="${esc(main.id)}">
          <img src="${esc(main.thumbnail)}" alt="${esc(movie.title)}" loading="lazy" onerror="this.onerror=null;this.src='https://via.placeholder.com/480x270/161B22/8B949E?text=No+Thumbnail';">
          <div class="trailer-play">▶</div>
        </div>
        <div class="trailer-info">
          <div class="trailer-title" title="${esc(movie.title)}">${esc(movie.title)}</div>
          <div class="trailer-channel">${esc(main.channel)}</div>
        </div>
      </div>
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

    // Wire clicks on cards (both thumb and info area)
    container.querySelectorAll(".trailer-card").forEach(el => {
      el.addEventListener("click", (e) => {
        const vid = el.dataset.vid;
        const title = el.dataset.title || "";
        if (!vid) return;
        // On mobile, open directly in YouTube app or browser
        if (window.innerWidth <= 768 || /Android|iPhone|iPad|iPod/.test(navigator.userAgent)) {
          window.open(`https://www.youtube.com/watch?v=${vid}`, "_blank", "noopener,noreferrer");
          return;
        }
        openTrailerModal(vid, title);
      });
    });
  }

  function openTrailerModal(videoId, title) {
    let ov = _trailers$("trailerModalOverlay");
    if (!ov) {
      ov = document.createElement("div");
      ov.id = "trailerModalOverlay";
      ov.className = "overlay";
      ov.innerHTML = `
        <div class="modal-panel" style="max-width: 900px; padding: 0; overflow: hidden;">
          <div class="panel-head" style="border-radius: 14px 14px 0 0;">
            <div style="flex:1; min-width:0;">
              <h2 id="trailerModalTitle" class="truncate" style="font-size:16px;">${esc(title || "Trailer")}</h2>
            </div>
            <button id="closeTrailerModal" class="ghost" type="button" aria-label="Close trailer">✕</button>
          </div>
          <div class="panel-body" style="padding:0;">
            <div id="trailerEmbedContainer" style="width:100%; aspect-ratio:16/9; background:#000; position:relative; min-height:200px;">
              <!-- iframe injected here -->
            </div>
            <div style="padding:10px 16px; text-align:center; border-top:1px solid rgba(255,255,255,0.08);">
              <a id="trailerFallbackLink" href="#" target="_blank" rel="noopener noreferrer" style="color:#58a6ff; font-size:13px; text-decoration:none;">Open on YouTube ↗</a>
            </div>
          </div>
        </div>
      `;
      document.body.appendChild(ov);
      ov.addEventListener("click", (e) => { if (e.target === ov) closeTrailerModal(); });
      _trailers$("closeTrailerModal")?.addEventListener("click", closeTrailerModal);
    }

    const fallbackLink = _trailers$("trailerFallbackLink");
    if (fallbackLink) {
      fallbackLink.href = `https://www.youtube.com/watch?v=${esc(videoId)}`;
    }

    const container = _trailers$("trailerEmbedContainer");
    if (container) container.innerHTML = "";

    ov.classList.remove("hidden");

    requestAnimationFrame(() => {
      if (!container) return;

      const iframe = document.createElement("iframe");
      iframe.style.cssText = "width:100%; height:100%; border:0; display:block; position:absolute; top:0; left:0; right:0; bottom:0;";
      iframe.allow = "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; fullscreen";
      iframe.allowFullscreen = true;
      iframe.title = title || "YouTube video player";
      iframe.loading = "eager";
      iframe.src = `https://www.youtube.com/embed/${esc(videoId)}?rel=0&modestbranding=1&autoplay=1`;
      container.appendChild(iframe);
    });
  }

  function closeTrailerModal() {
    const ov = _trailers$("trailerModalOverlay");
    if (!ov) return;
    ov.classList.add("hidden");
    const container = _trailers$("trailerEmbedContainer");
    if (container) container.innerHTML = "";
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
  window.openTrailerModal = openTrailerModal;
  window.closeTrailerModal = closeTrailerModal;
  window.refreshTrailers = refreshTrailers;
