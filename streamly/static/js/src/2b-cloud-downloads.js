(() => {
  window.isDownloadsOverlayOpen = false;
  let downloadsEventSource = null;
  window._dlCancelInFlight = new Set();

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatBytes(bytes, decimals = 2) {
    if (!bytes || bytes === 0) return '0 Bytes';
    const k = 1024;
    const dm = decimals < 0 ? 0 : decimals;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
  }

  window.openCloudDownloadsModal = function() {
    window.isDownloadsOverlayOpen = true;
    const overlay = document.getElementById("cloudDownloadsOverlay");
    if (overlay) overlay.classList.remove("hidden");
    const input = document.getElementById("tempDownloadUrl");
    if (input) { input.focus(); }
    initDownloadsSSE();
    fetchDownloadsState();
  };

  window.closeCloudDownloadsModal = function() {
    window.isDownloadsOverlayOpen = false;
    const overlay = document.getElementById("cloudDownloadsOverlay");
    if (overlay) overlay.classList.add("hidden");
  };

  window.cancelCloudDownload = async function(taskId, event) {
    if (event) {
      event.preventDefault();
      event.stopPropagation();
    }
    const tid = String(taskId || "").trim();
    if (!tid || window._dlCancelInFlight.has(tid)) return;

    window._dlCancelInFlight.add(tid);
    try {
      const res = await window.postJson("/api/temp_cloud/downloads/cancel", { task_id: tid });
      if (res && res.success) {
        if (window.toast) window.toast("Download cancelled");
        fetchDownloadsState();
      } else {
        if (window.toast) window.toast(res.error || "Failed to cancel");
      }
    } catch (e) {
      if (window.toast) window.toast(e.message || "Cancel failed");
    } finally {
      window._dlCancelInFlight.delete(tid);
    }
  };

  window.pauseCloudDownload = async function(taskId, event) {
    if (event) { event.preventDefault(); event.stopPropagation(); }
    const tid = String(taskId || "").trim();
    if (!tid) return;
    try {
      await window.postJson("/api/temp_cloud/downloads/pause", { task_id: tid });
      fetchDownloadsState();
    } catch (e) {
      if (window.toast) window.toast(e.message || "Pause failed");
    }
  };

  window.resumeCloudDownload = async function(taskId, event) {
    if (event) { event.preventDefault(); event.stopPropagation(); }
    const tid = String(taskId || "").trim();
    if (!tid) return;
    try {
      await window.postJson("/api/temp_cloud/downloads/resume", { task_id: tid });
      fetchDownloadsState();
    } catch (e) {
      if (window.toast) window.toast(e.message || "Resume failed");
    }
  };

  function renderDownloads(data) {
    if (!data) return;
    const active = data.active || [];
    const queue = data.queue || [];
    const completed = data.completed || [];

    // 1. Update Badges
    const totalActiveCount = active.length + queue.length;
    const badges = [document.getElementById("cloudDownloadsBadge"), document.getElementById("cmDownloadsBadge")];
    badges.forEach(b => {
      if (b) {
        if (totalActiveCount > 0) {
          b.textContent = String(totalActiveCount);
          b.classList.remove("hidden");
        } else {
          b.classList.add("hidden");
        }
      }
    });

    // 2. Render In-View Temp Cloud Banner
    const banner = document.getElementById("cloudActiveDownloadBanner");
    if (banner) {
      if (active.length > 0) {
        banner.classList.remove("hidden");
        banner.innerHTML = active.map(item => {
          const fname = escapeHtml(item.filename || "file");
          const pct = Math.min(100, Math.max(0, item.progress || 0)).toFixed(1);
          const speed = item.speed_mb ? `${item.speed_mb.toFixed(2)} MB/s` : (item.speed_mbps ? `${item.speed_mbps.toFixed(1)} Mbps` : "0.0 MB/s");
          const sent = formatBytes(item.downloaded_bytes || 0);
          const total = formatBytes(item.total_bytes || 0);
          const isPaused = item.status === "PAUSED";
          const statusText = item.status === "EXTRACTING" ? "EXTRACTING ARCHIVE..." : (isPaused ? "PAUSED" : (item.type === "bunkr" ? "BUNKR ALBUM" : "1DM TURBO"));

          return `
          <div style="background: rgba(15,23,42,0.9); border: 1px solid rgba(59,130,246,0.3); border-radius: 8px; padding: 10px 14px; margin-bottom: 10px; display: flex; flex-direction: column; gap: 6px;">
            <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px;">
              <div style="display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1;">
                <span style="font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px; background: rgba(59,130,246,0.2); color: #60a5fa; text-transform: uppercase;">${statusText}</span>
                <strong style="font-size: 13px; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${fname}">${fname}</strong>
              </div>
              <div style="display: flex; align-items: center; gap: 6px; flex-shrink: 0;">
                <button onclick="window.openCloudDownloadsModal()" style="background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.12); color: #cbd5e1; border-radius: 4px; cursor: pointer; padding: 3px 8px; font-size: 11px; font-weight: 600;">Details</button>
                <button onclick="window.cancelCloudDownload('${item.task_id}', event)" style="background: rgba(239,68,68,0.15); border: 1px solid rgba(239,68,68,0.3); color: #f87171; border-radius: 4px; cursor: pointer; padding: 3px 8px; font-size: 11px; font-weight: 700;" title="Cancel">✕</button>
              </div>
            </div>
            <!-- Progress Bar -->
            <div style="width: 100%; height: 6px; background: rgba(255,255,255,0.08); border-radius: 999px; overflow: hidden;">
              <div style="width: ${pct}%; height: 100%; background: linear-gradient(90deg, #3b82f6, #60a5fa); border-radius: 999px; transition: width 0.3s ease;"></div>
            </div>
            <div style="display: flex; justify-content: space-between; align-items: center; font-size: 11px;" class="muted">
              <span>${sent} / ${total} (${pct}%)</span>
              <span style="font-family: monospace; font-weight: 600; color: #60a5fa;">${speed}</span>
            </div>
          </div>`;
        }).join('');
      } else {
        banner.classList.add("hidden");
        banner.innerHTML = "";
      }
    }

    // 3. Render Modal Active Cards
    const modalActiveCard = document.getElementById("cloudActiveDownloadsCard");
    if (modalActiveCard) {
      if (active.length > 0) {
        modalActiveCard.innerHTML = active.map(item => {
          const fname = escapeHtml(item.filename || "file");
          const pct = Math.min(100, Math.max(0, item.progress || 0)).toFixed(1);
          const speed = item.speed_mb ? `${item.speed_mb.toFixed(2)} MB/s` : (item.speed_mbps ? `${item.speed_mbps.toFixed(1)} Mbps` : "0.0 MB/s");
          const sent = formatBytes(item.downloaded_bytes || 0);
          const total = formatBytes(item.total_bytes || 0);
          const isPaused = item.status === "PAUSED";
          const statusBadge = item.status === "EXTRACTING" ? "background: rgba(245,158,11,0.2); color: #fbbf24;" : (isPaused ? "background: rgba(239,68,68,0.2); color: #f87171;" : "background: rgba(59,130,246,0.2); color: #60a5fa;");

          return `
          <div style="padding: 12px; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; display: flex; flex-direction: column; gap: 8px;">
            <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px;">
              <div style="display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1;">
                <span style="font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px; ${statusBadge} text-transform: uppercase;">${item.status}</span>
                <strong style="font-size: 13px; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${fname}">${fname}</strong>
              </div>
              <div style="display: flex; align-items: center; gap: 6px; flex-shrink: 0;">
                ${isPaused ? 
                  `<button onclick="window.resumeCloudDownload('${item.task_id}', event)" style="background: rgba(16,185,129,0.15); border: 1px solid rgba(16,185,129,0.3); color: #34d399; border-radius: 4px; cursor: pointer; padding: 4px 8px; font-size: 11px; font-weight: 600;">▶ Resume</button>` :
                  `<button onclick="window.pauseCloudDownload('${item.task_id}', event)" style="background: rgba(245,158,11,0.15); border: 1px solid rgba(245,158,11,0.3); color: #fbbf24; border-radius: 4px; cursor: pointer; padding: 4px 8px; font-size: 11px; font-weight: 600;">⏸ Pause</button>`
                }
                <button onclick="window.cancelCloudDownload('${item.task_id}', event)" style="background: rgba(239,68,68,0.15); border: 1px solid rgba(239,68,68,0.3); color: #f87171; border-radius: 4px; cursor: pointer; padding: 4px 8px; font-size: 11px; font-weight: 700;" title="Cancel">✕</button>
              </div>
            </div>
            <div style="width: 100%; height: 6px; background: rgba(255,255,255,0.08); border-radius: 999px; overflow: hidden;">
              <div style="width: ${pct}%; height: 100%; background: linear-gradient(90deg, #3b82f6, #60a5fa); border-radius: 999px; transition: width 0.3s ease;"></div>
            </div>
            <div style="display: flex; justify-content: space-between; align-items: center; font-size: 11px;" class="muted">
              <span>${sent} / ${total} (${pct}%)</span>
              <span style="font-family: monospace; font-weight: 600; color: var(--accent);">${speed}</span>
            </div>
          </div>`;
        }).join('');
      } else {
        modalActiveCard.innerHTML = `<div class="empty" style="padding: 12px; margin: 0; font-size: 13px;">No active downloads running.</div>`;
      }
    }

    // 4. Render Queue Body
    const queueBody = document.getElementById("cloudDownloadsQueueBody");
    if (queueBody) {
      if (queue.length > 0) {
        queueBody.innerHTML = queue.map(item => {
          const fname = escapeHtml(item.filename || "file");
          const ftype = item.type === "bunkr" ? "Bunkr Album" : "Direct / Archive";
          return `
          <tr>
            <td style="padding: 10px; font-size: 13px; font-weight: 600; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${fname}</td>
            <td style="padding: 10px; font-size: 12px; color: var(--muted); text-align: center;">${ftype}</td>
            <td style="padding: 10px; text-align: center;">
              <button onclick="window.cancelCloudDownload('${item.task_id}', event)" style="background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.25); color: #f87171; border-radius: 4px; cursor: pointer; padding: 2px 8px; font-size: 11px; font-weight: 700;">✕</button>
            </td>
          </tr>`;
        }).join('');
      } else {
        queueBody.innerHTML = `<tr><td colspan="3" class="muted" style="text-align: center; padding: 16px; font-size: 13px;">No downloads in queue.</td></tr>`;
      }
    }
  }

  async function fetchDownloadsState() {
    try {
      const res = await window.getJson("/api/temp_cloud/downloads");
      if (res) renderDownloads(res);
    } catch (e) {
      // ignore transient polling errors
    }
  }

  function initDownloadsSSE() {
    if (downloadsEventSource) return;
    try {
      downloadsEventSource = new EventSource("/api/temp_cloud/downloads/sse");
      downloadsEventSource.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          renderDownloads(data);
        } catch (e) {}
      };
      downloadsEventSource.onerror = () => {
        if (downloadsEventSource) {
          downloadsEventSource.close();
          downloadsEventSource = null;
        }
        // Fallback to light interval poll if SSE disconnected
        setTimeout(initDownloadsSSE, 5000);
      };
    } catch (e) {
      console.warn("SSE init failed:", e);
    }
  }

  // Initialize event listeners when DOM loads
  document.addEventListener("DOMContentLoaded", () => {
    // Toolbar buttons
    document.getElementById("cloudDownloadsBtn")?.addEventListener("click", window.openCloudDownloadsModal);
    document.getElementById("cmDownloadsBtn")?.addEventListener("click", window.openCloudDownloadsModal);
    document.getElementById("closeCloudDownloadsBtn")?.addEventListener("click", window.closeCloudDownloadsModal);

    // Add Download submit handler
    const startDownloadBtn = document.getElementById("startTempDownloadBtn");
    const downloadInput = document.getElementById("tempDownloadUrl");
    const autoUnzipCb = document.getElementById("tempAutoUnzip");

    const handleStartDownload = async () => {
      const url = (downloadInput ? downloadInput.value : "").trim();
      if (!url) {
        if (window.toast) window.toast("Please paste a valid URL or Bunkr link");
        return;
      }
      if (downloadInput) downloadInput.value = "";

      try {
        const res = await window.postJson("/api/temp_cloud/download", {
          url: url,
          auto_unzip: autoUnzipCb ? autoUnzipCb.checked : true
        });
        if (res && res.success) {
          if (window.toast) window.toast(res.message || "Download queued!");
          fetchDownloadsState();
        } else {
          if (window.toast) window.toast(res?.error || "Failed to start download");
        }
      } catch (err) {
        if (window.toast) window.toast("Download error: " + (err.message || "Failed"));
      }
    };

    if (startDownloadBtn) startDownloadBtn.addEventListener("click", handleStartDownload);
    if (downloadInput) {
      downloadInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          handleStartDownload();
        }
      });
    }

    // Click outside overlay to close
    document.getElementById("cloudDownloadsOverlay")?.addEventListener("click", (e) => {
      if (e.target.id === "cloudDownloadsOverlay") window.closeCloudDownloadsModal();
    });

    // Start initial background SSE listener
    initDownloadsSSE();
  });
})();
