  // ============================ TRAILERS MODULE ============================
  // Standalone: expects #trailersView and #trailersContainer in the DOM.
  // Wire a tab button to call setTrailersTab() (see 6-main.js integration).

  const TRAILERS_API = "/api/trailers";

  // Spinner CSS for refresh button (injected once)
  if (!document.getElementById("trailerSpinnerStyle")) {
    const style = document.createElement("style");
    style.id = "trailerSpinnerStyle";
    style.textContent = "@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }";
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
    const d = new Date(iso + "T00:00:00");
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  }

  function _trailersTimeAgo(iso) {
    const d = new Date(iso);
    const diff = Date.now() - d.getTime();
    const days = Math.floor(diff / 86400000);
    if (days < 1) return "Today";
    if (days === 1) return "Yesterday";
    if (days < 7) return `${days}d ago`;
    return _trailersFormatDate(iso);
  }

  async function _trailersPostJson(url, body) {
    if (typeof postJson === "function") {
      return postJson(url, body);
    }
    const token = (document.querySelector('meta[name="csrf-token"]')?.content || window.csrfToken || "");
    const res = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": token
      },
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

  function _trailersRenderBadge(video) {
    const type = video.type === "teaser" ? "Teaser" : (video.number > 0 ? `Trailer ${video.number}` : "Trailer");
    return `<span class="trailer-badge ${esc(video.type)}" data-vid="${esc(video.id)}">${esc(type)}</span>`;
  }

  function _trailersRenderCard(movie) {
    const main = movie.videos[0];
    if (!main) return "";
    const badges = movie.videos.map(_trailersRenderBadge).join("");
    return `
      <div class="trailer-card" data-title="${esc(movie.title)}">
        <div class="trailer-thumb" data-vid="${esc(main.id)}">
          <img src="${esc(main.thumbnail)}" alt="${esc(movie.title)}" loading="lazy">
          <div class="trailer-play">▶</div>
        </div>
        <div class="trailer-info">
          <div class="trailer-title" title="${esc(movie.title)}">${esc(movie.title)}</div>
          <div class="trailer-badges">${badges}</div>
          <div class="trailer-meta">
            <span class="trailer-channel">${esc(main.channel)}</span>
            <span class="trailer-when">${_trailersTimeAgo(main.published)}</span>
          </div>
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
      const cards = day.items.map(_trailersRenderCard).join("");
      return `
        <div class="trailer-day">
          <h3 class="trailer-date">${_trailersFormatDate(day.date)}</h3>
          <div class="trailer-grid">${cards}</div>
        </div>
      `;
    }).join("");

    container.innerHTML = html;

    // Wire click events (delegation)
    container.querySelectorAll(".trailer-thumb").forEach(el => {
      el.addEventListener("click", () => {
        const vid = el.dataset.vid;
        if (vid) openTrailerModal(vid, el.closest(".trailer-card")?.dataset.title || "");
      });
    });

    container.querySelectorAll(".trailer-badge").forEach(el => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        const vid = el.dataset.vid;
        if (vid) openTrailerModal(vid, el.closest(".trailer-card")?.dataset.title || "");
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
              <h2 id="trailerModalTitle" class="truncate" style="font-size:16px;">Trailer</h2>
            </div>
            <button id="closeTrailerModal" class="ghost" type="button" aria-label="Close trailer">✕</button>
          </div>
          <div class="panel-body" style="padding:0;">
            <div class="trailer-embed-wrap" style="aspect-ratio:16/9; background:#000;">
              <iframe id="trailerIframe" style="width:100%; height:100%; border:0;" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>
            </div>
          </div>
        </div>
      `;
      document.body.appendChild(ov);
      ov.addEventListener("click", (e) => { if (e.target === ov) closeTrailerModal(); });
      _trailers$("closeTrailerModal")?.addEventListener("click", closeTrailerModal);
    }
    const t = _trailers$("trailerModalTitle");
    if (t) t.textContent = title || "Trailer";
    const iframe = _trailers$("trailerIframe");
    if (iframe) {
      iframe.src = `https://www.youtube-nocookie.com/embed/${esc(videoId)}?rel=0&modestbranding=1`;
    }
    ov.classList.remove("hidden");
  }

  function closeTrailerModal() {
    const ov = _trailers$("trailerModalOverlay");
    if (!ov) return;
    ov.classList.add("hidden");
    const iframe = _trailers$("trailerIframe");
    if (iframe) iframe.src = "";
  }

  // --- Refresh button ---

  function _trailersEnsureHeader() {
    let header = _trailers$("trailerHeader");
    if (!header) {
      const view = _trailers$("trailersView");
      if (!view) return;
      header = document.createElement("div");
      header.id = "trailerHeader";
      header.style.cssText = "display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid rgba(255,255,255,0.08);";
      header.innerHTML = `
        <h2 style="font-size:16px;font-weight:700;margin:0;color:#f0f6fc;">Latest Trailers</h2>
        <button id="trailersRefreshBtn" class="ghost" type="button" aria-label="Refresh trailers" style="display:flex;align-items:center;gap:6px;">
          <svg id="trailersRefreshIcon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-refresh-cw"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M3 21v-5h5"/></svg>
          <span>Refresh</span>
        </button>
      `;
      view.insertBefore(header, view.firstChild);
      _trailers$("trailersRefreshBtn")?.addEventListener("click", refreshTrailers);
    }
  }

  async function refreshTrailers() {
    const btn = _trailers$("trailersRefreshBtn");
    const icon = _trailers$("trailersRefreshIcon");
    const container = _trailers$("trailersContainer");

    if (btn) btn.disabled = true;
    if (icon) icon.style.animation = "spin 1s linear infinite";

    try {
      const data = await _trailersPostJson("/api/trailers/refresh", {});

      if (data.status === "started" || data.status === "running") {
        if (container) container.innerHTML = `<div class="status">Refreshing feed… This may take up to 2 minutes.</div>`;

        let attempts = 0;
        const poll = setInterval(async () => {
          attempts++;
          try {
            const feed = await _trailersFetch();
            if (feed.items && feed.items.length > 0) {
              clearInterval(poll);
              _trailersRender(feed);
              if (icon) icon.style.animation = "";
              if (btn) btn.disabled = false;
              return;
            }
          } catch (e) {}
          if (attempts >= 12) { // 60 seconds
            clearInterval(poll);
            if (container) container.innerHTML = `<div class="status">Refresh timeout. The feed updates automatically every 10 minutes. Please check back later.</div>`;
            if (icon) icon.style.animation = "";
            if (btn) btn.disabled = false;
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
      const data = await _trailersFetch();
      _trailersRender(data);
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
