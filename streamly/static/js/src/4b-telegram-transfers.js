(() => {
  window.pollTimer = null;
  window.isOverlayOpen = false;

  // Escape HTML so server/torrent-supplied values (e.g. filenames) can't inject
  // markup/script when inserted via innerHTML (XSS hardening).
  window.escapeHtml = function(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  window.formatBytes = function(bytes, decimals = 2) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const dm = decimals < 0 ? 0 : decimals;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
  }

  window._cancelInFlight = window._cancelInFlight || new Set();

  // Renamed from window.cancelTransfer -- 2-cloud.js also defined a DIFFERENT
  // function under that exact name (for cancelling a Seedr download, not a
  // Telegram upload). Both attached to the shared window object, so whichever
  // loaded last silently won, and the other's callers ended up invoking this
  // (wrong) function with the wrong argument shape. Renamed to remove the
  // collision -- see the matching comment in 2-cloud.js's cancelSeedrTransfer.
  window.cancelTelegramTransfer = async function(taskId, event) {
    if (event) {
      if (event.preventDefault) event.preventDefault();
      if (event.stopPropagation) event.stopPropagation();
    }
    const tid = String(taskId || "").trim();
    if (!tid) return;
    if (window._cancelInFlight.has(tid)) return;

    window._cancelInFlight.add(tid);
    const btn = event && (event.currentTarget || event.target);
    if (btn) {
      btn.disabled = true;
      btn.style.opacity = '0.4';
      btn.style.pointerEvents = 'none';
    }
    try {
      const res = await postJson("/api/telegram/cancel", { task_id: tid });
      if (res.success) {
        toast(res.message || "Transfer cancelled successfully.");
        // Immediate refresh
        refreshQueueStatus();
      } else {
        toast(res.error || "Failed to cancel transfer.");
        if (btn) {
          btn.disabled = false;
          btn.style.opacity = '1';
          btn.style.pointerEvents = 'auto';
        }
      }
    } catch (e) {
      toast(e.message || "Failed to cancel transfer.");
      if (btn) {
        btn.disabled = false;
        btn.style.opacity = '1';
        btn.style.pointerEvents = 'auto';
      }
    } finally {
      window._cancelInFlight.delete(tid);
    }
  }

  window.renderQueue = function(data) {
    // 1. Render Limit / Target
    const usage = Number(data.bandwidth_usage_gb || 0);
    const projected = Number(data.bandwidth_projected_gb || usage);
    const limit = Number(data.bandwidth_limit_gb || 4.5);
    
    let limitText = `${usage.toFixed(2)} GB / ${limit.toFixed(1)} GB`;
    if (projected > usage) {
      limitText = `${usage.toFixed(2)} GB (Proj: ${projected.toFixed(2)} GB) / ${limit.toFixed(1)} GB`;
    }
    $("tgTransfersLimitText").textContent = limitText;
    
    const pct = Math.min(100, (usage / limit) * 100);
    $("tgTransfersLimitBar").style.width = `${pct}%`;
    
    if (projected >= limit) {
      $("tgTransfersLimitBar").style.background = "#ef4444";
    } else if (projected >= 4.0) {
      $("tgTransfersLimitBar").style.background = "#f59e0b";
    } else {
      $("tgTransfersLimitBar").style.background = "var(--accent)";
    }
    
    $("tgTransfersTargetText").textContent = data.destination || "me";

    // 2. Render Active Transfers (Multi-Parallel Support)
    const activeCard = $("tgActiveTransferCard");
    const rawActive = data.active_items || (data.active ? [data.active] : []);
    const activeItems = rawActive.filter(a => a && a.status !== "COMPLETED" && a.status !== "FAILED");
    activeItems.sort((a, b) => {
      if (a.seq_num !== undefined && b.seq_num !== undefined && a.seq_num !== b.seq_num) {
        return a.seq_num - b.seq_num;
      }
      return (a.filename || "").localeCompare(b.filename || "", undefined, { numeric: true, sensitivity: "base" });
    });
    if (activeItems.length > 0) {
      activeCard.innerHTML = activeItems.map(active => {
        const speed = active.speed_mb !== undefined ? `${active.speed_mb.toFixed(2)} MB/s` : "0.00 MB/s";
        const fname = escapeHtml(active.filename || "file");
        const fstatus = escapeHtml(active.status || "UPLOADING");
        const ftask = escapeHtml(active.task_id || "");
        const sentFormatted = formatBytes(active.sent_bytes || 0);
        const totalFormatted = formatBytes(active.total_bytes || 0);
        
        let badgeStyle = "background: rgba(59,130,246,0.15); color: #60a5fa;";
        if (active.status === "WAITING TURN") {
          badgeStyle = "background: rgba(245,158,11,0.15); color: #fbbf24;";
        } else if (active.status === "DOWNLOADING") {
          badgeStyle = "background: rgba(16,185,129,0.15); color: #34d399;";
        }
        
        return `
        <div style="margin-bottom: 8px; padding: 10px 14px; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; display: flex; flex-direction: column; gap: 6px;">
          <div style="display: flex; align-items: center; justify-content: space-between; gap: 10px;">
            <div style="display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1;">
              <span style="font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px; ${badgeStyle} text-transform: uppercase; letter-spacing: 0.5px; flex-shrink: 0;">${fstatus}</span>
              <strong style="font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--text);" title="${fname}">${fname}</strong>
            </div>
            ${ftask ? `<button onclick="cancelTelegramTransfer('${ftask}', event)" style="background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.25); color: #f87171; border-radius: 4px; cursor: pointer; padding: 3px 8px; font-size: 12px; font-weight: 600; flex-shrink: 0; line-height: 1; transition: all 0.2s;" title="Cancel Upload" aria-label="Cancel Upload">✕</button>` : ''}
          </div>
          <div style="display: flex; align-items: center; justify-content: space-between; font-size: 11px;" class="muted">
            <span>${sentFormatted} / ${totalFormatted}</span>
            <span style="font-weight: 600; color: var(--accent); font-family: monospace;">${speed}</span>
          </div>
        </div>`;
      }).join('');
    } else {
      activeCard.innerHTML = `<div class="muted" style="font-size: 13px; text-align: center; padding: 12px;">No active transfer in progress.</div>`;
    }

    // 3. Render Queue List
    const qBody = $("tgQueueBody");
    if (data.queue && data.queue.length > 0) {
      qBody.innerHTML = "";
      data.queue.forEach((item) => {
        const tr = document.createElement("tr");
        
        const nameTd = document.createElement("td");
        nameTd.style.cssText = "font-size: 13px; padding: 10px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;";
        nameTd.textContent = item.filename;
        nameTd.title = item.filename;
        
        const sizeTd = document.createElement("td");
        sizeTd.style.cssText = "width: 100px; font-size: 13px; padding: 10px; text-align: right;";
        sizeTd.textContent = formatBytes(item.total_bytes);
        
        const actionTd = document.createElement("td");
        actionTd.style.cssText = "width: 90px; font-size: 13px; padding: 10px; text-align: center;";
        
        const cancelBtn = document.createElement("button");
        cancelBtn.className = "tg-cancel-btn danger ghost";
        cancelBtn.style.cssText = "padding: 3px 8px; font-size: 15px; line-height: 1;";
        cancelBtn.innerHTML = "&times;";
        cancelBtn.title = "Cancel transfer";
        cancelBtn.setAttribute("aria-label", "Cancel transfer");
        cancelBtn.dataset.taskId = item.task_id;
        
        actionTd.appendChild(cancelBtn);
        tr.append(nameTd, sizeTd, actionTd);
        qBody.appendChild(tr);
      });
    } else {
      qBody.innerHTML = `<tr><td colspan="3" class="muted" style="text-align: center; padding: 20px; font-size: 13px;">No transfers in queue.</td></tr>`;
    }

    // 4. Update tab badge count
    const activeCount = (data.active_items || []).filter(a => a && a.status !== "COMPLETED" && a.status !== "FAILED").length;
    const queueCount = data.queue ? data.queue.length : 0;
    const totalCount = activeCount + queueCount;
    
    const badge = $("tgBadge");
    if (badge) {
      if (totalCount > 0) {
        badge.textContent = totalCount;
        badge.classList.remove("hidden");
      } else {
        badge.classList.add("hidden");
      }
    }

    // Wire up cancel events. Use currentTarget (the button), not target, so a
    // click on the "×" glyph inside the button still resolves the task id.
    document.querySelectorAll(".tg-cancel-btn, #tgQueueBody button").forEach((btn) => {
      btn.onclick = (e) => {
        const tid = e.currentTarget.dataset.taskId;
        if (tid) cancelTelegramTransfer(tid);
      };
    });

    // Show/hide + wire the "Cancel All" button (only meaningful when the queue
    // has items). Cancels every queued task; the active transfer is left running.
    const cancelAllBtn = $("tgCancelAllBtn");
    if (cancelAllBtn) {
      if (queueCount > 0) {
        cancelAllBtn.classList.remove("hidden");
        cancelAllBtn.onclick = () => cancelAllQueued(data);
      } else {
        cancelAllBtn.classList.add("hidden");
      }
    }
  }

  window.cancelAllQueued = async function(data) {
    const queue = (data && data.queue) || [];
    const ids = queue.map((it) => it.task_id).filter(Boolean);
    if (ids.length === 0) return;
    if (!confirm(`Cancel all ${ids.length} queued transfer(s)? The active transfer will keep running.`)) return;

    const btn = $("tgCancelAllBtn");
    if (btn) { btn.disabled = true; btn.textContent = "Cancelling…"; }

    let ok = 0, fail = 0;
    // Sequential to avoid hammering the API / rate limiter with a burst.
    for (const tid of ids) {
      if (window._cancelInFlight.has(tid)) continue;
      window._cancelInFlight.add(tid);
      try {
        const res = await postJson("/api/telegram/cancel", { task_id: tid });
        if (res && res.success) ok++; else fail++;
      } catch (e) {
        fail++;
      } finally {
        window._cancelInFlight.delete(tid);
      }
    }

    toast(fail === 0 ? `Cancelled ${ok} queued transfer(s).` : `Cancelled ${ok}, ${fail} failed.`);
    if (btn) { btn.disabled = false; btn.textContent = "Cancel All"; }
    if (typeof refreshQueueStatus === "function") refreshQueueStatus();
  };

  window.refreshQueueStatus = async function() {
    // If SSE EventSource is active or connecting, skip HTTP GET polling loop to save bandwidth and server resources
    if (window._tgEventSource && (window._tgEventSource.readyState === EventSource.OPEN || window._tgEventSource.readyState === EventSource.CONNECTING)) {
      if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
      return;
    }

    try {
      const response = await fetch("/api/telegram/queue", { credentials: "same-origin" });
      if (response.ok) {
        const data = await response.json();
        renderQueue(data);
        
        // Keep polling ONLY if SSE is not active AND (overlay is open OR active transfer/queue exists)
        const hasWork = data.active || (data.queue && data.queue.length > 0);
        if ((isOverlayOpen || hasWork) && (!window._tgEventSource || window._tgEventSource.readyState !== EventSource.OPEN)) {
          if (pollTimer) clearTimeout(pollTimer);
          const interval = hasWork ? 10000 : 30000;
          pollTimer = setTimeout(refreshQueueStatus, interval);
        }
      }
    } catch (e) {
      console.error("Error refreshing Telegram queue status:", e);
      if (isOverlayOpen && (!window._tgEventSource || window._tgEventSource.readyState !== EventSource.OPEN)) {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(refreshQueueStatus, 15000);
      }
    }
  }

  window.stopTelegramSSE = function() {
    if (window._tgEventSource) {
      try {
        window._tgEventSource.close();
      } catch (e) {}
      window._tgEventSource = null;
    }
  };

  // Hook Navigation button
  if ($("telegramTabBtn")) {
    $("telegramTabBtn").addEventListener("click", () => {
      if (typeof window.updateBottomNavHighlight === "function") window.updateBottomNavHighlight(3);
      isOverlayOpen = true;
      $("telegramTransfersOverlay").classList.remove("hidden");
      window.initTelegramSSE();
      refreshQueueStatus();
    });
  }

  // Hook Close action
  if ($("closeTelegramTransfersBtn")) {
    $("closeTelegramTransfersBtn").addEventListener("click", () => {
      isOverlayOpen = false;
      $("telegramTransfersOverlay").classList.add("hidden");
      window.stopTelegramSSE();
      if (pollTimer) clearTimeout(pollTimer);
      if (typeof window.restoreActiveMainTabHighlight === "function") window.restoreActiveMainTabHighlight();
    });
  }

  window.initTelegramSSE = function() {
    if (!isOverlayOpen) return;
    if (window._tgEventSource) return;
    try {
      const es = new EventSource("/api/telegram/sse/progress");
      window._tgEventSource = es;
      es.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data) {
            window._lastQueueData = Object.assign({}, window._lastQueueData || {}, data);
            renderQueue(window._lastQueueData);
          }
        } catch (e) {
          console.debug("SSE JSON parse error:", e);
        }
      };
      es.onerror = () => {
        // EventSource browser client automatically reconnects on disconnect
      };
    } catch (e) {
      console.debug("SSE init error:", e);
    }
  };

  // Expose triggers so external actions can start the polling loop
  window.triggerQueuePolling = function() {
    if (isOverlayOpen) {
      window.initTelegramSSE();
      refreshQueueStatus();
    }
  };
})();
