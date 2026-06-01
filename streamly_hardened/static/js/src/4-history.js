  async function saveToHistory(magnet, title) {
    try {
      await postJson("/api/history/add", { magnet: magnet, name: title || "Unknown Magnet" });
    } catch (e) {
      console.warn("Failed to save history", e);
      // Optional: toast("History save failed: " + (e.message || "Unknown error"));
    }
  }

  async function renderHistory() {
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
        
        const actionTd = document.createElement("td");
        actionTd.style.textAlign = "right";
        const btnGroup = document.createElement("div");
        btnGroup.style.display = "inline-flex";
        btnGroup.style.gap = "4px";
        
        const copyBtn = document.createElement("button");
        copyBtn.className = "secondary hist-icon";
        copyBtn.textContent = "📋";
        copyBtn.title = "Copy magnet link";
        copyBtn.onclick = async () => {
          try {
            await navigator.clipboard.writeText(item.magnet);
            copyBtn.textContent = "✓";
            toast("Magnet copied");
            setTimeout(() => { copyBtn.textContent = "📋"; }, 1500);
          } catch (e) {
            toast("Copy failed");
          }
        };

        const addBtn = document.createElement("button");
        addBtn.className = "hist-icon";
        addBtn.textContent = "+";
        addBtn.title = "Add to Destination";
        addBtn.onclick = async () => {
          addBtn.disabled = true;
          addBtn.textContent = "\u2026";
          try {
            await postJson("/api/add", { magnet: item.magnet });
            toast("Added from history: " + item.title);
            await saveToHistory(item.magnet, item.title); // Update timestamp
            addBtn.textContent = "✓";
          } catch (e) {
            toast("Failed: " + e.message);
            addBtn.disabled = false;
            addBtn.textContent = "+";
          }
        };
        
        const delBtn = document.createElement("button");
        delBtn.className = "danger ghost";
        delBtn.textContent = "✕";
        delBtn.style.padding = "6px 10px";
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
        
        btnGroup.append(copyBtn, addBtn, delBtn);
        actionTd.appendChild(btnGroup);
        
        tr.append(nameTd, actionTd);
        tbody.appendChild(tr);
      });
    } catch(e) {
      tbody.innerHTML = "<tr><td colspan='2' class='error' style='text-align:center;'>Failed to load history</td></tr>";
    }
  }

  $("historyBtn").addEventListener("click", () => {
    renderHistory();
    $("historyOverlay").classList.remove("hidden");
  });

  $("closeHistoryBtn").addEventListener("click", () => {
    $("historyOverlay").classList.add("hidden");
  });

  $("clearHistoryBtn").addEventListener("click", async () => {
    if (confirm("Clear global magnet history?")) {
      await postJson("/api/history/clear", {});
      renderHistory();
    }
  });

