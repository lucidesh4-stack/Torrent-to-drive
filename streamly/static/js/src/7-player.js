/**
 * Seamless Direct Stream Video Player Modal Component for Streamly (7-player.js)
 * High-performance direct streaming with VLC launcher, link copier, codec error fallback, and capture-phase keyboard shortcuts.
 */
(function() {
  const $ = (id) => document.getElementById(id);
  let activeVideoEl = null;
  let currentStreamUrl = "";

  function clearElementFocus() {
    if (document.activeElement && typeof document.activeElement.blur === "function" && document.activeElement !== document.body) {
      document.activeElement.blur();
    }
  }

  function getAbsoluteUrl(url) {
    if (!url) return "";
    if (url.startsWith("http://") || url.startsWith("https://")) return url;
    return window.location.origin + (url.startsWith("/") ? "" : "/") + url;
  }

  async function copyToClipboard(text) {
    if (!text) return;
    const absUrl = getAbsoluteUrl(text);
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(absUrl);
        if (typeof window.toast === "function") window.toast("Stream link copied to clipboard!");
        else alert("Copied to clipboard: " + absUrl);
        return;
      }
    } catch (e) {
      console.warn("Clipboard API failed:", e);
    }
    prompt("Direct Stream Link:", absUrl);
  }

  function openInVlc(url) {
    if (!url) return;
    const absUrl = getAbsoluteUrl(url);
    window.location.href = "vlc://" + absUrl;
  }

  function toggleFullscreen(modal, videoEl) {
    const isFS = !!(document.fullscreenElement || document.webkitFullscreenElement || document.mozFullScreenElement);
    if (!isFS) {
      const target = videoEl || modal;
      if (target.requestFullscreen) target.requestFullscreen().catch(() => {});
      else if (target.webkitRequestFullscreen) target.webkitRequestFullscreen().catch(() => {});
      else if (modal.requestFullscreen) modal.requestFullscreen().catch(() => {});
    } else {
      if (document.exitFullscreen) document.exitFullscreen().catch(() => {});
      else if (document.webkitExitFullscreen) document.webkitExitFullscreen().catch(() => {});
    }
    clearElementFocus();
  }

  async function resolveDirectUrl(provider, itemId) {
    if (!itemId) return "";
    const strId = String(itemId).trim();
    if (strId.startsWith("http://") || strId.startsWith("https://")) {
      return strId;
    }

    const p = (provider || "").toLowerCase();

    // Temp Cloud: Direct streaming route with HTTP 206 range seeking
    if (p === "temp") {
      if (strId.startsWith("/api/temp_cloud/stream")) return strId;
      return `/api/temp_cloud/stream?file_id=${encodeURIComponent(strId)}`;
    }
    
    // Seedr: Fetch authenticated direct stream URL
    if (p === "seedr") {
      try {
        const res = await fetch(`/api/url?file_id=${encodeURIComponent(strId)}`, { credentials: "same-origin" });
        const data = await res.json();
        if (data && data.url) return data.url;
      } catch (e) {
        console.warn("Seedr direct URL fetch failed:", e);
      }
    }
    
    // Offcloud: Explore folder or return direct URL
    if (p === "offcloud") {
      try {
        const res = await fetch(`/api/offcloud/explore/${encodeURIComponent(strId)}`, { credentials: "same-origin" });
        const data = await res.json();
        if (data && data.files && data.files.length > 0) {
          return data.files[0].download_url || strId;
        }
      } catch (e) {
        console.warn("Offcloud direct URL fetch failed:", e);
      }
    }

    if (p === "telegram") {
      return `/api/telegram/download/${encodeURIComponent(strId)}`;
    }

    return strId;
  }

  window.openVideoPlayerModal = async function(provider, itemId, title = "Video Stream", meta = "") {
    const modal = $("videoPlayerModal");
    const titleEl = $("vpmTitle");
    const metaEl = $("vpmMeta");
    const videoEl = $("vpmVideo");
    const errorOverlay = $("vpmErrorOverlay");
    
    const directUrl = await resolveDirectUrl(provider, itemId);

    if (!directUrl) {
      const msg = "Could not resolve stream URL for this item.";
      if (typeof window.toast === "function") window.toast(msg);
      else alert(msg);
      return;
    }

    currentStreamUrl = directUrl;

    if (!modal || !videoEl) {
      window.open(directUrl, "_blank", "noopener,noreferrer");
      return;
    }

    if (titleEl) titleEl.textContent = title;
    if (metaEl) metaEl.textContent = meta;

    // Reset error overlay
    if (errorOverlay) errorOverlay.classList.add("hidden");

    // Reset previous media element state
    videoEl.pause();
    videoEl.removeAttribute("src");
    videoEl.load();

    // Assign source and active player
    videoEl.src = directUrl;
    activeVideoEl = videoEl;

    // Setup action buttons in header
    const dlBtn = $("vpmDownloadBtn");
    if (dlBtn) {
      if (provider === "temp") {
        dlBtn.href = `/api/temp_cloud/stream?file_id=${encodeURIComponent(itemId)}&download=1`;
      } else {
        dlBtn.href = directUrl;
      }
      dlBtn.setAttribute("download", title || "video.mp4");
    }

    const vlcBtn = $("vpmVlcBtn");
    if (vlcBtn) {
      vlcBtn.onclick = () => openInVlc(directUrl);
    }

    const copyBtn = $("vpmCopyBtn");
    if (copyBtn) {
      copyBtn.onclick = () => copyToClipboard(directUrl);
    }

    // Setup error overlay buttons
    const errVlcBtn = $("vpmErrorVlcBtn");
    if (errVlcBtn) errVlcBtn.onclick = () => openInVlc(directUrl);

    const errCopyBtn = $("vpmErrorCopyBtn");
    if (errCopyBtn) errCopyBtn.onclick = () => copyToClipboard(directUrl);

    const errDlBtn = $("vpmErrorDownloadBtn");
    if (errDlBtn) {
      errDlBtn.href = dlBtn ? dlBtn.href : directUrl;
      errDlBtn.setAttribute("download", title || "video.mp4");
    }

    // Error handler for unsupported codecs (e.g. MKV/AC3)
    videoEl.onerror = function() {
      const err = videoEl.error;
      console.warn("Video element decode/network error:", err);
      if (errorOverlay) {
        errorOverlay.classList.remove("hidden");
      }
    };

    videoEl.onloadeddata = function() {
      if (errorOverlay) errorOverlay.classList.add("hidden");
    };

    modal.classList.remove("hidden");
    document.body.style.overflow = "hidden";
    clearElementFocus();

    // Start playback
    videoEl.load();
    const p = videoEl.play();
    if (p && typeof p.catch === "function") {
      p.catch((err) => {
        console.warn("Video playback was interrupted or codec is unsupported:", err);
      });
    }
  };

  window.closeVideoPlayerModal = function() {
    const modal = $("videoPlayerModal");
    const videoEl = $("vpmVideo");
    const errorOverlay = $("vpmErrorOverlay");

    if (videoEl) {
      videoEl.pause();
      videoEl.removeAttribute("src");
      videoEl.load();
    }
    if (errorOverlay) errorOverlay.classList.add("hidden");
    if (modal) modal.classList.add("hidden");
    document.body.style.overflow = "";
    activeVideoEl = null;
    currentStreamUrl = "";
    clearElementFocus();
  };

  // Event Listeners for close button, backdrop, and auto focus-blur
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
      modal.addEventListener("pointerdown", function() {
        setTimeout(clearElementFocus, 100);
      });
      modal.addEventListener("click", function(e) {
        if (e.target === modal) {
          closeVideoPlayerModal();
        }
        setTimeout(clearElementFocus, 100);
      });
    }
    
    document.addEventListener("fullscreenchange", function() {
      setTimeout(clearElementFocus, 100);
    });
    document.addEventListener("webkitfullscreenchange", function() {
      setTimeout(clearElementFocus, 100);
    });
  });

  // Capture-phase Keyboard Shortcuts Listener for Video Player
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
    const isPlayerKey = [" ", "k", "arrowleft", "j", "arrowright", "l", "f", "m", "arrowup", "arrowdown"].includes(key);

    if (isPlayerKey) {
      e.preventDefault();
      e.stopPropagation();
      clearElementFocus();

      switch (key) {
        case " ":
        case "k":
          if (activeVideoEl.paused) activeVideoEl.play().catch(() => {});
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
        case "arrowup":
          activeVideoEl.volume = Math.min(1.0, activeVideoEl.volume + 0.1);
          break;
        case "arrowdown":
          activeVideoEl.volume = Math.max(0.0, activeVideoEl.volume - 0.1);
          break;
        case "f":
          toggleFullscreen(modal, activeVideoEl);
          break;
        case "m":
          activeVideoEl.muted = !activeVideoEl.muted;
          break;
      }
    }
  }, true);
})();
