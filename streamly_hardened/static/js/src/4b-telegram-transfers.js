(() => {
  let pollTimer = null;
  let isOverlayOpen = false;

  function formatBytes(bytes, decimals = 2) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const dm = decimals < 0 ? 0 : decimals;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
  }

  async function cancelTransfer(taskId) {
    if (!confirm("Are you sure you want to cancel this transfer?")) return;
    try {
      const res = await postJson("/api/telegram/cancel", { task_id: taskId });
      if (res.success) {
        toast(res.message || "Transfer cancelled successfully.");
        // Immediate refresh
        refreshQueueStatus();
      } else {
        toast(res.error || "Failed to cancel transfer.");
      }
    } catch (e) {
      toast(e.message || "Failed to cancel transfer.");
    }
  }

  function renderQueue(data) {
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

    // 2. Render Active Transfer
    const activeCard = $("tgActiveTransferCard");
    if (data.active) {
      const active = data.active;
      const progress = active.progress !== undefined ? Number(active.progress).toFixed(1) : "0.0";
      const speed = active.speed_mb !== undefined ? `${active.speed_mb.toFixed(2)} MB/s` : "0.00 MB/s";
      
      activeCard.innerHTML = `
        <div style="display: flex; justify-content: space-between; align-items: start; gap: 12px;">
          <div style="flex: 1; min-width: 0;">
            <strong style="display: block; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--text);" title="${active.filename || 'file'}">${active.filename || 'file'}</strong>
            <span class="muted" style="font-size: 12px;">Status: <span style="color: var(--accent); font-weight: 600;">${active.status || 'UPLOADING'}</span></span>
          </div>
          <button class="tg-cancel-btn danger ghost" data-task-id="${active.task_id}" style="padding: 6px 12px; font-size: 12px;">Cancel</button>
        </div>
        <div style="width: 100%; height: 6px; background: var(--panel-1); border-radius: 3px; overflow: hidden; border: 1px solid var(--line);">
          <div style="width: ${progress}%; height: 100%; background: var(--accent); transition: width 0.3s;"></div>
        </div>
        <div style="display: flex; justify-content: space-between; font-size: 11px;" class="muted">
          <span>Progress: ${progress}% (${formatBytes(active.sent_bytes || 0)} / ${formatBytes(active.total_bytes || 0)})</span>
          <strong style="color: var(--accent);">${speed}</strong>
        </div>
      `;
    } else {
      activeCard.innerHTML = `<div class="empty" style="padding: 8px; margin: 0;">No active transfers running.</div>`;
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
        cancelBtn.className = "danger ghost";
        cancelBtn.style.cssText = "padding: 4px 8px; font-size: 11px;";
        cancelBtn.textContent = "Cancel";
        cancelBtn.dataset.taskId = item.task_id;
        
        actionTd.appendChild(cancelBtn);
        tr.append(nameTd, sizeTd, actionTd);
        qBody.appendChild(tr);
      });
    } else {
      qBody.innerHTML = `<tr><td colspan="3" class="muted" style="text-align: center; padding: 20px; font-size: 13px;">No transfers in queue.</td></tr>`;
    }

    // 4. Update tab badge count
    const activeCount = (data.active && (data.active.status === "UPLOADING" || data.active.status === "QUEUED")) ? 1 : 0;
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

    // Wire up cancel events
    document.querySelectorAll(".tg-cancel-btn, #tgQueueBody button").forEach((btn) => {
      btn.onclick = (e) => {
        const tid = e.target.dataset.taskId;
        if (tid) cancelTransfer(tid);
      };
    });
  }

  async function refreshQueueStatus() {
    try {
      const response = await fetch("/api/telegram/queue", { credentials: "same-origin" });
      if (response.ok) {
        const data = await response.json();
        renderQueue(data);
        
        // Keep polling if overlay is open OR if there's an active/queued transfer
        const hasWork = data.active || (data.queue && data.queue.length > 0);
        if (isOverlayOpen || hasWork) {
          if (pollTimer) clearTimeout(pollTimer);
          const interval = hasWork ? 10000 : 30000; // Poll every 10s if active, 30s if idle
          pollTimer = setTimeout(refreshQueueStatus, interval);
        }
      }
    } catch (e) {
      console.error("Error refreshing Telegram queue status:", e);
      if (isOverlayOpen) {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(refreshQueueStatus, 15000); // Poll every 15s on error if overlay open
      }
    }
  }

  // Hook Navigation button
  if ($("telegramTabBtn")) {
    $("telegramTabBtn").addEventListener("click", () => {
      if (typeof window.updateBottomNavHighlight === "function") window.updateBottomNavHighlight(3);
      isOverlayOpen = true;
      $("telegramTransfersOverlay").classList.remove("hidden");
      refreshQueueStatus();
    });
  }

  // Hook Close action
  if ($("closeTelegramTransfersBtn")) {
    $("closeTelegramTransfersBtn").addEventListener("click", () => {
      isOverlayOpen = false;
      $("telegramTransfersOverlay").classList.add("hidden");
      if (pollTimer) clearTimeout(pollTimer);
      if (typeof window.restoreActiveMainTabHighlight === "function") window.restoreActiveMainTabHighlight();
    });
  }

  // Expose triggers so external actions can start the polling loop
  window.triggerQueuePolling = function() {
    refreshQueueStatus();
  };
})();
