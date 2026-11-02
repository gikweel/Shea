/* SHEA Control — preserves all panel loaders; chat shell redesigned */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const PREFS_KEY = "shea.gui.prefs";

const state = {
  events: [],
  agentAvatar: "",
  userAvatar: "",
  listening: false,
  recognition: null,
  attachments: [],
};

function loadPrefs() {
  try {
    return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
  } catch {
    return {};
  }
}

function applyPrefs(p) {
  const theme = p.theme || "ember";
  document.documentElement.setAttribute("data-theme", theme);
  if ($("#set-theme")) $("#set-theme").value = theme;

  const name = p.name || "SHEA";
  if ($("#brand-name")) $("#brand-name").textContent = name;
  if ($("#set-name")) $("#set-name").value = name;

  const mark = p.mark || "S";
  if ($("#brand-mark")) $("#brand-mark").textContent = mark;
  if ($("#set-mark")) $("#set-mark").value = mark;

  state.agentAvatar = p.agentAvatar || "";
  state.userAvatar = p.userAvatar || "";
  if ($("#set-agent-avatar")) $("#set-agent-avatar").value = state.agentAvatar;
  if ($("#set-user-avatar")) $("#set-user-avatar").value = state.userAvatar;

  if ($("#chat-session") && p.session) $("#chat-session").value = p.session;
  if ($("#chat-profile") && p.profile) $("#chat-profile").value = p.profile;
  if ($("#model-select") && p.model != null) $("#model-select").value = p.model;

  document.body.classList.toggle("no-grain", p.grain === false);
  if ($("#set-grain")) $("#set-grain").checked = p.grain !== false;

  document.body.classList.toggle("nav-anim", p.navAnim !== false);
  if ($("#set-nav-anim")) $("#set-nav-anim").checked = p.navAnim !== false;

  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) {
    const bg = getComputedStyle(document.documentElement)
      .getPropertyValue("--bg")
      .trim();
    if (bg) meta.setAttribute("content", bg);
  }
}

function savePrefs() {
  const p = {
    theme: $("#set-theme")?.value || "ember",
    name: $("#set-name")?.value || "SHEA",
    mark: $("#set-mark")?.value || "S",
    agentAvatar: $("#set-agent-avatar")?.value || "",
    userAvatar: $("#set-user-avatar")?.value || "",
    session: $("#chat-session")?.value || "",
    profile: $("#chat-profile")?.value || "default",
    model: $("#model-select")?.value || "",
    grain: $("#set-grain")?.checked !== false,
    navAnim: $("#set-nav-anim")?.checked !== false,
  };
  localStorage.setItem(PREFS_KEY, JSON.stringify(p));
  applyPrefs(p);
}

applyPrefs(loadPrefs());
$("#set-save")?.addEventListener("click", savePrefs);
$("#model-select")?.addEventListener("change", () => {
  const p = loadPrefs();
  p.model = $("#model-select").value;
  localStorage.setItem(PREFS_KEY, JSON.stringify(p));
});

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function shortId(id) {
  if (!id) return "—";
  return id.length > 14 ? id.slice(0, 10) + "…" : id;
}
function fmtTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return String(iso);
  }
}

// Modal & History Logic
const settingsModal = $("#settings-modal");
$("#nav-settings")?.addEventListener("click", () => {
  if (settingsModal) {
    settingsModal.showModal();
    // open settings view by default if nothing is active
    if (!$$(".modal-body > .view.active").length) {
      $("#view-settings")?.classList.add("active");
    }
  }
});
$("#close-modal")?.addEventListener("click", () => {
  if (settingsModal) settingsModal.close();
});

$$(".modal-sidebar .nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".modal-sidebar .nav-item").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const v = btn.dataset.view;
    $$(".modal-body > .view").forEach((el) => el.classList.remove("active"));
    $(`#view-${v}`)?.classList.add("active");
    ({
      tasks: loadTasks,
      pending: loadPending,
      memory: loadMemories,
      vault: loadVault,
      profiles: loadProfiles,
      tools: loadTools,
      audit: loadAudit,
      system: refreshSystem,
      settings: loadModelSettings,
    })[v]?.();
  });
});

$("#nav-new-chat")?.addEventListener("click", () => {
  window.location.hash = "";
});
$("#nav-current-chat")?.addEventListener("click", () => {
  // If we just want to jump to the current session or clear selection, it's just standard chat.
});

async function loadSessions() {
  try {
    const res = await fetch("/api/interaction/sessions");
    const sessions = await res.json();
    const list = $("#history-list");
    if (!list) return;
    list.innerHTML = "";

    sessions.forEach((s) => {
      const wrap = document.createElement("div");
      wrap.className = "history-item-wrap";

      const btn = document.createElement("button");
      btn.className = "history-item";
      if (s.session_id === $("#chat-session").value) {
        btn.classList.add("active");
      }
      btn.textContent = s.title || shortId(s.session_id);
      btn.addEventListener("click", () => {
        window.location.hash = "session=" + s.session_id;
      });

      const del = document.createElement("button");
      del.className = "ghost-ico del-session";
      del.innerHTML = "&times;";
      del.title = "Delete Chat";
      del.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (confirm("Delete this chat history?")) {
          await fetch(
            "/api/interaction/sessions/" + encodeURIComponent(s.session_id),
            { method: "DELETE" },
          );
          if ($("#chat-session").value === s.session_id) {
            window.location.hash = "";
          }
          loadSessions();
        }
      });

      wrap.appendChild(btn);
      wrap.appendChild(del);
      list.appendChild(wrap);
    });
  } catch (e) {
    console.error("Failed to load sessions", e);
  }
}

async function loadHistory(sessionId) {
  try {
    const res = await fetch(
      "/api/interaction/history?session_id=" + encodeURIComponent(sessionId),
    );
    const history = await res.json();

    const container = $("#chat-log");
    if (!container) return;

    // Keep the welcome element if there's no history
    if (history.length === 0) {
      // do nothing or reset to welcome
      return;
    }

    container.innerHTML = "";

    history.forEach((ev) => {
      if (ev.source === "user") {
        appendRow("user", escapeHtml(ev.content));
      } else {
        let out = ev.content || "";
        let parsed =
          typeof marked !== "undefined"
            ? `<div style="white-space: normal;">${marked.parse(out)}</div>`
            : escapeHtml(out);
        appendRow("agent", parsed);
      }
    });

    $("#chat-session").value = sessionId;
    $$(".history-item").forEach((b) => {
      b.classList.toggle("active", b.textContent.includes(shortId(sessionId)));
    });
  } catch (e) {
    console.error("Failed to load history", e);
  }
}

window.addEventListener("hashchange", () => {
  const hash = window.location.hash.replace("#", "");
  const params = new URLSearchParams(hash);
  const sessionId = params.get("session");

  if (sessionId) {
    $("#chat-session").value = sessionId;
    loadHistory(sessionId);
  } else {
    $("#chat-session").value = "";
    const chatLog = $("#chat-log");
    if (chatLog) {
      chatLog.innerHTML = `<div class="welcome" id="chat-welcome">
              <p class="welcome-title">What do you need done?</p>
              <p class="welcome-sub">Plans go through policy before anything runs.</p>
            </div>`;
    }
    loadSessions();
  }
});

setTimeout(() => {
  if (window.location.hash.includes("session=")) {
    window.dispatchEvent(new Event("hashchange"));
  } else {
    loadSessions();
  }
}, 500);

function hideWelcome() {
  $("#chat-welcome")?.remove();
}

function avatarHtml(kind) {
  const url = kind === "user" ? state.userAvatar : state.agentAvatar;
  if (url)
    return `<div class="avatar"><img src="${escapeHtml(url)}" alt="" /></div>`;
  const glyph =
    kind === "user"
      ? "You"
      : ($("#brand-mark")?.textContent || "S").slice(0, 1);
  return `<div class="avatar">${escapeHtml(glyph)}</div>`;
}

function appendRow(kind, htmlBody) {
  hideWelcome();
  const log = $("#chat-log");
  const row = document.createElement("div");
  row.className = `row ${kind}`;
  row.innerHTML = `${avatarHtml(kind)}<div class="bubble">${htmlBody}</div>`;
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
  return row;
}

function appendText(kind, text) {
  return appendRow(kind, escapeHtml(text));
}

function showTyping() {
  return appendRow(
    "agent",
    `<div class="typing" aria-label="Loading"><span></span><span></span><span></span></div>`,
  );
}

function replaceTyping(row, htmlBody) {
  const bubble = row.querySelector(".bubble");
  if (bubble) bubble.innerHTML = htmlBody;
}

function renderChips() {
  const el = $("#attach-chips");
  if (!el) return;
  if (!state.attachments.length) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  el.innerHTML = state.attachments
    .map(
      (f, i) =>
        `<span class="chip">${escapeHtml(f.name)} <button type="button" data-rm="${i}" aria-label="Remove">×</button></span>`,
    )
    .join("");
  $$("[data-rm]").forEach((b) => {
    b.onclick = () => {
      state.attachments.splice(Number(b.dataset.rm), 1);
      renderChips();
    };
  });
}

$("#upload-btn")?.addEventListener("click", () => $("#file-input")?.click());
$("#file-input")?.addEventListener("change", async (e) => {
  const files = Array.from(e.target.files || []);
  for (const file of files) {
    if (state.attachments.length >= 8) break;

    // Instead of reading text, upload it to the backend!
    const formData = new FormData();
    formData.append("file", file);

    try {
      const res = await fetch("/api/interaction/upload", {
        method: "POST",
        body: formData,
      });
      if (!res.ok) throw new Error("Upload failed");
      const data = await res.json();

      const entry = {
        name: data.filename,
        size: file.size,
        type: file.type,
        path: data.path,
        url: data.url,
      };
      state.attachments.push(entry);
    } catch (err) {
      console.error("Upload error:", err);
      // fallback just in case
      state.attachments.push({
        name: file.name,
        size: file.size,
        type: file.type,
        text: "Upload failed",
      });
    }
  }
  e.target.value = "";
  renderChips();
});

async function sendChat() {
  const input = $("#chat-input");
  const message = input.value.trim();
  if (!message && !state.attachments.length) return;
  input.value = "";

  let userLine = message;
  if (state.attachments.length) {
    userLine +=
      (userLine ? "\n" : "") +
      state.attachments.map((a) => `📎 ${a.name}`).join(", ");
  }
  appendText("user", userLine || "(attachment)");
  const typing = showTyping();

  const model = $("#model-select")?.value || "";
  const overrides = {};
  if (model) overrides.model_provider = model;
  if (state.attachments.length) {
    overrides.attachments = state.attachments.map((a) => ({
      name: a.name,
      size: a.size,
      type: a.type,
      path: a.path || null,
      url: a.url || null,
      text: a.text || null,
    }));
  }

  try {
    const res = await fetch("/api/interaction/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: message || "(see attachments)",
        session_id: $("#chat-session")?.value || "",
        profile_id: $("#chat-profile")?.value || "default",
        context_overrides: overrides,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);

    const currentSession = $("#chat-session")?.value;
    if (!currentSession && data.session_id) {
      if ($("#chat-session")) $("#chat-session").value = data.session_id;
      window.history.replaceState(null, "", "#session=" + data.session_id);
      loadSessions();
    }

    state.attachments = [];
    renderChips();

    if (data.needs_confirmation) {
      const taskId = data.task_id || data.confirmation?.task_id || "";
      replaceTyping(
        typing,
        `${escapeHtml(data.text || "Confirmation required")}<div class="confirm-card">
          <h3>Confirm execution</h3>
          <div>${escapeHtml(shortId(taskId))}</div>
          <div class="confirm-actions">
            <button class="btn primary" data-confirm="${escapeHtml(taskId)}">Confirm</button>
            <button class="btn danger" data-deny="${escapeHtml(taskId)}">Deny</button>
          </div>
        </div>`,
      );
      bindConfirmButtons();
      loadPending();
    } else {
      let out = data.text || "Done.";
      replaceTyping(typing, escapeHtml(out));
    }
  } catch (err) {
    replaceTyping(typing, escapeHtml(`Error: ${err.message}`));
  }
}

function bindConfirmButtons() {
  $$("[data-confirm]").forEach((btn) => {
    btn.onclick = async () => {
      const task_id = btn.dataset.confirm;
      const typing = showTyping();
      const res = await fetch("/api/interaction/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_id, actor: "web-user", acknowledge: true }),
      });
      const data = await res.json().catch(() => ({}));
      replaceTyping(
        typing,
        escapeHtml(data.text || (res.ok ? "Confirmed." : "Failed.")),
      );
      loadPending();
    };
  });
  $$("[data-deny]").forEach((btn) => {
    btn.onclick = async () => {
      const task_id = btn.dataset.deny;
      const res = await fetch("/api/interaction/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_id,
          actor: "web-user",
          acknowledge: false,
        }),
      });
      const data = await res.json().catch(() => ({}));
      appendText("agent", data.text || (res.ok ? "Denied." : "Failed."));
      loadPending();
    };
  });
}

$("#chat-send")?.addEventListener("click", sendChat);
$("#chat-input")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendChat();
  }
});

function setupMic() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const btn = $("#mic-btn");
  if (!SR || !btn) {
    if (btn) {
      btn.title = "Voice not supported here";
      btn.style.opacity = "0.35";
    }
    return;
  }
  const rec = new SR();
  rec.continuous = false;
  rec.interimResults = false;
  rec.lang = navigator.language || "en-US";
  rec.onresult = (ev) => {
    const text = ev.results[0][0].transcript;
    const input = $("#chat-input");
    if (input) input.value = (input.value ? input.value + " " : "") + text;
  };
  rec.onend = () => {
    state.listening = false;
    btn.classList.remove("live");
  };
  rec.onerror = () => {
    state.listening = false;
    btn.classList.remove("live");
  };
  state.recognition = rec;
  btn.addEventListener("click", () => {
    if (state.listening) {
      rec.stop();
      return;
    }
    state.listening = true;
    btn.classList.add("live");
    rec.start();
  });
}
setupMic();

function connectSSE() {
  try {
    const es = new EventSource("/api/interaction/stream");
    es.onmessage = (ev) => {
      let payload = {};
      try {
        payload = JSON.parse(ev.data);
      } catch {
        payload = { message: ev.data };
      }
      const type = payload.type || payload.event || "event";
      const message =
        payload.message ||
        payload.detail ||
        JSON.stringify(payload).slice(0, 160);
      state.events.unshift({ ts: new Date().toISOString(), type, message });
      if (state.events.length > 200) state.events.pop();
      renderEvents();
    };
  } catch {
    /* offline */
  }
}
function renderEvents() {
  const feed = $("#events-feed");
  if (!feed) return;
  if (!state.events.length) {
    feed.innerHTML = `<div class="empty">Waiting for events…</div>`;
    return;
  }
  feed.innerHTML = state.events
    .map(
      (e) => `<div class="event-line">
        <span class="event-ts">${escapeHtml(fmtTime(e.ts))}</span>
        <span class="event-type">${escapeHtml(e.type)}</span>
        <span>${escapeHtml(e.message)}</span>
      </div>`,
    )
    .join("");
}

async function loadTasks() {
  const body = $("#tasks-body");
  if (!body) return;
  try {
    const res = await fetch("/api/tasks/?limit=100");
    const rows = await res.json();
    body.innerHTML =
      (rows || [])
        .map(
          (t) => `<tr>
          <td class="mono">${escapeHtml(shortId(t.id))}</td>
          <td><span class="pill">${escapeHtml(t.state)}</span></td>
          <td>${escapeHtml(t.session_id || "—")}</td>
          <td>${escapeHtml(fmtTime(t.updated_at))}</td>
        </tr>`,
        )
        .join("") || `<tr><td colspan="4">None</td></tr>`;
  } catch (e) {
    body.innerHTML = `<tr><td colspan="4">${escapeHtml(e.message)}</td></tr>`;
  }
}
$("#tasks-refresh")?.addEventListener("click", loadTasks);

async function loadPending() {
  const list = $("#pending-list");
  const session = $("#chat-session")?.value || "";
  try {
    const res = await fetch(
      `/api/interaction/pending?session_id=${encodeURIComponent(session)}`,
    );
    const rows = await res.json();
    const badge = $("#pending-badge");
    if (badge) {
      badge.textContent = String(rows.length);
      badge.classList.toggle("hidden", !rows.length);
    }
    if (!list) return;
    if (!rows.length) {
      list.innerHTML = `<div class="empty">No pending confirmations.</div>`;
      return;
    }
    list.innerHTML = rows
      .map(
        (p) => `<div class="item-card">
          <div class="item-title">${escapeHtml(p.risk || "risk")} · ${escapeHtml(shortId(p.task_id))}</div>
          <div class="item-body">${escapeHtml(p.explanation || "")}</div>
          <div class="item-meta">
            <button class="btn primary" data-confirm="${escapeHtml(p.task_id)}">Confirm</button>
            <button class="btn danger" data-deny="${escapeHtml(p.task_id)}">Deny</button>
          </div>
        </div>`,
      )
      .join("");
    bindConfirmButtons();
  } catch (e) {
    if (list)
      list.innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}
$("#pending-refresh")?.addEventListener("click", loadPending);

async function loadMemories() {
  const list = $("#memory-list");
  const profile = $("#memory-profile")?.value || "default";
  if (!list) return;
  try {
    const res = await fetch(
      `/api/memory/?profile_id=${encodeURIComponent(profile)}`,
    );
    const rows = await res.json();
    list.innerHTML =
      (rows || [])
        .map(
          (m) => `<div class="item-card">
            <div class="item-title">${escapeHtml(m.type)}</div>
            <div class="item-body">${escapeHtml(m.content)}</div>
            <div class="item-meta"><button class="btn danger" data-del-mem="${escapeHtml(m.id)}">Delete</button></div>
          </div>`,
        )
        .join("") || `<div class="empty">Empty</div>`;
    $$("[data-del-mem]").forEach((b) => {
      b.onclick = async () => {
        await fetch(`/api/memory/${b.dataset.delMem}`, { method: "DELETE" });
        loadMemories();
      };
    });
  } catch (e) {
    list.innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}
$("#memory-refresh")?.addEventListener("click", loadMemories);

async function loadVault() {
  const list = $("#vault-list");
  if (!list) return;
  try {
    const res = await fetch("/api/credentials/");
    const rows = await res.json();
    list.innerHTML =
      (rows || [])
        .map(
          (c) => `<div class="item-card">
            <div class="item-title">${escapeHtml(c.name)}</div>
            <div class="item-body">${escapeHtml(c.description || "—")}</div>
            <div class="item-meta"><button class="btn danger" data-rev="${escapeHtml(c.id)}">Revoke</button></div>
          </div>`,
        )
        .join("") || `<div class="empty">Empty</div>`;
    $$("[data-rev]").forEach((b) => {
      b.onclick = async () => {
        await fetch(`/api/credentials/${b.dataset.rev}`, { method: "DELETE" });
        loadVault();
      };
    });
  } catch (e) {
    list.innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}
$("#vault-refresh")?.addEventListener("click", loadVault);
$("#vault-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  await fetch("/api/credentials/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: fd.get("name"),
      secret: fd.get("secret"),
      profile_id: fd.get("profile_id") || "default",
      allowed_tools: String(fd.get("allowed_tools") || "*")
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    }),
  });
  e.target.reset();
  loadVault();
});

const modelProviderDefaults = {
  "openai-compatible": {
    baseUrl: "https://api.openai.com/v1",
    model: "gpt-4o-mini",
  },
  ollama: {
    baseUrl: "http://127.0.0.1:11434/v1",
    model: "llama3.2",
  },
};

function modelProviderDefault(provider) {
  return modelProviderDefaults[provider.toLowerCase()] ||
    modelProviderDefaults["openai-compatible"];
}

function selectedModelProvider() {
  const provider = $("#model-provider").value;
  return provider === "custom" ? $("#custom-provider-name").value.trim() : provider;
}

function selectedModelName() {
  const model = $("#model-name").value;
  return model === "custom" ? $("#custom-model-name").value.trim() : model;
}

function toggleCustomModelInput(show) {
  const customName = $("#custom-model-name");
  customName.required = show;
  $("#custom-model-name-row").classList.toggle("hidden", !show);
}

function setModelName(model) {
  const options = Array.from($("#model-name").options).map((option) => option.value);
  const isKnownModel = options.includes(model);
  const selector = $("#model-name");
  const customName = $("#custom-model-name");
  selector.value = isKnownModel ? model : "custom";
  customName.value = isKnownModel ? "" : model || "";
  toggleCustomModelInput(!isKnownModel);
}

function setModelProvider(provider) {
  const knownProvider = Object.keys(modelProviderDefaults).find(
    (name) => name.toLowerCase() === provider?.toLowerCase(),
  );
  const selector = $("#model-provider");
  const customName = $("#custom-provider-name");
  const customRow = $("#custom-provider-name-row");
  selector.value = knownProvider || "custom";
  customName.value = knownProvider ? "" : provider || "";
  customName.required = !knownProvider;
  customRow.classList.toggle("hidden", Boolean(knownProvider));
}

async function loadModelSettings() {
  const status = $("#model-settings-status");
  try {
    const response = await fetch("/api/model-settings/");
    const settings = await response.json();
    if (!response.ok) throw new Error(settings.detail || "Could not load model settings.");

    const baseUrl = $("#model-base-url");
    const apiKey = $("#model-api-key");
    const selectedProvider = settings.provider || "openai-compatible";
    const chatModel = $("#model-select");
    const defaultModelOption = chatModel?.querySelector('option[value=""]');
    if (defaultModelOption) {
      defaultModelOption.textContent = settings.configured && settings.model_name
        ? `configured · ${settings.model_name}`
        : "default";
      defaultModelOption.title = settings.configured
        ? "Use the custom model configured in Settings"
        : "Use the default model provider";
    }
    setModelProvider(selectedProvider);
    baseUrl.value =
      settings.base_url || modelProviderDefault(selectedProvider).baseUrl;
    setModelName(settings.model_name || modelProviderDefault(selectedProvider).model);
    apiKey.value = "";
    apiKey.placeholder = settings.api_key_configured
      ? "Leave blank to keep the saved key"
      : selectedProvider.toLowerCase() === "ollama"
        ? "Not required for local Ollama"
        : "Enter your own provider key";
    $("#model-clear").classList.toggle("hidden", !settings.configured);
    if (status) {
      status.textContent = settings.configured
        ? `Saved: ${selectedProvider}${settings.api_key_configured ? " (API key stored securely)" : ""}`
        : "No interface-saved provider. Existing environment providers remain available.";
    }
  } catch (error) {
    if (status) status.textContent = error.message;
  }
}

$("#model-provider")?.addEventListener("change", (event) => {
  const selectedProvider = event.target.value;
  const customName = $("#custom-provider-name");
  const customRow = $("#custom-provider-name-row");
  const isCustom = selectedProvider === "custom";
  customName.required = isCustom;
  customRow.classList.toggle("hidden", !isCustom);
  const defaults = modelProviderDefault(
    isCustom ? "openai-compatible" : selectedProvider,
  );
  $("#model-base-url").value = defaults.baseUrl;
  setModelName(defaults.model);
  $("#model-api-key").value = "";
  $("#model-api-key").placeholder =
    selectedProvider === "ollama"
      ? "Not required for local Ollama"
      : "Enter your own provider key";
});

$("#model-name")?.addEventListener("change", (event) => {
  toggleCustomModelInput(event.target.value === "custom");
});

$("#model-provider-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const status = $("#model-settings-status");
  if (status) status.textContent = "Saving provider settings…";
  try {
    const response = await fetch("/api/model-settings/", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider: selectedModelProvider(),
        base_url: $("#model-base-url").value,
        model_name: selectedModelName(),
        api_key: $("#model-api-key").value,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Could not save provider settings.");
    await loadModelSettings();
    if (status) status.textContent = "Provider saved and ready to use.";
  } catch (error) {
    if (status) status.textContent = error.message;
  }
});

$("#model-clear")?.addEventListener("click", async () => {
  if (!window.confirm("Remove the saved provider and API key from the keychain?")) return;
  const status = $("#model-settings-status");
  try {
    const response = await fetch("/api/model-settings/", { method: "DELETE" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Could not clear model settings.");
    await loadModelSettings();
    if (status) status.textContent = "Saved provider removed.";
  } catch (error) {
    if (status) status.textContent = error.message;
  }
});

async function loadProfiles() {
  const list = $("#profiles-list");
  if (!list) return;
  try {
    const res = await fetch("/api/profiles/");
    const rows = await res.json();
    list.innerHTML =
      (rows || [])
        .map(
          (p) => `<div class="item-card">
            <div class="item-title">${escapeHtml(p.name)} <span class="mono">${escapeHtml(p.id)}</span></div>
          </div>`,
        )
        .join("") || `<div class="empty">Empty</div>`;
  } catch (e) {
    list.innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}
$("#profiles-refresh")?.addEventListener("click", loadProfiles);
$("#profile-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const id = String(fd.get("id"));
  let rules = {};
  try {
    rules = JSON.parse(String(fd.get("context_rules") || "{}"));
  } catch {
    return;
  }
  await fetch(`/api/profiles/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id,
      name: fd.get("name"),
      preferences: {},
      context_rules: rules,
    }),
  });
  loadProfiles();
});

async function loadTools() {
  const body = $("#tools-body");
  const empty = $("#tools-empty");
  try {
    const res = await fetch("/api/tools/");
    if (res.status === 404) {
      empty?.classList.remove("hidden");
      return;
    }
    const rows = await res.json();
    empty?.classList.add("hidden");
    if (body)
      body.innerHTML = (rows || [])
        .map(
          (t) => `<tr>
            <td class="mono">${escapeHtml(t.name)}</td>
            <td>${(t.capabilities || [])
              .map((c) => `<span class="pill">${escapeHtml(c)}</span>`)
              .join(" ")}</td>
            <td>${escapeHtml(t.description || "")}</td>
          </tr>`,
        )
        .join("");
  } catch {
    empty?.classList.remove("hidden");
  }
}
$("#tools-refresh")?.addEventListener("click", loadTools);

async function loadAudit() {
  const body = $("#audit-body");
  if (!body) return;
  try {
    const res = await fetch("/api/observability/audit?limit=100");
    const data = await res.json();
    const events = data.events || [];
    body.innerHTML =
      events
        .map(
          (e) => `<tr>
          <td class="mono">${escapeHtml(String(e.sequence_number ?? "—"))}</td>
          <td>${escapeHtml(fmtTime(e.timestamp || e.created_at))}</td>
          <td>${escapeHtml(e.event_type || e.type || "—")}</td>
          <td class="mono">${escapeHtml(shortId(e.task_id))}</td>
        </tr>`,
        )
        .join("") || `<tr><td colspan="4">None</td></tr>`;
  } catch (e) {
    body.innerHTML = `<tr><td colspan="4">${escapeHtml(e.message)}</td></tr>`;
  }
}
$("#audit-refresh")?.addEventListener("click", loadAudit);

let sysChart = null;
const chartData = { labels: [], cpu: [], ram: [], disk: [] };

function initChart() {
  const canvas = $("#sys-chart");
  if (!canvas || typeof Chart === "undefined" || sysChart) return;
  const ink =
    getComputedStyle(document.body).getPropertyValue("--ink-soft").trim() ||
    "#888";
  sysChart = new Chart(canvas, {
    type: "line",
    data: {
      labels: chartData.labels,
      datasets: [
        {
          label: "CPU %",
          data: chartData.cpu,
          borderColor: "#e8a05c",
          tension: 0.35,
          pointRadius: 0,
        },
        {
          label: "RAM %",
          data: chartData.ram,
          borderColor: "#7aa2ff",
          tension: 0.35,
          pointRadius: 0,
        },
        {
          label: "Disk %",
          data: chartData.disk,
          borderColor: "#6dbf8a",
          tension: 0.35,
          pointRadius: 0,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      scales: {
        y: {
          beginAtZero: true,
          max: 100,
          ticks: { color: ink },
          grid: { color: "rgba(128,128,128,0.1)" },
        },
        x: {
          ticks: { color: ink, maxTicksLimit: 6 },
          grid: { display: false },
        },
      },
      plugins: { legend: { labels: { color: ink } } },
    },
  });
}

function updateChart(m) {
  initChart();
  if (!sysChart) return;
  chartData.labels.push(new Date().toLocaleTimeString());
  chartData.cpu.push(m.cpu_percent ?? 0);
  chartData.ram.push(m.ram_percent ?? 0);
  chartData.disk.push(m.disk_percent ?? 0);
  if (chartData.labels.length > 20) {
    chartData.labels.shift();
    chartData.cpu.shift();
    chartData.ram.shift();
    chartData.disk.shift();
  }
  sysChart.update();
}

async function refreshSystem() {
  try {
    const res = await fetch("/api/observability/metrics");
    const m = await res.json();
    if ($("#m-cpu")) {
      $("#m-cpu").textContent = `${(m.cpu_percent ?? 0).toFixed(1)}%`;
      if ($("#m-load"))
        $("#m-load").textContent = `load: ${(m.load_1m ?? 0).toFixed(2)}`;
      $("#m-ram").textContent = `${(m.ram_percent ?? 0).toFixed(1)}%`;
      if ($("#m-ram-detail"))
        $("#m-ram-detail").textContent =
          `${Math.round(m.ram_used_mb ?? 0)} / ${Math.round(m.ram_total_mb ?? 0)} MB`;
      $("#m-disk").textContent = `${(m.disk_percent ?? 0).toFixed(1)}%`;
      if ($("#m-disk-detail"))
        $("#m-disk-detail").textContent =
          `${(m.disk_used_gb ?? 0).toFixed(1)} / ${(m.disk_total_gb ?? 0).toFixed(1)} GB`;
      $("#m-prov").textContent = `${m.provider_health ?? "—"}`;
      if ($("#m-rpm"))
        $("#m-rpm").textContent = `${(m.requests_per_minute ?? 0).toFixed(1)}`;
      if ($("#m-budget"))
        $("#m-budget").textContent =
          `${((m.rate_limit_remaining ?? 0) * 100).toFixed(1)}%`;
      if ($("#m-rl-hits"))
        $("#m-rl-hits").textContent = `hits: ${m.rate_limit_hits ?? 0}`;
      if ($("#m-active")) $("#m-active").textContent = `${m.active_tasks ?? 0}`;
      if ($("#m-sec")) $("#m-sec").textContent = `${m.security_blocks ?? 0}`;
    }
    updateChart(m);
  } catch (err) {
    console.error("refreshSystem error:", err);
  }
}
$("#sys-refresh")?.addEventListener("click", refreshSystem);

connectSSE();
refreshSystem();
setInterval(refreshSystem, 10000);
loadPending();

// Custom Option Popout
function createCustomSelects() {
  document.querySelectorAll("select").forEach((select) => {
    // only do this once
    if (select.dataset.customized) return;
    select.dataset.customized = "true";

    select.style.display = "none";

    const wrapper = document.createElement("div");
    wrapper.className = "custom-select-wrapper";
    wrapper.style.position = "relative";
    wrapper.style.display = "inline-block";
    wrapper.style.width = "100%";

    const trigger = document.createElement("div");
    trigger.className = "custom-select-trigger field";
    trigger.style.cursor = "pointer";
    trigger.style.display = "flex";
    trigger.style.justifyContent = "space-between";
    trigger.style.alignItems = "center";

    const isModelPill = select.id === "model-select";
    if (isModelPill) {
      trigger.style.background = "transparent";
      trigger.style.border = "none";
      trigger.style.padding = "2px 6px 2px 4px";
      trigger.style.fontWeight = "500";
      trigger.style.fontSize = "12.5px";
    } else {
      trigger.style.background = "var(--bg)";
      trigger.style.border = "1px solid var(--line)";
      trigger.style.borderRadius = "8px";
      trigger.style.padding = "8px 10px";
    }

    trigger.innerHTML = `<span>${select.options[select.selectedIndex]?.text || ""}</span>`;

    const menu = document.createElement("div");
    menu.className = "custom-select-menu";
    menu.style.position = "absolute";

    menu.style.top = "100%";

    menu.style.left = "0";
    menu.style.width = "100%";
    menu.style.background = "var(--bg)";
    menu.style.border = "1px solid var(--line)";
    menu.style.borderRadius = isModelPill ? "12px" : "8px";
    if (isModelPill) menu.style.minWidth = "140px";

    menu.style.marginTop = "4px";
    menu.style.zIndex = "9999";
    menu.style.display = "none";
    menu.style.maxHeight = "200px";
    menu.style.overflowY = "auto";
    menu.style.boxShadow = "var(--shadow)";

    Array.from(select.options).forEach((opt) => {
      const item = document.createElement("div");
      item.className = "custom-select-item";
      item.style.padding = "8px 10px";
      item.style.cursor = "pointer";
      item.textContent = opt.text;

      item.addEventListener("click", (e) => {
        e.stopPropagation(); // prevent label click
        e.preventDefault();
        select.value = opt.value;
        select.dispatchEvent(new Event("change"));
        trigger.querySelector("span").textContent = opt.text;
        menu.style.display = "none";

        // update active style
        Array.from(menu.children).forEach(
          (c) => (c.style.background = "transparent"),
        );
        item.style.background = "var(--accent-soft)";
      });

      item.addEventListener(
        "mouseenter",
        () => (item.style.background = "var(--accent-soft)"),
      );
      item.addEventListener("mouseleave", () => {
        if (select.value !== opt.value) item.style.background = "transparent";
      });

      if (select.value === opt.value) {
        item.style.background = "var(--accent-soft)";
      }

      menu.appendChild(item);
    });

    // Toggle menu
    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      e.preventDefault();
      const isVisible = menu.style.display === "block";
      document
        .querySelectorAll(".custom-select-menu")
        .forEach((m) => (m.style.display = "none"));
      if (!isVisible) {
        menu.style.display = "block";
        const rect = trigger.getBoundingClientRect();
        if (rect.top > window.innerHeight - 250) {
          menu.style.bottom = "100%";
          menu.style.top = "auto";
          menu.style.marginTop = "0";
          menu.style.marginBottom = "4px";
        } else {
          menu.style.top = "100%";
          menu.style.bottom = "auto";
          menu.style.marginTop = "4px";
          menu.style.marginBottom = "0";
        }
      }
    });

    wrapper.appendChild(trigger);
    wrapper.appendChild(menu);
    select.parentNode.insertBefore(wrapper, select.nextSibling);
  });

  // Close all on outside click
  document.addEventListener("click", () => {
    document
      .querySelectorAll(".custom-select-menu")
      .forEach((m) => (m.style.display = "none"));
  });
}

// run it once DOM is fully set
setTimeout(createCustomSelects, 500);
