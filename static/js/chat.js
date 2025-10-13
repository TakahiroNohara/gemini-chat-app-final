// static/js/chat.js
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
  async function ajaxWithTimeout(url, method, body, timeoutMs = 15000) {
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

    if (!msgBox || !input || !sendBtn) {
      console.warn("chat UI elements not found");
      return;
    }

    // ---------- 状態 ----------
    let currentConversationId = null;
    let sending = false;

    // ---------- UIヘルパ ----------
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
    function trimOneLine(s) {
      if (!s) return "";
      let t = String(s).split(/\r?\n/)[0] || "";
      t = t.replace(/^#{1,6}\s*/, "").replace(/^>\s*/, "").replace(/`{1,3}/g, "");
      return t.replace(/\s+/g, " ").trim();
    }
    function limitLen(s, max = 34) {
      if (!s) return "";
      return s.length > max ? s.slice(0, max - 1) + "…" : s;
    }
    function displayTitleFromConversation(c) {
      const base = (c.title && c.title.trim())
        ? c.title.trim()
        : (trimOneLine(c.summary || "") || "会話");
      return limitLen(base, 34);
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
      if (!convList) return;
      const q = convSearch ? convSearch.value.trim() : "";
      const data = await ajax(`/api/conversations${q ? `?q=${encodeURIComponent(q)}` : ""}`);
      const items = Array.isArray(data) ? data : (data.items || []);
      convList.innerHTML = "";
      if (!items.length) {
        convList.innerHTML = `<div class="muted">会話はありません。</div>`;
        return;
      }
      const frag = document.createDocumentFragment();
      items.forEach((c) => frag.appendChild(convRow(c)));
      convList.appendChild(frag);
    }

    // ---------- 履歴 + 要約 ----------
    async function openConversation(id) {
      try {
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

    // ---------- Web検索API（/api/search_summarize） ----------
    async function searchAndSummarize(queryText) {
      const payload = {
        query: queryText,
        top_k: 5,
        model: (modelSel && modelSel.value) ? modelSel.value : ""
      };
      const data = await ajaxWithTimeout("/api/search_summarize", "POST", payload, 15000);
      const refs = (data.citations || []).map(c => ({ title: c.title, url: c.url }));
      return { summary: data.summary || "", refs };
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

        // 1) 通常チャット返信
        const chatPayload = {
          conversation_id: currentConversationId,
          history: [],
          message: text,
          model: (modelSel && modelSel.value) ? modelSel.value : ""
        };
        const chatData = await ajax("/api/chat", "POST", chatPayload);
        render("assistant", chatData.reply || "(no reply)");

        // 2) Web検索トグルがONなら、検索→要約も続けて出す（失敗してもチャットは出る）
        if (websearchToggle && websearchToggle.checked) {
          try {
            const { summary, refs } = await searchAndSummarize(text);
            if (summary) {
              render("assistant", summary, refs);
            } else {
              render("assistant", "（Web要約：該当情報の抽出に失敗しました）");
            }
          } catch (e) {
            render("assistant", "（Web要約エラー: " + e.message + "）");
          }
        }

        // 3) 要約（会話全体）を最新化
        try {
          const h = await ajax(`/api/history/${currentConversationId}`);
          setSummary(h.summary || "");
        } catch (_) {}

        // 4) サイドバーのタイトル更新反映
        await loadConversations();

      } catch (e) {
        render("assistant", "エラー: " + e.message);
      } finally {
        sending = false; sendBtn.disabled = false; input.focus();
      }
    }

    // ---------- イベント ----------
    sendBtn.type = "button";
    sendBtn.style.pointerEvents = "auto";
    sendBtn.addEventListener("click", doSend);

    // Enter=改行 / Shift+Enter=送信
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
