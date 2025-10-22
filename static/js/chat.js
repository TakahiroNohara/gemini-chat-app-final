// static/js/chat.js v1.5
console.log("[chat.js] Version 1.5 loaded - Summary display fix");
(() => {
  // ---------- 共通fetch ----------
  async function ajax(url, method = "GET", body = null, signal = undefined) {
    const csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || "";
    const res = await fetch(url, {
      method,
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrf,
      },
      credentials: "same-origin",
      body: body ? JSON.stringify(body) : null,
      signal
    });
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok || data?.ok === false) {
      const msg = data?.details || data?.error || `HTTP ${res.status}`;
      throw new Error(msg);
    }
    return data;
  }

  // /api/search_summarize 用のタイムアウト付き呼び出し（安全策）
  async function ajaxWithTimeout(url, method, body, timeoutMs = 180000) {  // 3分 - 長い検索・要約処理に対応
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      return await ajax(url, method, body, ctrl.signal);
    } finally {
      clearTimeout(t);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    // ---------- 要素 ----------
    const msgBox = document.getElementById("messages");
    const input = document.getElementById("input");           // textarea
    const sendBtn = document.getElementById("send");
    const modelSel = document.getElementById("model");
    const btnNew = document.getElementById("btn-new");
    const convList = document.getElementById("conv-list");
    const convSearch = document.getElementById("conv-search");
    const btnRefresh = document.getElementById("btn-refresh");
    const exportBtn = document.getElementById("btn-export");
    const summaryBox = document.getElementById("summary");
    const websearchToggle = document.getElementById("websearch-enabled");
    const deepResearchToggle = document.getElementById("deepresearch-enabled");
    const deepResearchProgress = document.getElementById("deep-research-progress");
    const researchStatus = document.getElementById("research-status");
    const researchPhase = document.getElementById("research-phase");
    const researchMessage = document.getElementById("research-message");
    const researchQueries = document.getElementById("research-queries");

    if (!msgBox || !input || !sendBtn) {
      console.warn("chat UI elements not found");
      return;
    }

    // ---------- 状態 ----------
    let currentConversationId = null;
    let sending = false;
    let deepResearchJobId = null;
    let deepResearchPollInterval = null;
    let deepResearchMasterTimeout = null;
    let deepResearchConversationId = null;  // Track which conversation the job belongs to
    let deepResearchPollingErrors = 0;
    let deepResearchAbortController = null;  // AbortController for canceling in-flight requests
    const DEEP_RESEARCH_TIMEOUT_MS = 900000; // 15 minutes (increased from 5)
    const MAX_POLL_INTERVAL = 60000; // Hard cap: 60 seconds

    // ---------- UIヘルパ ----------
    function showLoading(message = "処理中...") {
      const div = document.createElement("div");
      div.className = "loading-indicator";
      div.textContent = message;
      div.id = "loading-indicator";
      msgBox.appendChild(div);
      msgBox.scrollTop = msgBox.scrollHeight;
      return div;
    }

    function hideLoading() {
      const loader = document.getElementById("loading-indicator");
      if (loader) loader.remove();
    }

    function render(role, text, refs = []) {
      const div = document.createElement("div");
      div.className = "msg " + role;
      const p = document.createElement("div");
      p.textContent = text;
      div.appendChild(p);
      if (Array.isArray(refs) && refs.length) {
        const ref = document.createElement("div");
        ref.className = "refs";
        const label = document.createElement("div");
        label.textContent = "参考 / 出典";
        ref.appendChild(label);
        const ul = document.createElement("ul");
        ul.style.margin = "6px 0 0";
        ul.style.paddingLeft = "18px";
        refs.forEach((r, i) => {
          const li = document.createElement("li");
          const a = document.createElement("a");
          a.href = r.url;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          a.textContent = r.title ? `[${i + 1}] ${r.title}` : r.url;
          li.appendChild(a);
          ul.appendChild(li);
        });
        ref.appendChild(ul);
        div.appendChild(ref);
      }
      msgBox.appendChild(div);
      msgBox.scrollTop = msgBox.scrollHeight;
    }

    function setSummary(text) {
      if (!summaryBox) return;
      summaryBox.textContent = text ? `🧾 要約: ${text}` : "";
    }

    // ---------- Deep Research ヘルパー ----------
    function showDeepResearchProgress() {
      if (deepResearchProgress) {
        deepResearchProgress.style.display = "block";
      }
    }

    function hideDeepResearchProgress() {
      if (deepResearchProgress) {
        deepResearchProgress.style.display = "none";
      }
      // Abort any in-flight request
      if (deepResearchAbortController) {
        deepResearchAbortController.abort();
        deepResearchAbortController = null;
      }
      // Comprehensive cleanup of all Deep Research state
      if (deepResearchPollInterval) {
        clearTimeout(deepResearchPollInterval);  // Changed from clearInterval to clearTimeout
        deepResearchPollInterval = null;
      }
      if (deepResearchMasterTimeout) {
        clearTimeout(deepResearchMasterTimeout);
        deepResearchMasterTimeout = null;
      }
      deepResearchJobId = null;
      deepResearchConversationId = null;
      deepResearchPollingErrors = 0;
    }

    function updateDeepResearchUI(data) {
      if (!deepResearchProgress) return;

      // Status badge
      if (researchStatus) {
        researchStatus.textContent = data.status === "completed" ? "完了" :
          data.status === "failed" ? "失敗" : "進行中";
        researchStatus.className = "status-badge " +
          (data.status === "completed" ? "completed" :
            data.status === "failed" ? "failed" : "");
      }

      // Phase
      if (researchPhase && data.phase) {
        researchPhase.textContent = data.phase;
      }

      // Progress message
      if (researchMessage && data.progress_message) {
        researchMessage.textContent = data.progress_message;
      }

      // Sub-queries (if available) - hide if no sub-queries yet
      if (researchQueries) {
        if (data.sub_queries && data.sub_queries.length) {
          researchQueries.innerHTML = `サブクエリ数: ${data.sub_queries.length} | ソース数: ${data.sources_count || 0}`;
          researchQueries.style.display = "block";
        } else {
          researchQueries.style.display = "none";
        }
      }
    }

    async function startDeepResearch(query) {
      try {
        // Create Deep Research job
        const jobData = await ajax("/api/deep_research", "POST", {
          query: query,
          conversation_id: currentConversationId
        });

        if (!jobData.ok || !jobData.job_id) {
          throw new Error(jobData.error || "Failed to create Deep Research job");
        }

        deepResearchJobId = jobData.job_id;
        deepResearchConversationId = currentConversationId; // Remember which conversation this job belongs to
        deepResearchPollingErrors = 0;
        showDeepResearchProgress();
        updateDeepResearchUI({ status: "pending", phase: "初期化中...", progress_message: "Deep Research ジョブを開始しました" });

        // Set master timeout (15 minutes)
        deepResearchMasterTimeout = setTimeout(() => {
          hideDeepResearchProgress();
          hideLoading();
          render("assistant", "Deep Research がタイムアウトしました（15分経過）。もう一度お試しください。");
        }, DEEP_RESEARCH_TIMEOUT_MS);

        // Start polling for status with variable interval (exponential backoff for scalability)
        let pollCount = 0;
        const poll = async () => {
          try {
            // Critical: Check if conversation has been switched
            if (currentConversationId !== deepResearchConversationId) {
              console.warn("[DeepResearch] Conversation switched, stopping polling");
              hideDeepResearchProgress();
              return;
            }

            const statusData = await ajax(`/api/deep_research/status/${deepResearchJobId}`, "GET");

            // Reset error counter on successful poll
            deepResearchPollingErrors = 0;

            updateDeepResearchUI(statusData);

            // Check if completed or failed
            if (statusData.status === "completed") {
              // Save job_id before clearing (hideDeepResearchProgress sets it to null)
              const completedJobId = deepResearchJobId;
              const savedConversationId = deepResearchConversationId;
              hideDeepResearchProgress(); // Clears interval and timeout

              // Fetch final result using saved job_id
              const resultData = await ajax(`/api/deep_research/result/${completedJobId}`, "GET");

              hideLoading();

              // Display result (only if still in the same conversation)
              if (currentConversationId === savedConversationId) {
                // Wait briefly for backend to save messages to database
                await new Promise(resolve => setTimeout(resolve, 500));

                // Reload conversation history to show saved messages
                try {
                  const h = await ajax(`/api/history/${currentConversationId}`);
                  setSummary(h.summary || "");

                  // Clear and redraw all messages from database
                  msgBox.innerHTML = "";
                  (h.messages || []).forEach((m) => {
                    render(m.role === "assistant" ? "assistant" : "user", m.content);
                  });
                } catch (err) {
                  console.error("Failed to reload conversation:", err);
                  // Fallback: just display the result without reloading
                  render("assistant", resultData.result_report || "Deep Research が完了しました");
                }

                // Update conversation list in sidebar
                await loadConversations();
              }
              return; // Stop polling

            } else if (statusData.status === "failed") {
              hideDeepResearchProgress();
              hideLoading();
              render("assistant", `Deep Research エラー: ${statusData.error || "Unknown error"}`);
              return; // Stop polling
            }

            // Schedule next poll with variable interval (exponential backoff)
            // 2s for first 30s, then 5s for ~4min, then 10s thereafter
            pollCount++;
            const interval = pollCount < 15 ? 2000 : (pollCount < 60 ? 5000 : 10000);
            deepResearchPollInterval = setTimeout(poll, interval);

          } catch (err) {
            console.error("Deep Research polling error:", err);
            deepResearchPollingErrors++;

            // Stop polling after 3 consecutive errors
            if (deepResearchPollingErrors >= 3) {
              hideDeepResearchProgress();
              hideLoading();
              render("assistant", "Deep Research の進捗確認に失敗しました。ネットワーク接続を確認してください。");
              return; // Stop polling
            }

            // Schedule retry with same variable interval
            pollCount++;
            const interval = pollCount < 15 ? 2000 : (pollCount < 60 ? 5000 : 10000);
            deepResearchPollInterval = setTimeout(poll, interval);
          }
        };

        // Start initial poll
        poll();

      } catch (err) {
        hideDeepResearchProgress();
        hideLoading();
        throw err;
      }
    }

    function trimOneLine(s) {
      if (!s) return "";
      let t = String(s).split(/\r?\n/)[0] || "";
      t = t.replace(/^#{1,6}\s*/, "").replace(/^>\s*/, "").replace(/`{1,3}/g, "");
      return t.replace(/\s+/g, " ").trim();
    }
    function limitLen(s, max = 20) {
      if (!s) return "";
      return s.length > max ? s.slice(0, max - 1) + "…" : s;
    }
    function displayTitleFromConversation(c) {
      // 要約を優先的に表示（AIが生成した要約が最も会話の内容を表す）
      const summary = trimOneLine(c.summary || "");
      if (summary) {
        return limitLen(summary, 20);
      }
      // 要約がない場合はタイトルを使用
      const title = (c.title && c.title.trim()) ? c.title.trim() : "";
      if (title) {
        return limitLen(title, 20);
      }
      // どちらもない場合
      return "会話";
    }

    // ---------- サイドバー行 ----------
    function convRow(c) {
      const row = document.createElement("div");
      row.className = "item" + (c.id === currentConversationId ? " active" : "");
      row.dataset.id = c.id;

      const pin = document.createElement("span");
      pin.textContent = c.is_pinned ? "📌" : "📍";
      pin.className = "pin";
      pin.title = c.is_pinned ? "ピンを外す" : "ピン留め";
      pin.addEventListener("click", async (e) => {
        e.stopPropagation();
        try {
          await ajax(`/api/conversations/${c.id}`, "PATCH", { is_pinned: !c.is_pinned });
          await loadConversations();
        } catch (err) { alert(err.message); }
      });

      const title = document.createElement("div");
      title.className = "title";
      title.textContent = displayTitleFromConversation(c);

      const del = document.createElement("button");
      del.className = "ghost-btn";
      del.type = "button";
      del.textContent = "削除";
      del.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm("この会話を削除しますか？")) return;
        try {
          await ajax(`/api/conversations/${c.id}`, "DELETE");
          if (c.id === currentConversationId) {
            currentConversationId = null;
            msgBox.innerHTML = "";
            setSummary("");
          }
          await loadConversations();
        } catch (err) { alert(err.message); }
      });

      row.addEventListener("click", () => openConversation(c.id));
      row.addEventListener("contextmenu", async (e) => {
        e.preventDefault();
        const current = c.title || "";
        const fallback = displayTitleFromConversation(c);
        const newTitle = prompt("タイトルを変更", current || fallback);
        if (newTitle && newTitle.trim()) {
          try {
            await ajax(`/api/conversations/${c.id}`, "PATCH", { title: newTitle.trim() });
            await loadConversations();
          } catch (err) { alert(err.message); }
        }
      });

      row.appendChild(pin);
      row.appendChild(title);
      row.appendChild(del);
      return row;
    }

    // ---------- 会話一覧 ----------
    async function loadConversations() {
      console.log("[loadConversations] START");
      if (!convList) {
        console.log("[loadConversations] ERROR: convList element not found!");
        return;
      }
      const q = convSearch ? convSearch.value.trim() : "";
      console.log("[loadConversations] Search query:", q);

      const url = `/api/conversations${q ? `?q=${encodeURIComponent(q)}` : ""}`;
      console.log("[loadConversations] Fetching:", url);

      const data = await ajax(url);
      console.log("[loadConversations] API response:", data);

      const items = Array.isArray(data) ? data : (data.items || []);
      console.log("[loadConversations] Parsed items:", items.length, items);

      convList.innerHTML = "";
      if (!items.length) {
        console.log("[loadConversations] No conversations found, showing message");
        convList.innerHTML = `<div class="muted">会話はありません。</div>`;
        return;
      }

      console.log("[loadConversations] Creating conversation rows");
      const frag = document.createDocumentFragment();
      items.forEach((c, idx) => {
        console.log(`[loadConversations] Processing conversation ${idx}:`, c);
        const row = convRow(c);
        console.log(`[loadConversations] Created row for conversation ${c.id}:`, row);
        frag.appendChild(row);
      });

      console.log("[loadConversations] Appending fragment to convList");
      convList.appendChild(frag);
      console.log("[loadConversations] COMPLETE - Total conversations displayed:", items.length);
    }

    // ---------- 履歴 + 要約 ----------
    async function openConversation(id) {
      try {
        // Clean up any ongoing Deep Research polling when switching conversations
        if (deepResearchJobId !== null && currentConversationId !== id) {
          hideDeepResearchProgress();
        }

        const data = await ajax(`/api/history/${id}`);
        currentConversationId = id;
        if (convList) {
          Array.from(convList.querySelectorAll(".item")).forEach((el) => {
            el.classList.toggle("active", Number(el.dataset.id) === id);
          });
        }
        msgBox.innerHTML = "";
        (data.messages || []).forEach((m) => {
          render(m.role === "assistant" ? "assistant" : "user", m.content);
        });
        setSummary(data.summary || "");
      } catch (err) {
        alert("履歴取得エラー: " + err.message);
      }
    }

    // ---------- 送信 ----------
    async function doSend() {
      const text = input.value.trim();
      if (!text || sending) return;
      sendBtn.disabled = true; sending = true;

      try {
        // 会話が未作成なら作成
        if (!currentConversationId) {
          const created = await ajax("/api/conversations", "POST", { title: "新しい会話" });
          currentConversationId = created.id || created?.data?.id;
          await loadConversations();
        }

        // 先にユーザ吹き出し
        render("user", text);
        input.value = "";

        const useWebSearch = websearchToggle && websearchToggle.checked;
        const useDeepResearch = deepResearchToggle && deepResearchToggle.checked;

        // Deep Research has priority over Web Search
        if (useDeepResearch) {
          // Prevent multiple concurrent Deep Research jobs
          if (deepResearchJobId !== null) {
            alert("Deep Research ジョブが既に進行中です。完了してから新しいジョブを開始してください。");
            return;
          }

          // Deep Researchの場合、ユーザーメッセージを先にデータベースに保存
          try {
            await ajax("/api/chat", "POST", {
              conversation_id: currentConversationId,
              message: text,
              model: ""  // Deep Researchではモデル不要（AIレスポンスは返さない）
            });
          } catch (e) {
            console.error("Failed to save user message:", e);
            // エラーでも続行（メッセージは画面に表示されている）
          }

          // ローディング表示
          showLoading("Deep Research を開始しています... (数分かかる場合があります)");

          try {
            await startDeepResearch(text);
            // startDeepResearch handles its own UI updates via polling
            // Don't hide loading here - it will be hidden by polling callback
            return; // Exit early - polling will handle completion
          } catch (e) {
            hideLoading();
            hideDeepResearchProgress();
            throw e;
          }
        } else if (useWebSearch) {
          // ローディング表示
          showLoading("Web検索中... (最大3分かかることがあります)");

          try {
            // Web検索を使用する場合
            const searchPayload = {
              conversation_id: currentConversationId,
              query: text,
              model: (modelSel && modelSel.value) ? modelSel.value : "",
              top_k: 10
            };

            const chatData = await ajaxWithTimeout("/api/search_summarize", "POST", searchPayload, 180000);  // 3分
            const reply = chatData.answer || chatData.summary || chatData.text || "(no reply)";
            const refs = (chatData.citations || []).map(c => ({ title: c.title, url: c.url }));
            hideLoading();
            render("assistant", reply, refs);
          } catch (e) {
            hideLoading();
            throw e;
          }
        } else {
          // ローディング表示
          showLoading("応答生成中...");

          try {
            // 通常のチャット
            const chatPayload = {
              conversation_id: currentConversationId,
              message: text,
              model: (modelSel && modelSel.value) ? modelSel.value : ""
            };
            const chatData = await ajax("/api/chat", "POST", chatPayload);
            hideLoading();
            render("assistant", chatData.reply || "(no reply)");
          } catch (e) {
            hideLoading();
            throw e;
          }
        }

        // 要約（会話全体）を最新化 (Deep Research の場合はstartDeepResearch内で処理)
        if (!useDeepResearch) {
          try {
            const h = await ajax(`/api/history/${currentConversationId}`);
            setSummary(h.summary || "");
          } catch (_) {}

          // サイドバーのタイトル更新反映
          await loadConversations();
        }

      } catch (e) {
        render("assistant", "エラー: " + e.message);
      } finally {
        // Re-enable send button (Deep Research continues in background via polling)
        sending = false; sendBtn.disabled = false; input.focus();
      }
    }

    // ---------- イベント ----------
    sendBtn.type = "button";
    sendBtn.style.pointerEvents = "auto";
    sendBtn.addEventListener("click", doSend);

    // Shift+Enterで送信 / Enterで改行
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.shiftKey) {
        e.preventDefault();
        doSend();
      }
    });

    if (btnNew) {
      btnNew.type = "button";
      btnNew.addEventListener("click", async () => {
        try {
          const data = await ajax("/api/conversations", "POST", { title: "新しい会話" });
          const newId = data.id || data?.data?.id;
          await loadConversations();
          await openConversation(newId);
          render("assistant", "新しい会話を開始しました。最初のメッセージを送ってください。");
        } catch (e) {
          alert("新規作成エラー: " + e.message);
        }
      });
    }
    if (btnRefresh) btnRefresh.addEventListener("click", loadConversations);
    if (convSearch) convSearch.addEventListener("input", loadConversations);

    if (exportBtn) {
      exportBtn.addEventListener("click", async (e) => {
        e.preventDefault();
        if (!currentConversationId) return alert("会話がありません。");
        try {
          const data = await ajax(`/api/export/${currentConversationId}`, "GET");
          let md = `# 💬 会話「${data.title}」\n\n作成: ${data.created_at || "(不明)"}\n\n---\n\n`;
          for (const m of (data.messages || [])) {
            const role = m.role === "user" ? "👤 あなた" : "🤖 Gemini";
            md += `### ${role}\n${m.content}\n\n`;
          }
          const blob = new Blob([md], { type: "text/markdown" });
          const a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = `${(data.title || "conversation").replace(/\s+/g, "_")}.md`;
          a.click();
        } catch (err) {
          alert("エクスポート失敗: " + err.message);
        }
      });
    }

    // ---------- 初期ロード ----------
    (async () => {
      try { await loadConversations(); } catch (e) { console.warn("初期ロード失敗:", e); }
      sendBtn.disabled = false; sendBtn.style.pointerEvents = "auto";
    })();
  });
})();
