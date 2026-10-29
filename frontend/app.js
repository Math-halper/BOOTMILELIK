"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const VIEW_TITLES = {
  chat: "Chat with Milelik",
  workspace: "Workspace — code on this device",
  apis: "Local APIs for coding, maths and more",
  settings: "Preferences and setup",
};

const state = {
  settings: {},
  status: null,
  endpoints: [],
  approvalsTimer: null,
  activeApproval: null,
  chatBusy: false,
  editorDirty: false,
  loadedWorkspace: false,
};

function savedToken() {
  try { return localStorage.getItem("milelikToken") || ""; } catch { return ""; }
}

function storeToken(token) {
  try {
    if (token) localStorage.setItem("milelikToken", token);
    else localStorage.removeItem("milelikToken");
  } catch { /* private browsing */ }
}

function toast(message, isError = false, timeout = 4200) {
  const box = $("#toast");
  box.textContent = message;
  box.classList.toggle("error", isError);
  box.hidden = false;
  window.clearTimeout(toast._timer);
  toast._timer = window.setTimeout(() => { box.hidden = true; }, timeout);
}

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  const token = savedToken();
  if (token) headers["X-Milelik-Token"] = token;
  let body = options.body;
  if (body !== undefined && typeof body !== "string") {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  const response = await fetch(path, { method, headers, body, credentials: "same-origin" });
  const raw = options.raw ? await response.blob() : await response.text();
  if (options.raw) {
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    return raw;
  }
  let data = {};
  if (raw) {
    try { data = JSON.parse(raw); }
    catch { data = { text: raw }; }
  }
  if (!response.ok) {
    if (response.status === 401) $("#connectionBanner").hidden = false;
    throw new Error(data.error || `Request failed (${response.status})`);
  }
  if (path.startsWith("/api/")) $("#connectionBanner").hidden = true;
  return data;
}

function formatResult(value) {
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

function setResult(selector, value) {
  const node = $(selector);
  if (node) node.textContent = formatResult(value);
}

function errorText(error) {
  return error && error.message ? error.message : String(error);
}

function showError(selector, error) {
  setResult(selector, `Error: ${errorText(error)}`);
  toast(errorText(error), true);
}

function applyTheme(theme) {
  const chosen = theme || "system";
  const resolved = chosen === "system"
    ? (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark")
    : chosen;
  document.documentElement.dataset.theme = resolved;
}

function showView(view) {
  if (!VIEW_TITLES[view]) view = "chat";
  $$(".nav-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  $$(".view").forEach((section) => section.classList.remove("active"));
  $(`#view-${view}`).classList.add("active");
  $("#pageTitle").textContent = VIEW_TITLES[view];
  try { localStorage.setItem("milelikCurrentView", view); } catch { /* ignore */ }
  if (view === "workspace" && !state.loadedWorkspace) loadWorkspace().catch((e) => showError("#workspaceOutput", e));
  if (view === "apis" && !state.endpoints.length) loadApiDocs().catch((e) => showError("#apiResult", e));
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* Status/settings --------------------------------------------------------- */

function fillSettings(settings) {
  state.settings = { ...settings };
  const values = {
    "set-model": settings.model || "",
    "set-backend": settings.backend || "auto",
    "set-host": settings.host || "",
    "set-ctx": settings.ctx ?? 8192,
    "set-max-steps": settings.max_steps ?? 12,
    "set-cmd-timeout": settings.cmd_timeout ?? 60,
    "set-workspace": settings.workspace || "",
    "set-web-host": settings.web_host || "127.0.0.1",
    "set-web-port": settings.web_port ?? 8765,
    "set-ui-theme": settings.ui_theme || "system",
    "set-ui-default-view": settings.ui_default_view || "chat",
  };
  for (const [id, value] of Object.entries(values)) {
    const input = document.getElementById(id);
    if (input) input.value = value;
  }
  $("#set-auto").checked = Boolean(settings.auto);
  $("#set-open-browser").checked = settings.open_browser !== false;
  $("#set-frontend-enabled").checked = settings.frontend_enabled !== false;
  applyTheme(settings.ui_theme);
}

function renderModelOptions(models) {
  const list = $("#modelOptions");
  list.replaceChildren();
  for (const model of models || []) {
    const option = document.createElement("option");
    option.value = model;
    list.append(option);
  }
}

async function refreshStatus() {
  const dot = $("#statusDot");
  dot.className = "status-dot checking";
  $("#statusText").textContent = "Checking local services…";
  try {
    const status = await api("/api/status");
    state.status = status;
    fillSettings(status.settings || {});
    const model = status.model || {};
    const available = Boolean(model.model_available);
    dot.className = `status-dot ${available ? "ok" : "bad"}`;
    $("#statusText").textContent = available ? "Local model ready" : "Front end ready — model needs attention";
    $("#modelSummary").textContent = available
      ? `${model.selected_model || model.configured_model} via ${model.selected_backend || model.configured_backend}`
      : `${model.configured_model || "No model configured"} — open Preferences or start Ollama/LM Studio`;
    $("#workspaceSummary").textContent = status.workspace || "";
    $("#workspaceRootLabel").textContent = status.workspace || "";
    renderModelOptions(model.available_models || []);

    const tools = $("#toolList");
    tools.replaceChildren();
    for (const tool of status.tools || []) {
      const code = document.createElement("code");
      code.textContent = tool;
      tools.append(code);
    }
    return status;
  } catch (error) {
    dot.className = "status-dot bad";
    $("#statusText").textContent = "Could not connect to Milelik API";
    $("#modelSummary").textContent = errorText(error);
    throw error;
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const payload = {
    model: $("#set-model").value.trim(),
    backend: $("#set-backend").value,
    host: $("#set-host").value.trim(),
    ctx: Number($("#set-ctx").value),
    max_steps: Number($("#set-max-steps").value),
    cmd_timeout: Number($("#set-cmd-timeout").value),
    auto: $("#set-auto").checked,
    workspace: $("#set-workspace").value.trim(),
    web_host: $("#set-web-host").value.trim(),
    web_port: Number($("#set-web-port").value),
    open_browser: $("#set-open-browser").checked,
    frontend_enabled: $("#set-frontend-enabled").checked,
    ui_theme: $("#set-ui-theme").value,
    ui_default_view: $("#set-ui-default-view").value,
  };
  try {
    const result = await api("/api/settings", { method: "POST", body: payload });
    fillSettings(result.settings || payload);
    try { localStorage.setItem("milelikCurrentView", payload.ui_default_view); } catch { /* ignore */ }
    state.loadedWorkspace = false;
    $("#settingsStatus").textContent = result.restart_required_for_web_address
      ? "Saved. Restart milelik --gui to use the new web address/port."
      : "Saved — the CLI and front end will use these preferences.";
    await refreshStatus();
    if ($("#view-workspace").classList.contains("active")) await loadWorkspace();
    toast("Preferences saved.");
  } catch (error) {
    $("#settingsStatus").textContent = `Could not save: ${errorText(error)}`;
    toast(errorText(error), true);
  }
}

/* Approvals --------------------------------------------------------------- */

function setApprovalButtonsEnabled() {
  const approval = state.activeApproval;
  const approve = $("#approveApprovalBtn");
  if (!approval) return;
  approve.disabled = Boolean(approval.dangerous_reason) && $("#approvalTypedYes").value !== "YES";
}

async function pollApprovals() {
  try {
    const data = await api("/api/approvals");
    const approvals = data.approvals || [];
    if (!approvals.length) {
      state.activeApproval = null;
      $("#approvalModal").hidden = true;
      return;
    }
    const approval = approvals[0];
    if (state.activeApproval?.id === approval.id && !$("#approvalModal").hidden) return;
    state.activeApproval = approval;
    $("#approvalAction").textContent = `Allow Milelik to ${approval.action}?`;
    $("#approvalDetail").textContent = approval.detail || "(no extra detail)";
    const bits = [];
    if (approval.sudo) bits.push("Uses sudo");
    if (approval.dangerous_reason) bits.push(`Danger: ${approval.dangerous_reason}`);
    bits.push(`Requested at ${approval.created_at || "now"}`);
    $("#approvalMeta").textContent = bits.join(" · ");
    $("#approvalYesWrap").hidden = !approval.dangerous_reason;
    $("#approvalTypedYes").value = "";
    $("#approvalModal").hidden = false;
    setApprovalButtonsEnabled();
    $("#denyApprovalBtn").focus();
  } catch {
    /* Status refresh will surface authentication/connection problems. */
  }
}

function startApprovalPolling() {
  pollApprovals();
  if (!state.approvalsTimer) state.approvalsTimer = window.setInterval(pollApprovals, 900);
}

async function decideApproval(approved) {
  const approval = state.activeApproval;
  if (!approval) return;
  try {
    await api(`/api/approvals/${encodeURIComponent(approval.id)}/decide`, {
      method: "POST",
      body: { approved, typed_yes: $("#approvalTypedYes").value },
    });
    state.activeApproval = null;
    $("#approvalModal").hidden = true;
    toast(approved ? "Action approved." : "Action denied.");
  } catch (error) {
    toast(errorText(error), true);
    setApprovalButtonsEnabled();
  }
}

/* Chat -------------------------------------------------------------------- */

function appendMessage(role, content, isError = false) {
  const container = $("#chatMessages");
  const empty = $(".empty-state", container);
  if (empty) empty.remove();
  const article = document.createElement("article");
  article.className = `message ${role}${isError ? " error" : ""}`;
  const label = document.createElement("span");
  label.className = "message-role";
  label.textContent = role === "user" ? "You" : "Milelik";
  const text = document.createElement("span");
  text.textContent = content;
  article.append(label, text);
  container.append(article);
  container.scrollTop = container.scrollHeight;
  return article;
}

async function loadChatHistory() {
  try {
    const data = await api("/api/chat/history");
    if ((data.messages || []).length) {
      $("#chatMessages").replaceChildren();
      for (const message of data.messages) appendMessage(message.role, message.content || "");
    }
  } catch { /* A fresh server simply has no history. */ }
}

async function submitChat(event) {
  event?.preventDefault();
  if (state.chatBusy) return;
  const input = $("#chatInput");
  const message = input.value.trim();
  if (!message) return;
  appendMessage("user", message);
  input.value = "";
  state.chatBusy = true;
  $("#sendChatBtn").disabled = true;
  $("#sendChatBtn").textContent = "Milelik is working…";
  $("#chatBusy").textContent = "Milelik is thinking. Approval dialogs may appear here.";
  startApprovalPolling();
  try {
    const result = await api("/api/chat", { method: "POST", body: { message } });
    appendMessage("assistant", result.answer || "(No answer returned.)");
    $("#runLog").textContent = result.logs || "No tool log was returned for this task.";
  } catch (error) {
    appendMessage("assistant", `I could not complete that: ${errorText(error)}`, true);
  } finally {
    state.chatBusy = false;
    $("#sendChatBtn").disabled = false;
    $("#sendChatBtn").textContent = "Send to Milelik";
    $("#chatBusy").textContent = "Press Enter to send · Shift+Enter for a new line";
    input.focus();
  }
}

/* Workspace --------------------------------------------------------------- */

function parentPath(path) {
  const clean = String(path || ".").replace(/\/$/, "");
  const index = clean.lastIndexOf("/");
  return index >= 0 ? clean.slice(0, index) : ".";
}

function renderTreeNodes(nodes, container) {
  for (const node of nodes || []) {
    if (node.type === "folder") {
      const details = document.createElement("details");
      if (node.path === "examples") details.open = true;
      const summary = document.createElement("summary");
      summary.textContent = `📁 ${node.name}`;
      const children = document.createElement("div");
      children.className = "file-children";
      renderTreeNodes(node.children || [], children);
      details.append(summary, children);
      container.append(details);
    } else {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "file-button";
      button.dataset.path = node.path;
      button.textContent = `📄 ${node.name}`;
      if (node.path === $("#currentFilePath").value) button.classList.add("active");
      button.addEventListener("click", () => openWorkspaceFile(node.path));
      container.append(button);
    }
  }
}

function renderWorkspace(data) {
  $("#workspaceRootLabel").textContent = data.root || "";
  const tree = $("#fileTree");
  tree.replaceChildren();
  renderTreeNodes(data.tree || [], tree);
  if (!tree.childNodes.length) tree.textContent = "This workspace is empty.";

  const suggestions = $("#projectSuggestions");
  suggestions.replaceChildren();
  const project = data.project || {};
  if (project.readme) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button secondary suggestion";
    button.textContent = `Open ${project.readme}`;
    button.addEventListener("click", () => openWorkspaceFile(project.readme));
    suggestions.append(button);
  }
  for (const item of project.suggestions || []) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button secondary suggestion";
    const label = document.createElement("span");
    label.textContent = item.label;
    const code = document.createElement("code");
    code.textContent = item.command;
    button.append(label, code);
    button.addEventListener("click", () => {
      $("#terminalCommand").value = item.command;
      $("#terminalCommand").focus();
    });
    suggestions.append(button);
  }
  if (!suggestions.childNodes.length) {
    const span = document.createElement("span");
    span.className = "muted";
    span.textContent = "Clone or create a project to see suggested commands.";
    suggestions.append(span);
  }
  state.loadedWorkspace = true;
}

async function loadWorkspace(openDefault = false) {
  const data = await api("/api/workspace");
  renderWorkspace(data);
  if (openDefault) await openWorkspaceFile($("#currentFilePath").value || "README.md");
  return data;
}

function setEditorDirty(dirty = true) {
  state.editorDirty = dirty;
  $("#editorStatus").textContent = dirty ? "Unsaved changes" : "No unsaved changes";
}

async function openWorkspaceFile(path) {
  if (!path) return;
  if (state.editorDirty && !window.confirm(`Open ${path} and discard unsaved changes?`)) return;
  try {
    const data = await api(`/api/workspace/file?path=${encodeURIComponent(path)}`);
    $("#currentFilePath").value = data.path;
    $("#codeEditor").value = data.content || "";
    const language = data.language || "text";
    if ($(`#languageSelect option[value="${CSS.escape(language)}"]`)) $("#languageSelect").value = language;
    setEditorDirty(false);
    $$(".file-button").forEach((b) => b.classList.toggle("active", b.dataset.path === data.path));
  } catch (error) {
    showError("#workspaceOutput", error);
  }
}

async function saveWorkspaceFile(reloadTree = true) {
  const path = $("#currentFilePath").value.trim();
  if (!path) throw new Error("Enter a file path first.");
  const result = await api("/api/workspace/file", {
    method: "PUT",
    body: { path, content: $("#codeEditor").value },
  });
  $("#currentFilePath").value = result.path;
  setEditorDirty(false);
  if (reloadTree) await loadWorkspace();
  return result;
}

function renderRunResult(selector, result) {
  const parts = [`exit_code: ${result.exit_code ?? "?"} · ${result.duration_ms ?? "?"} ms`];
  if (result.cwd) parts.push(`cwd: ${result.cwd}`);
  if (result.stdout) parts.push(`\nstdout:\n${result.stdout}`);
  if (result.stderr) parts.push(`\nstderr:\n${result.stderr}`);
  if (!result.stdout && !result.stderr) parts.push("\n(no output)");
  setResult(selector, parts.join("\n"));
}

async function saveAndRunFile() {
  try {
    await saveWorkspaceFile(false);
    const language = $("#languageSelect").value;
    let result;
    if (["python", "bash", "javascript"].includes(language)) {
      result = await api("/api/workspace/run-file", {
        method: "POST",
        body: { path: $("#currentFilePath").value, stdin: $("#stdinInput").value, timeout: 60 },
      });
    } else {
      throw new Error("Only Python, Bash and JavaScript files can be run directly. Choose one of those languages or use a workspace command.");
    }
    renderRunResult("#workspaceOutput", result);
    await loadWorkspace();
    toast("File saved and run.");
  } catch (error) {
    showError("#workspaceOutput", error);
  }
}

async function checkCurrentFile() {
  try {
    const language = $("#languageSelect").value;
    if (!["python", "bash", "javascript"].includes(language)) throw new Error("Syntax check supports Python, Bash and JavaScript.");
    const result = await api("/api/code/check", {
      method: "POST", body: { language, code: $("#codeEditor").value },
    });
    setResult("#workspaceOutput", result.ok ? "Syntax OK." : result);
  } catch (error) {
    showError("#workspaceOutput", error);
  }
}

async function deleteCurrentFile() {
  const path = $("#currentFilePath").value.trim();
  if (!path || !window.confirm(`Delete ${path} from the workspace?`)) return;
  try {
    await api("/api/workspace/delete", { method: "POST", body: { path } });
    $("#codeEditor").value = "";
    setEditorDirty(false);
    await loadWorkspace();
    toast("File deleted.");
  } catch (error) {
    showError("#workspaceOutput", error);
  }
}

async function downloadCurrentFile() {
  const path = $("#currentFilePath").value.trim();
  if (!path) return;
  try {
    const blob = await api(`/api/workspace/download?path=${encodeURIComponent(path)}`, { raw: true });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = path.split("/").pop() || "milelik-file";
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    showError("#workspaceOutput", error);
  }
}

async function createWorkspaceItem(folder) {
  const defaultPath = folder ? "new-folder" : "new-file.py";
  const path = window.prompt(folder ? "Folder path inside the workspace:" : "File path inside the workspace:", defaultPath);
  if (!path) return;
  try {
    if (folder) {
      await api("/api/workspace/folder", { method: "POST", body: { path } });
    } else {
      $("#currentFilePath").value = path;
      $("#codeEditor").value = "";
      await saveWorkspaceFile(false);
    }
    await loadWorkspace();
    if (!folder) await openWorkspaceFile(path);
    toast(folder ? "Folder created." : "File created.");
  } catch (error) {
    showError("#workspaceOutput", error);
  }
}

async function submitTerminal(event) {
  event.preventDefault();
  const command = $("#terminalCommand").value.trim();
  if (!command) return;
  const current = $("#currentFilePath").value.trim();
  try {
    setResult("#workspaceOutput", `$ ${command}\nRunning…`);
    const result = await api("/api/code/run", {
      method: "POST",
      body: { language: "bash", code: command, cwd: parentPath(current), timeout: 120 },
    });
    renderRunResult("#workspaceOutput", result);
    await loadWorkspace();
  } catch (error) {
    showError("#workspaceOutput", error);
  }
}

async function submitClone(event) {
  event.preventDefault();
  const url = $("#cloneUrl").value.trim();
  if (!url) return;
  try {
    setResult("#workspaceOutput", `Cloning ${url}…`);
    const result = await api("/api/workspace/clone", {
      method: "POST",
      body: { url, directory: $("#cloneDir").value.trim() },
    });
    if (result.workspace) renderWorkspace(result.workspace);
    else await loadWorkspace();
    renderRunResult("#workspaceOutput", result);
    if (result.cloned) toast(`Cloned into ${result.path}`);
  } catch (error) {
    showError("#workspaceOutput", error);
  }
}

/* APIs -------------------------------------------------------------------- */

function populateUnits() {
  const groups = {
    Temperature: ["C", "F", "K"],
    Length: ["mm", "cm", "m", "km", "in", "ft", "yd", "mi", "nmi"],
    Mass: ["g", "kg", "t", "oz", "lb"],
    Time: ["ms", "s", "min", "h", "day", "week"],
    Data: ["bit", "B", "KB", "MB", "GB", "TB", "KiB", "MiB", "GiB", "TiB"],
    Speed: ["m/s", "km/h", "mph", "knot"],
    Area: ["cm2", "m2", "km2", "ha", "acre", "ft2"],
    Volume: ["ml", "L", "m3", "gallon_us", "cup"],
    Pressure: ["Pa", "kPa", "bar", "atm", "psi"],
    Energy: ["J", "kJ", "cal", "kcal", "kWh"],
  };
  for (const select of [$("#convertFrom"), $("#convertTo")]) {
    select.replaceChildren();
    for (const [label, units] of Object.entries(groups)) {
      const group = document.createElement("optgroup");
      group.label = label;
      for (const unit of units) {
        const option = document.createElement("option");
        option.value = unit;
        option.textContent = unit;
        group.append(option);
      }
      select.append(group);
    }
  }
  $("#convertFrom").value = "F";
  $("#convertTo").value = "C";
}

function renderEndpoints(filter = "") {
  const list = $("#endpointList");
  list.replaceChildren();
  const needle = filter.trim().toLowerCase();
  const visible = state.endpoints.filter((item) =>
    !needle || `${item.method} ${item.path} ${item.category} ${item.description}`.toLowerCase().includes(needle));
  $("#endpointCount").textContent = `${visible.length} shown`;
  for (const item of visible) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "endpoint-item";
    const method = document.createElement("span");
    method.className = `method ${item.method.toLowerCase()}`;
    method.textContent = item.method;
    const path = document.createElement("span");
    path.className = "endpoint-path";
    path.textContent = item.path;
    const desc = document.createElement("div");
    desc.className = "endpoint-desc";
    desc.textContent = `${item.category} — ${item.description}`;
    button.append(method, path, desc);
    button.addEventListener("click", () => {
      $("#apiMethod").value = item.method;
      $("#apiPath").value = item.path;
      $("#apiBody").value = JSON.stringify(item.example || {}, null, 2);
      $("#apiPath").focus();
    });
    list.append(button);
  }
  if (!visible.length) list.textContent = "No endpoints match that search.";
}

async function loadApiDocs() {
  const data = await api("/api/docs");
  state.endpoints = data.endpoints || [];
  renderEndpoints($("#apiSearch").value || "");
  return data;
}

async function runApiForm(resultSelector, path, body) {
  try {
    const result = await api(path, { method: "POST", body });
    setResult(resultSelector, result);
    return result;
  } catch (error) {
    showError(resultSelector, error);
    return null;
  }
}

async function submitGenericApi(event) {
  event.preventDefault();
  const method = $("#apiMethod").value;
  const path = $("#apiPath").value.trim();
  if (!path.startsWith("/api/")) {
    showError("#apiResult", new Error("Use a local /api/… path."));
    return;
  }
  let body;
  if (!["GET", "DELETE"].includes(method)) {
    try { body = JSON.parse($("#apiBody").value || "{}"); }
    catch (error) { showError("#apiResult", new Error(`Invalid JSON body: ${error.message}`)); return; }
  }
  try {
    startApprovalPolling();
    const result = await api(path, { method, body });
    setResult("#apiResult", result);
  } catch (error) {
    showError("#apiResult", error);
  }
}

/* Events/initialisation --------------------------------------------------- */

function bindEvents() {
  $$(".nav-button").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
  $$("[data-goto-view]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.gotoView)));
  $("#refreshAllBtn").addEventListener("click", async () => {
    try {
      await refreshStatus();
      if ($("#view-workspace").classList.contains("active")) await loadWorkspace();
      if ($("#view-apis").classList.contains("active")) await loadApiDocs();
      toast("Status refreshed.");
    } catch (error) { toast(errorText(error), true); }
  });

  $("#chatForm").addEventListener("submit", submitChat);
  $("#chatInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submitChat();
    }
  });
  $$(".quick-prompt").forEach((button) => button.addEventListener("click", () => {
    $("#chatInput").value = button.textContent.trim();
    submitChat();
  }));
  $("#clearChatBtn").addEventListener("click", async () => {
    try {
      await api("/api/chat/reset", { method: "POST", body: {} });
      $("#chatMessages").replaceChildren();
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.innerHTML = "<h4>Chat cleared</h4><p class=\"muted\">Ask Milelik something new.</p>";
      $("#chatMessages").append(empty);
      $("#runLog").textContent = "No task has run yet.";
    } catch (error) { toast(errorText(error), true); }
  });

  $("#approvalTypedYes").addEventListener("input", setApprovalButtonsEnabled);
  $("#approveApprovalBtn").addEventListener("click", () => decideApproval(true));
  $("#denyApprovalBtn").addEventListener("click", () => decideApproval(false));

  $("#settingsForm").addEventListener("submit", saveSettings);
  $("#set-ui-theme").addEventListener("change", (event) => applyTheme(event.target.value));
  $("#saveTokenBtn").addEventListener("click", async () => {
    storeToken($("#tokenInput").value.trim());
    $("#tokenInput").value = "";
    try { await refreshStatus(); toast("Token saved in this browser."); }
    catch (error) { toast(errorText(error), true); }
  });
  $("#clearTokenBtn").addEventListener("click", () => { storeToken(""); toast("Saved token cleared. The browser cookie may still work."); });

  $("#refreshWorkspaceBtn").addEventListener("click", () => loadWorkspace().catch((e) => showError("#workspaceOutput", e)));
  $("#newFileBtn").addEventListener("click", () => createWorkspaceItem(false));
  $("#newFolderBtn").addEventListener("click", () => createWorkspaceItem(true));
  $("#cloneForm").addEventListener("submit", submitClone);
  $("#saveFileBtn").addEventListener("click", () => saveWorkspaceFile().then(() => toast("File saved.")).catch((e) => showError("#workspaceOutput", e)));
  $("#runFileBtn").addEventListener("click", saveAndRunFile);
  $("#checkFileBtn").addEventListener("click", checkCurrentFile);
  $("#deleteFileBtn").addEventListener("click", deleteCurrentFile);
  $("#downloadFileBtn").addEventListener("click", downloadCurrentFile);
  $("#terminalForm").addEventListener("submit", submitTerminal);
  $("#currentFilePath").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); openWorkspaceFile(event.target.value.trim()); }
  });
  $("#codeEditor").addEventListener("input", () => setEditorDirty(true));
  $("#codeEditor").addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      saveWorkspaceFile().then(() => toast("File saved.")).catch((e) => showError("#workspaceOutput", e));
    } else if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      saveAndRunFile();
    } else if (event.key === "Tab") {
      event.preventDefault();
      const editor = event.target;
      editor.setRangeText("    ", editor.selectionStart, editor.selectionEnd, "end");
      setEditorDirty(true);
    }
  });

  $("#apiSearch").addEventListener("input", (event) => renderEndpoints(event.target.value));
  $("#genericApiForm").addEventListener("submit", submitGenericApi);

  $("#mathEvalForm").addEventListener("submit", (event) => {
    event.preventDefault();
    let variables = {};
    try { variables = JSON.parse($("#mathVariables").value || "{}"); }
    catch (error) { showError("#mathResult", new Error(`Variables must be JSON: ${error.message}`)); return; }
    runApiForm("#mathResult", "/api/math/evaluate", { expression: $("#mathExpression").value, variables });
  });
  $("#mathStatsForm").addEventListener("submit", (event) => {
    event.preventDefault();
    runApiForm("#mathResult", "/api/math/statistics", { numbers: $("#mathNumbers").value });
  });
  $("#mathConvertForm").addEventListener("submit", (event) => {
    event.preventDefault();
    runApiForm("#mathResult", "/api/math/convert", {
      value: Number($("#convertValue").value), from: $("#convertFrom").value, to: $("#convertTo").value,
    });
  });
  $("#mathQuadraticForm").addEventListener("submit", (event) => {
    event.preventDefault();
    runApiForm("#mathResult", "/api/math/quadratic", {
      a: Number($("#quadA").value), b: Number($("#quadB").value), c: Number($("#quadC").value),
    });
  });

  $("#snippetForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const result = await runApiForm("#codingResult", "/api/code/run", {
      language: $("#snippetLanguage").value, code: $("#snippetCode").value, timeout: 30,
    });
    if (result) renderRunResult("#codingResult", result);
  });
  $("#checkSnippetBtn").addEventListener("click", () => runApiForm("#codingResult", "/api/code/check", {
    language: $("#snippetLanguage").value, code: $("#snippetCode").value,
  }));
  $("#regexForm").addEventListener("submit", (event) => {
    event.preventDefault();
    runApiForm("#codingResult", "/api/code/regex", {
      pattern: $("#regexPattern").value, flags: $("#regexFlags").value, text: $("#regexText").value,
    });
  });
  $("#encodeForm").addEventListener("submit", (event) => {
    event.preventDefault();
    runApiForm("#codingResult", "/api/code/encode", {
      operation: $("#encodeOperation").value, text: $("#encodeText").value,
    });
  });

  $("#textUtilityForm").addEventListener("submit", (event) => {
    event.preventDefault();
    runApiForm("#utilityResult", "/api/utilities/text", {
      operation: $("#textOperation").value, text: $("#textUtilityInput").value,
    });
  });
  $("#hashForm").addEventListener("submit", (event) => {
    event.preventDefault();
    runApiForm("#utilityResult", "/api/utilities/hash", {
      algorithm: $("#hashAlgorithm").value, text: $("#hashText").value,
    });
  });
  $("#passwordForm").addEventListener("submit", (event) => {
    event.preventDefault();
    runApiForm("#utilityResult", "/api/utilities/password", {
      length: Number($("#passwordLength").value), symbols: $("#passwordSymbols").checked,
    });
  });
  $("#timeBtn").addEventListener("click", async () => {
    try { setResult("#utilityResult", await api("/api/utilities/time")); }
    catch (error) { showError("#utilityResult", error); }
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  populateUnits();
  bindEvents();
  applyTheme("system");
  startApprovalPolling();

  let initialView = "chat";
  try {
    const status = await refreshStatus();
    initialView = status.settings?.ui_default_view || initialView;
    await loadChatHistory();
    await loadWorkspace(true);
    await loadApiDocs();
  } catch {
    $("#connectionBanner").hidden = false;
  }

  try {
    const last = localStorage.getItem("milelikCurrentView");
    if (last && VIEW_TITLES[last]) initialView = last;
  } catch { /* ignore */ }
  showView(initialView);
});
