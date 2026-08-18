/**
 * Seamless Direct Stream Video Player Modal Component for Streamly (7-player.js)
 * High-performance direct streaming with robust capture-phase keyboard shortcuts.
 */
(function() {
  let activeVideoEl = null;

  async function resolveDirectUrl(provider, itemId) {
    const p = (provider || "").toLowerCase();
    
    // Seedr: Use Seedr API or file object URL if available
    if (p === "seedr") {
      try {
        const res = await fetch(`/api/url?file_id=${encodeURIComponent(itemId)}`);
        const data = await res.json();
        if (data && data.url) return data.url;
      } catch (e) {
        console.warn("Seedr direct URL fetch failed:", e);
      }
    }
    
    // Offcloud: Explore folder or return direct URL
    if (p === "offcloud") {
      if (itemId.startsWith("http://") || itemId.startsWith("https://")) {
        return itemId;
      }
      try {
        const res = await fetch(`/api/offcloud/explore/${encodeURIComponent(itemId)}`);
        const data = await res.json();
        if (data && data.files && data.files.length > 0) {
          return data.files[0].download_url || itemId;
        }
      } catch (e) {
        console.warn("Offcloud direct URL fetch failed:", e);
      }
    }

    if (p === "telegram") {
      return `/api/telegram/download/${encodeURIComponent(itemId)}`;
    }

    return itemId;
  }

  window.openVideoPlayerModal = async function(provider, itemId, title = "Video Stream", meta = "") {
    const modal = $("videoPlayerModal");
    const titleEl = $("vpmTitle");
    const metaEl = $("vpmMeta");
    const videoEl = $("vpmVideo");
    
    const directUrl = await resolveDirectUrl(provider, itemId);

    if (!modal || !videoEl) {
      if (directUrl) window.open(directUrl, "_blank");
      return;
    }

    if (titleEl) titleEl.textContent = title;
    if (metaEl) metaEl.textContent = meta;

    // Reset previous media element state
    videoEl.pause();
    videoEl.removeAttribute("src");
    videoEl.load();

    videoEl.src = directUrl;
    activeVideoEl = videoEl;

    modal.classList.remove("hidden");
    document.body.style.overflow = "hidden";
    videoEl.focus();
    videoEl.play().catch(() => {});
  };

  window.closeVideoPlayerModal = function() {
    const modal = $("videoPlayerModal");
    const videoEl = $("vpmVideo");
    if (videoEl) {
      videoEl.pause();
      videoEl.removeAttribute("src");
      videoEl.load();
    }
    if (modal) modal.classList.add("hidden");
    document.body.style.overflow = "";
    activeVideoEl = null;
  };

  // Event Listeners for close button and backdrop
  document.addEventListener("DOMContentLoaded", function() {
    const modal = $("videoPlayerModal");
    const closeBtn = $("vpmCloseBtn");
    if (closeBtn) {
      closeBtn.addEventListener("click", function(e) {
        e.stopPropagation();
        closeVideoPlayerModal();
      });
    }
    if (modal) {
      modal.addEventListener("click", function(e) {
        if (e.target === modal) {
          closeVideoPlayerModal();
        }
      });
    }
  });

  // Capture-phase Keyboard Shortcuts Listener for Video Player
  // Uses true capture phase to intercept keys reliably regardless of clicked player element
  window.addEventListener("keydown", function(e) {
    const modal = $("videoPlayerModal");
    if (!modal || modal.classList.contains("hidden")) return;

    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      closeVideoPlayerModal();
      return;
    }

    if (!activeVideoEl) return;

    const key = e.key.toLowerCase();
    const isPlayerKey = [" ", "k", "arrowleft", "j", "arrowright", "l", "f", "m"].includes(key);

    if (isPlayerKey) {
      e.preventDefault();
      e.stopPropagation();

      switch (key) {
        case " ":
        case "k":
          if (activeVideoEl.paused) activeVideoEl.play();
          else activeVideoEl.pause();
          break;
        case "arrowleft":
        case "j":
          activeVideoEl.currentTime = Math.max(0, activeVideoEl.currentTime - 5);
          break;
        case "arrowright":
        case "l":
          activeVideoEl.currentTime = Math.min(activeVideoEl.duration || 0, activeVideoEl.currentTime + 5);
          break;
        case "f":
          if (!document.fullscreenElement) {
            modal.requestFullscreen().catch(() => {});
          } else {
            document.exitFullscreen().catch(() => {});
          }
          break;
        case "m":
          activeVideoEl.muted = !activeVideoEl.muted;
          break;
      }
    }
  }, true);
})();
