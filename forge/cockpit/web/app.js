/* Forge Cockpit - vanilla JS, no build step, no secrets.
 * Talks only to same-origin /api/v1. The backend authorizes everything;
 * disabled buttons are convenience, never security.
 */
"use strict";

const state = {
  session: null,
  route: "dashboard",
  taskId: null,
  cursor: 0,
  seenSeq: new Set(),
  events: [],
  eventSource: null,
  pollers: [],
  connected: true,
};

const STAGES = ["planning", "coding", "testing", "debugging", "review",
  "security", "benchmark", "acceptance", "checkpoint", "commit", "completed"];

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function esc(value) {
  return String(value === null || value === undefined ? "" : value);
}

function setConnected(ok) {
  state.connected = ok;
  const badge = document.getElementById("conn");
  badge.textContent = ok ? "connected" : "connection lost \u2014 reconnecting\u2026";
  badge.className = ok ? "conn ok" : "conn down";
}

async function api(path, options) {
  const opts = Object.assign({ headers: {} }, options || {});
  opts.headers["Accept"] = "application/json";
  const method = (opts.method || "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD") {
    opts.headers["X-Requested-With"] = "forge-cockpit";
    if (opts.body && typeof opts.body === "object") {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    setConnected(false);
    throw new Error("Network error: backend unreachable.");
  }
  setConnected(true);
  let data = null;
  try {
    data = await res.json();
  } catch (err) {
    data = null;
  }
  if (!res.ok) {
    const err = (data && data.error) || {};
    const failure = new Error(err.message || ("Request failed: " + res.status));
    failure.code = err.code || "INTERNAL_ERROR";
    failure.status = res.status;
    failure.details = err.details || {};
    throw failure;
  }
  return data;
}

function stopPollers() {
  for (const id of state.pollers) clearInterval(id);
  state.pollers = [];
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
}

function show(view) {
  stopPollers();
  state.route = view;
  document.querySelectorAll("#nav a").forEach((a) => {
    a.classList.toggle("active", a.dataset.route === view ||
      (view === "task" && a.dataset.route === "tasks"));
  });
  const host = document.getElementById("view");
  host.innerHTML = "";
  const tpl = document.getElementById("tpl-" + view);
  host.appendChild(tpl.content.cloneNode(true));
  ({ dashboard: renderDashboard, tasks: renderTasks, task: renderTask,
    projects: renderProjects, models: renderModels,
    permissions: renderPermissions, git: renderGit })[view]();
}

function route() {
  const hash = window.location.hash || "#/dashboard";
  const parts = hash.replace(/^#\//, "").split("/");
  if (parts[0] === "tasks" && parts[1]) {
    state.taskId = parts[1];
    show("task");
  } else if (["dashboard", "tasks", "projects", "models", "permissions", "git"].includes(parts[0])) {
    show(parts[0]);
  } else {
    show("dashboard");
  }
}

/* ---------- dashboard ---------- */

async function renderDashboard() {
  const load = async () => {
    let data;
    try {
      data = await api("/api/v1/dashboard");
    } catch (err) {
      document.getElementById("d-status").textContent = err.message;
      return;
    }
    const kv = (obj) => {
      const table = el("table", "kv");
      for (const [k, v] of Object.entries(obj)) {
        const tr = el("tr");
        tr.appendChild(el("td", null, k));
        tr.appendChild(el("td", null, esc(v)));
        table.appendChild(tr);
      }
      return table;
    };
    const status = document.getElementById("d-status");
    status.innerHTML = "";
    status.appendChild(kv(Object.assign({ project: data.project_id }, data.tasks,
      { approvals_waiting: data.approvals_waiting })));
    const models = document.getElementById("d-models");
    models.innerHTML = "";
    models.appendChild(kv(data.models));
    const workers = document.getElementById("d-workers");
    workers.innerHTML = "";
    workers.appendChild(kv(data.workers));
    const feed = document.getElementById("d-activity");
    feed.innerHTML = "";
    for (const item of data.recent_activity || []) {
      feed.appendChild(el("div", "feeditem",
        `#${item.seq} ${item.type} (${item.task_id})`));
    }
  };
  await load();
  state.pollers.push(setInterval(load, 5000));
}

/* ---------- tasks ---------- */

function badge(text, kind) {
  return el("span", "badge " + (kind || ""), text);
}

function statusBadge(status) {
  const kind = { SUCCEEDED: "ok", FAILED: "bad", CANCELLED: "warn",
    ROLLED_BACK: "warn", RUNNING: "", WAITING_APPROVAL: "warn",
    PAUSED: "warn", QUEUED: "" }[status] || "";
  return badge(status, kind);
}

async function renderTasks() {
  const form = document.getElementById("new-task-form");
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const input = document.getElementById("new-task-req");
    const errBox = document.getElementById("new-task-error");
    errBox.textContent = "";
    try {
      const data = await api("/api/v1/tasks",
        { method: "POST", body: { requirement: input.value } });
      input.value = "";
      window.location.hash = "#/tasks/" + data.task.task_id;
    } catch (err) {
      errBox.textContent = err.message;
    }
  });
  const load = async () => {
    let data;
    try {
      data = await api("/api/v1/tasks?limit=50");
    } catch (err) {
      document.getElementById("task-list").textContent = err.message;
      return;
    }
    const list = document.getElementById("task-list");
    list.innerHTML = "";
    if (!data.tasks.length) {
      list.appendChild(el("div", "muted", "No tasks yet."));
      return;
    }
    for (const task of data.tasks) {
      const item = el("div", "listitem");
      const link = el("a", null, task.requirement.slice(0, 80));
      link.href = "#/tasks/" + task.task_id;
      item.appendChild(link);
      item.appendChild(statusBadge(task.status));
      item.appendChild(el("span", "muted",
        `${esc(task.stage || "")} \u00b7 ${esc(task.mode)} \u00b7 v${task.version}`));
      list.appendChild(item);
    }
  };
  await load();
  state.pollers.push(setInterval(load, 4000));
}

/* ---------- task detail ---------- */

async function renderTask() {
  const taskId = state.taskId;
  state.cursor = 0;
  state.seenSeq = new Set();
  state.events = [];

  const fail = (err) => {
    document.getElementById("t-error").textContent = err.message;
  };

  const loadTask = async () => {
    let data;
    try {
      data = await api("/api/v1/tasks/" + encodeURIComponent(taskId));
    } catch (err) {
      fail(err);
      return null;
    }
    const task = data.task;
    document.getElementById("t-title").textContent =
      "Task " + task.task_id;
    const meta = document.getElementById("t-meta");
    meta.innerHTML = "";
    meta.appendChild(el("div", null, task.requirement));
    meta.appendChild(el("div", null,
      `status ${task.status} \u00b7 stage ${task.stage || "-"} \u00b7 mode ${task.mode} ` +
      `\u00b7 v${task.version} \u00b7 model ${task.model || "-"} (${task.provider || "-"})`));
    renderActions(task);
    renderTimeline(task);
    renderDetails(task);
    if (["SUCCEEDED", "FAILED", "CANCELLED", "ROLLED_BACK"].includes(task.status)) {
      loadVerification(taskId);
      loadReport(taskId);
    }
    loadCheckpoints(task);
    return task;
  };

  const task = await loadTask();
  if (!task) return;
  await loadHistory(taskId);
  openStream(taskId);
  state.pollers.push(setInterval(async () => {
    const current = await loadTask();
    if (current && ["SUCCEEDED", "FAILED", "CANCELLED", "ROLLED_BACK"].includes(current.status)) {
      // Terminal: stop polling task state, keep the stream briefly.
      stopPollers();
      openStream(taskId);
      setTimeout(() => { if (state.eventSource) state.eventSource.close(); }, 5000);
    }
  }, 3000));
}

function renderActions(task) {
  const box = document.getElementById("t-actions");
  box.innerHTML = "";
  const terminal = ["SUCCEEDED", "FAILED", "CANCELLED", "ROLLED_BACK"].includes(task.status);
  const mk = (label, fn, cls) => {
    const btn = el("button", "btn small " + (cls || ""), label);
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      document.getElementById("t-error").textContent = "";
      try {
        await fn();
      } catch (err) {
        document.getElementById("t-error").textContent = err.message;
      } finally {
        btn.disabled = false;
      }
    });
    box.appendChild(btn);
  };
  if (!terminal) {
    if (task.status !== "PAUSED") {
      mk("Pause", () => api(`/api/v1/tasks/${task.task_id}/pause`,
        { method: "POST", body: { expected_version: task.version } }));
    } else {
      mk("Resume", () => api(`/api/v1/tasks/${task.task_id}/resume`,
        { method: "POST", body: { expected_version: task.version } }));
    }
    mk("Cancel", () => api(`/api/v1/tasks/${task.task_id}/cancel`,
      { method: "POST", body: { expected_version: task.version } }), "danger");
  } else {
    mk("Retry", async () => {
      const data = await api(`/api/v1/tasks/${task.task_id}/retry`, { method: "POST", body: {} });
      window.location.hash = "#/tasks/" + data.task.task_id;
    });
    if (task.status === "SUCCEEDED") {
      mk("Rollback", async () => {
        try {
          await api(`/api/v1/tasks/${task.task_id}/rollback`,
            { method: "POST", body: { expected_version: task.version } });
        } catch (err) {
          if (err.code === "APPROVAL_REQUIRED") {
            document.getElementById("t-error").textContent =
              "Rollback needs approval: see Permissions (request " +
              (err.details.approval_id || "?") + ").";
          } else {
            throw err;
          }
        }
      }, "danger");
    }
  }
}

function renderTimeline(task) {
  const ol = document.getElementById("t-timeline");
  ol.innerHTML = "";
  const seen = new Set(state.events.filter((e) => e.type === "stage.started")
    .map((e) => (e.data && e.data.stage) || ""));
  const failed = task.status === "FAILED" || task.status === "CANCELLED";
  for (const stage of STAGES) {
    const li = el("li", null, (seen.has(stage) ? "\u2713 " : "\u25cb ") + stage);
    if (seen.has(stage)) li.classList.add("done");
    if (task.stage === stage && !["SUCCEEDED", "FAILED", "CANCELLED", "ROLLED_BACK"].includes(task.status)) {
      li.classList.add("active");
    }
    if (failed && task.stage === stage) li.classList.add("failed");
    li.title = stage;
    ol.appendChild(li);
  }
}

function renderDetails(task) {
  const box = document.getElementById("t-details");
  box.innerHTML = "";
  const table = el("table", "kv");
  const rows = [
    ["actor", task.actor], ["attempts", task.attempts],
    ["files", (task.files || []).join(", ") || "-"],
    ["checkpoint", task.checkpoint_id || "-"],
    ["error", task.error || "-"],
    ["rollback", String(task.rollback)],
  ];
  for (const [k, v] of rows) {
    const tr = el("tr");
    tr.appendChild(el("td", null, k));
    tr.appendChild(el("td", null, esc(v)));
    table.appendChild(tr);
  }
  box.appendChild(table);
}

async function loadVerification(taskId) {
  try {
    const data = await api(`/api/v1/tasks/${encodeURIComponent(taskId)}/verification`);
    const box = document.getElementById("t-verification");
    if (!box) return;
    box.innerHTML = "";
    const table = el("table", "kv");
    for (const [k, v] of Object.entries(data)) {
      if (k === "task_id" || k === "status") continue;
      const tr = el("tr");
      tr.appendChild(el("td", null, k));
      tr.appendChild(el("td", null, JSON.stringify(v)));
      table.appendChild(tr);
    }
    box.appendChild(table);
  } catch (err) { /* keep quiet; task may still run */ }
}

async function loadReport(taskId) {
  try {
    const data = await api(`/api/v1/tasks/${encodeURIComponent(taskId)}/report`);
    const box = document.getElementById("t-report");
    if (!box) return;
    box.innerHTML = "";
    const report = data.report || {};
    const table = el("table", "kv");
    for (const key of ["final_status", "stages", "model", "provider",
        "files_changed", "tests_run", "retries", "duration_seconds",
        "acceptance", "error"]) {
      if (report[key] === undefined) continue;
      const tr = el("tr");
      tr.appendChild(el("td", null, key));
      tr.appendChild(el("td", null,
        typeof report[key] === "object" ? JSON.stringify(report[key]) : esc(report[key])));
      table.appendChild(tr);
    }
    box.appendChild(table);
  } catch (err) { /* keep quiet */ }
}

async function loadCheckpoints(task) {
  try {
    const data = await api(`/api/v1/tasks/${encodeURIComponent(task.task_id)}/checkpoints`);
    const box = document.getElementById("t-checkpoints");
    if (!box) return;
    box.innerHTML = "";
    if (!data.checkpoints.length) {
      box.appendChild(el("div", "muted", "No checkpoints recorded."));
      return;
    }
    for (const cp of data.checkpoints) {
      box.appendChild(el("div", "feeditem",
        `${cp.checkpoint_id} \u00b7 ${cp.status} \u00b7 ${cp.affected_paths.length} paths`));
    }
  } catch (err) { /* keep quiet */ }
}

async function loadHistory(taskId) {
  try {
    const data = await api(`/api/v1/tasks/${encodeURIComponent(taskId)}/events?after=0&limit=500`);
    for (const item of data.events) pushEvent(item);
    state.cursor = data.latest || 0;
  } catch (err) {
    document.getElementById("t-error").textContent = err.message;
  }
}

function pushEvent(item) {
  if (!item || state.seenSeq.has(item.seq)) return;
  state.seenSeq.add(item.seq);
  state.events.push(item);
  if (item.seq > state.cursor) state.cursor = item.seq;
  const feed = document.getElementById("t-events");
  if (feed) {
    feed.appendChild(el("div", "feeditem",
      `#${item.seq} ${item.type} ${JSON.stringify(item.data || {})}`));
    feed.scrollTop = feed.scrollHeight;
  }
  if (item.type === "stage.started") {
    const title = document.getElementById("t-title");
    if (title) renderTimeline({ stage: (item.data && item.data.stage) || "", status: "RUNNING" });
  }
}

function openStream(taskId) {
  const label = document.getElementById("t-stream-state");
  const url = `/api/v1/tasks/${encodeURIComponent(taskId)}/events/stream?after=${state.cursor}`;
  const source = new EventSource(url);
  state.eventSource = source;
  source.onopen = () => { if (label) label.textContent = "(live)"; };
  source.onerror = () => { if (label) label.textContent = "(reconnecting\u2026)"; };
  source.onmessage = (msg) => {
    try {
      pushEvent(JSON.parse(msg.data));
    } catch (err) { /* ignore malformed frames */ }
  };
  source.addEventListener("end", () => source.close());
}

/* ---------- projects ---------- */

async function renderProjects() {
  const list = document.getElementById("p-list");
  try {
    const data = await api("/api/v1/projects");
    list.innerHTML = "";
    for (const project of data.projects) {
      const item = el("div", "listitem");
      item.appendChild(el("span", null, `${project.name} (${project.project_id})`));
      if (state.session && project.project_id === state.session.project_id) {
        item.appendChild(badge("current", "ok"));
      }
      list.appendChild(item);
    }
  } catch (err) {
    list.textContent = err.message;
  }
  const current = document.getElementById("p-current");
  try {
    const data = await api(`/api/v1/projects/${encodeURIComponent(state.session.project_id)}`);
    current.innerHTML = "";
    const table = el("table", "kv");
    for (const [k, v] of [["name", data.name], ["root", data.root],
        ["counts", JSON.stringify(data.counts)],
        ["git", JSON.stringify(data.git)]]) {
      const tr = el("tr");
      tr.appendChild(el("td", null, k));
      tr.appendChild(el("td", null, esc(v)));
      table.appendChild(tr);
    }
    current.appendChild(table);
  } catch (err) {
    current.textContent = err.message;
  }
}

/* ---------- models ---------- */

async function renderModels() {
  try {
    const data = await api("/api/v1/models");
    const box = document.getElementById("m-list");
    box.innerHTML = "";
    const table = el("table", "kv");
    const head = el("tr");
    for (const h of ["model", "provider", "capabilities", "context",
        "health", "reliability", "latency_ms", "free", "local"]) {
      head.appendChild(el("th", null, h));
    }
    table.appendChild(head);
    for (const model of data.models || []) {
      const tr = el("tr");
      for (const key of ["name", "provider"]) tr.appendChild(el("td", null, esc(model[key])));
      tr.appendChild(el("td", null, (model.capabilities || []).join(", ")));
      tr.appendChild(el("td", null, esc(model.context_window)));
      tr.appendChild(el("td", null, esc((model.health || {}).status)));
      tr.appendChild(el("td", null, esc(model.reliability)));
      tr.appendChild(el("td", null, esc(model.latency_ms)));
      tr.appendChild(el("td", null, esc(model.free)));
      tr.appendChild(el("td", null, esc(model.local)));
      table.appendChild(tr);
    }
    box.appendChild(table);
    const routing = document.getElementById("m-routing");
    routing.innerHTML = "";
    for (const entry of data.recent_routing || []) {
      routing.appendChild(el("div", "feeditem", JSON.stringify(entry)));
    }
  } catch (err) {
    document.getElementById("m-list").textContent = err.message;
  }
  try {
    const data = await api("/api/v1/providers");
    const box = document.getElementById("m-providers");
    box.innerHTML = "";
    const table = el("table", "kv");
    for (const provider of data.providers || []) {
      const tr = el("tr");
      tr.appendChild(el("td", null, esc(provider.name)));
      tr.appendChild(el("td", null, JSON.stringify(provider)));
      table.appendChild(tr);
    }
    box.appendChild(table);
  } catch (err) {
    document.getElementById("m-providers").textContent = err.message;
  }
}

/* ---------- permissions ---------- */

async function renderPermissions() {
  const load = async () => {
    let data;
    try {
      data = await api("/api/v1/permissions");
    } catch (err) {
      document.getElementById("pm-effective").textContent = err.message;
      return;
    }
    const eff = document.getElementById("pm-effective");
    eff.innerHTML = "";
    const table = el("table", "kv");
    const headRow = el("tr");
    headRow.appendChild(el("td", null, "profile"));
    headRow.appendChild(el("td", null, esc(data.profile)));
    table.appendChild(headRow);
    for (const [op, level] of Object.entries(data.effective || {})) {
      const tr = el("tr");
      tr.appendChild(el("td", null, op));
      tr.appendChild(el("td", null, esc(level)));
      table.appendChild(tr);
    }
    eff.appendChild(table);

    const box = document.getElementById("pm-approvals");
    box.innerHTML = "";
    if (!data.pending_approvals.length) {
      box.appendChild(el("div", "muted", "No pending approvals."));
    }
    for (const approval of data.pending_approvals || []) {
      const card = el("div", "approval");
      const fields = [
        ["WHAT", `${approval.operation} (${approval.resource})`],
        ["WHY", approval.reason || "-"],
        ["TASK", approval.task_ref || approval.task_id],
        ["AGENT", approval.agent],
        ["PATHS", (approval.files || []).join(", ") || "-"],
        ["OPERATION", approval.operation],
        ["RISK", approval.risk],
        ["SCOPE", (approval.scopes || []).join(", ") || "-"],
        ["EXPIRATION", approval.expires_at ?
          new Date(approval.expires_at * 1000).toISOString() : "-"],
        ["MODEL", approval.model ? `${approval.model} (${approval.provider || "?"})` : "-"],
      ];
      for (const [k, v] of fields) {
        const row = el("div", null, "");
        row.appendChild(el("strong", null, k + ": "));
        row.appendChild(el("span", null, esc(v)));
        card.appendChild(row);
      }
      const row = el("div", "btnrow");
      const allow = el("button", "btn primary small", "ALLOW");
      const deny = el("button", "btn danger small", "DENY");
      allow.addEventListener("click", () => decide(approval.id, true));
      deny.addEventListener("click", () => decide(approval.id, false));
      row.appendChild(allow);
      row.appendChild(deny);
      card.appendChild(row);
      box.appendChild(card);
    }

    const rules = document.getElementById("pm-rules");
    rules.innerHTML = "";
    for (const rule of data.policy_rules || []) {
      rules.appendChild(el("div", "feeditem", JSON.stringify(rule)));
    }
    if (!data.policy_rules || !data.policy_rules.length) {
      rules.appendChild(el("div", "muted", "No fine-grained policy rules attached."));
    }
  };
  const decide = async (id, allow) => {
    try {
      await api(`/api/v1/approvals/${encodeURIComponent(id)}/${allow ? "approve" : "deny"}`,
        { method: "POST", body: {} });
    } catch (err) {
      document.getElementById("pm-effective").textContent = err.message;
    }
    load();
  };
  await load();
  state.pollers.push(setInterval(load, 4000));
}

/* ---------- git ---------- */

async function renderGit() {
  const projectId = encodeURIComponent(state.session.project_id);
  try {
    const data = await api(`/api/v1/projects/${projectId}/git`);
    const box = document.getElementById("g-state");
    box.innerHTML = "";
    const table = el("table", "kv");
    for (const [k, v] of Object.entries(data)) {
      const tr = el("tr");
      tr.appendChild(el("td", null, k));
      tr.appendChild(el("td", null,
        typeof v === "object" ? JSON.stringify(v) : esc(v)));
      table.appendChild(tr);
    }
    box.appendChild(table);
  } catch (err) {
    document.getElementById("g-state").textContent = err.message;
  }
  for (const [id, staged] of [["g-diff", false], ["g-diff-staged", true]]) {
    try {
      const data = await api(
        `/api/v1/projects/${projectId}/git/diff?staged=${staged}`);
      document.getElementById(id).textContent =
        data.diff + (data.truncated ? "\n\u2026(truncated)" : "");
    } catch (err) {
      document.getElementById(id).textContent = err.message;
    }
  }
}

/* ---------- auth bootstrap ---------- */

async function bootstrap() {
  try {
    const data = await api("/api/v1/sessions/me");
    state.session = data.session;
    enter();
  } catch (err) {
    document.getElementById("login").classList.remove("hidden");
  }
  document.getElementById("login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const errBox = document.getElementById("login-error");
    errBox.textContent = "";
    const body = {
      actor: document.getElementById("login-actor").value,
      project_id: document.getElementById("login-project").value,
      profile: document.getElementById("login-profile").value,
    };
    try {
      const data = await api("/api/v1/sessions",
        { method: "POST", body });
      state.session = data.session;
      // The session token is kept in the HttpOnly cookie only; the body
      // copy is for non-browser clients and is deliberately dropped here.
      enter();
    } catch (err) {
      errBox.textContent = err.message;
    }
  });
}

function enter() {
  document.getElementById("login").classList.add("hidden");
  document.getElementById("nav").classList.remove("hidden");
  document.getElementById("logout").classList.remove("hidden");
  document.getElementById("whoami").textContent =
    `${state.session.actor} @ ${state.session.project_id} (${state.session.profile})`;
  document.getElementById("logout").addEventListener("click", async () => {
    try {
      await api("/api/v1/sessions/me", { method: "DELETE" });
    } catch (err) { /* ignore */ }
    window.location.hash = "#/dashboard";
    window.location.reload();
  });
  window.addEventListener("hashchange", route);
  route();
}

document.addEventListener("DOMContentLoaded", bootstrap);
