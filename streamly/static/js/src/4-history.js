  window.saveToHistory = async function(magnet, title, size) {
    try {
      await postJson("/api/history/add", { magnet: magnet, name: title || "Unknown Magnet", size: size || "" });
    } catch (e) {
      console.warn("Failed to save history", e);
      // Optional: toast("History save failed: " + (e.message || "Unknown error"));
    }
  }

  window.renderHistory = async function() {
    const tbody = $("historyBody");
    tbody.innerHTML = "<tr><td colspan='2' class='muted' style='text-align:center;'>Loading...</td></tr>";
    
    try {
      const data = await parseResponse(await fetch("/api/history", { credentials: "same-origin" }));
      const history = data.items || [];
      
      tbody.innerHTML = "";
      $("historyEmpty").classList.toggle("hidden", history.length > 0);
      
      history.forEach(item => {
        const tr = document.createElement("tr");
        
        const nameTd = document.createElement("td");
        nameTd.style.maxWidth = "0"; // allows truncate inside table-layout: fixed
        nameTd.style.width = "100%";
        const titleDiv = document.createElement("div");
        titleDiv.className = "truncate";
        titleDiv.style.fontWeight = "bold";
        titleDiv.textContent = item.title;
        nameTd.append(titleDiv);
        
        const sizeDiv = document.createElement("div");
        sizeDiv.className = "text-meta";
        sizeDiv.style.fontSize = "11px";
        sizeDiv.style.marginTop = "2px";
        sizeDiv.textContent = item.size ? `${item.size} · ${item.time}` : item.time;
        nameTd.append(sizeDiv);
        
        const actionTd = document.createElement("td");
        actionTd.style.textAlign = "right";
        const btnGroup = document.createElement("div");
        btnGroup.style.display = "inline-flex";
        btnGroup.style.gap = "4px";
        
        const copyBtn = document.createElement("button");
        copyBtn.className = "secondary hist-icon";
        copyBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-copy"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>`;
        copyBtn.title = "Copy magnet link";
        copyBtn.onclick = async () => {
          try {
            await navigator.clipboard.writeText(item.magnet);
            copyBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-check"><polyline points="20 6 9 17 4 12"/></svg>`;
            toast("Magnet copied");
            setTimeout(() => {
              copyBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-copy"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>`;
            }, 1500);
          } catch (e) {
            toast("Copy failed");
          }
        };

        const makeAddBtn = (provider, label) => {
          const btn = document.createElement("button");
          btn.style.padding = "4px 8px";
          btn.style.fontSize = "11px";
          btn.textContent = label;
          btn.title = `Add to ${label}`;
          btn.onclick = async () => {
            if (provider === "offcloud" && !window.offcloudEnabled) {
              if (typeof window.promptOffcloudApiKey === "function") {
                window.promptOffcloudApiKey(async () => {
                  await performAdd();
                });
                return;
              }
            }
            await performAdd();

            async function performAdd() {
              btn.disabled = true;
              btn.textContent = "...";
              try {
                await postJson("/api/add", { magnet: item.magnet, provider: provider });
                toast(`Added to ${label}: ` + item.title);
                await saveToHistory(item.magnet, item.title, item.size); // Update timestamp
                btn.textContent = "✓";
              } catch (e) {
                toast("Failed: " + e.message);
                btn.disabled = false;
                btn.textContent = label;
              }
            }
          };
          return btn;
        };

        const addSeedrBtn = makeAddBtn("seedr", "Seedr");
        const addOffcloudBtn = makeAddBtn("offcloud", "Offcloud");
        
        const delBtn = document.createElement("button");
        delBtn.className = "danger ghost hist-icon";
        delBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-trash-2"><path d="M3 6h18M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2M10 11v6M14 11v6"/></svg>`;
        delBtn.title = "Remove from history";
        delBtn.onclick = async () => {
          delBtn.disabled = true;
          try {
            await postJson("/api/history/delete", { magnet: item.magnet });
            renderHistory();
          } catch(e) {
             toast("Failed to delete from history");
             delBtn.disabled = false;
          }
        };
        
        btnGroup.append(copyBtn, addSeedrBtn, addOffcloudBtn, delBtn);
        actionTd.appendChild(btnGroup);
        
        tr.append(nameTd, actionTd);
        tbody.appendChild(tr);
      });
    } catch(e) {
      tbody.innerHTML = "<tr><td colspan='2' class='error' style='text-align:center;'>Failed to load history</td></tr>";
    }
  }

  $("historyBtn").addEventListener("click", () => {
    if (typeof window.updateBottomNavHighlight === "function") window.updateBottomNavHighlight(2);
    renderHistory();
    $("historyOverlay").classList.remove("hidden");
  });

  $("closeHistoryBtn").addEventListener("click", () => {
    $("historyOverlay").classList.add("hidden");
    if (typeof window.restoreActiveMainTabHighlight === "function") window.restoreActiveMainTabHighlight();
  });

  $("clearHistoryBtn").addEventListener("click", async () => {
    if (confirm("Clear global magnet history?")) {
      await postJson("/api/history/clear", {});
      renderHistory();
    }
  });

