  // ============================ TRAILERS MODULE ============================
  // Standalone: expects #trailersView and #trailersContainer in the DOM.
  // Wire a tab button to call setTrailersTab() (see 6-main.js integration).

  const TRAILERS_API = "/api/trailers";

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
      container.innerHTML = `<div class="empty">No trailers in the last 30 days. The feed refreshes automatically.</div>`;
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
    // Update bottom nav highlight if you add a 5th tab
    if (typeof window.updateBottomNavHighlight === "function") {
      window.updateBottomNavHighlight(4); // adjust index to your nav
    }
    loadTrailers();
  }

  // Expose
  window.loadTrailers = loadTrailers;
  window.setTrailersTab = setTrailersTab;
  window.openTrailerModal = openTrailerModal;
  window.closeTrailerModal = closeTrailerModal;
