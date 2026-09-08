/* Forge Cockpit - vanilla JS, no build step, no stored credentials.
 * Talks only to same-origin /api/v1. The backend authorizes everything;
 * disabled buttons are convenience, never security.
 */
"use strict";

const state = {
  session: null,
  route: "dashboard",
  epoch: 0,
  taskId: null,
  deskToken: null,
  cursor: 0,
  histMax: 0,
  seenSeq: new Set(),
  events: [],
  eventSource: null,
  pollers: [],
  connected: true,
  entered: false,
  cache: { tasks: [], task: null, filter: "ALL", search: "" },
  palette: { open: false, selected: 0, items: [] },
};

const STAGES = ["planning", "coding", "testing", "debugging", "review",
  "security", "benchmark", "acceptance", "checkpoint", "commit", "completed"];

const TERMINAL = ["SUCCEEDED", "FAILED", "CANCELLED", "ROLLED_BACK"];

const ROUTES = {
  dashboard: { render: renderDashboard, title: "Overview" },
  tasks: { render: renderTasks, title: "Tasks" },
  task: { render: renderTask, title: "Task" },
  projects: { render: renderProjects, title: "Projects" },
  models: { render: renderModels, title: "Models" },
  permissions: { render: renderPermissions, title: "Permissions" },
  git: { render: renderGit, title: "Git" },
  activity: { render: renderActivity, title: "Activity" },
  approvals: { render: renderApprovals, title: "Approvals" },
  desktop: { render: renderDesktop, title: "Desktop" },
  system: { render: renderSystem, title: "System" },
};

const PROFILE_BLURBS = {
  safe: ["SAFE MODE", "Forge can read and analyze. Every modification is blocked."],
  assisted: ["ASSISTED MODE", "Forge reads automatically. Changes require your approval."],
  autonomous: ["AUTONOMOUS MODE", "Low-risk writes are automatic. Sensitive operations still require approval."],
  locked: ["LOCKED MODE", "No modifications at all. Observation only."],
};

const OP_LABELS = {
  read_file: "Read files", search_files: "Search", write_file: "Write files",
  delete_file: "Delete files", run_command: "Run commands", run_tests: "Run tests",
  git_status: "Git status", git_diff: "Git diff", git_commit: "Git commit",
  git_push: "Git push",
};

const SECURITY_POSTURE = [
  ["Browser trust", "Never trusted", "on"],
  ["Policy enforcement", "Active", "on"],
  ["Approval system", "Active", "on"],
  ["Redaction of sensitive values", "Active", "on"],
  ["Project isolation", "Active", "on"],
  ["Audit logging", "Active", "on"],
];

/* ---------- tiny DOM + format helpers ---------- */

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function esc(value) {
  return String(value === null || value === undefined ? "" : value);
}

function fmtRel(ts) {
  if (!ts) return "–";
  const diff = Date.now() / 1000 - Number(ts);
  if (!(diff >= 0)) return "just now";
  if (diff < 60) return "just now";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  return Math.floor(diff / 86400) + "d ago";
}

function fmtAbs(ts) {
  if (!ts) return "–";
  try {
    return new Date(Number(ts) * 1000).toLocaleString();
  } catch (err) {
    return String(ts);
  }
}

function fmtDur(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const pad = (n) => String(n).padStart(2, "0");
  return pad(Math.floor(total / 60)) + ":" + pad(total % 60);
}

function greet() {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

function shortId(id) {
  const text = esc(id);
  return text.length > 14 ? text.slice(0, 14) + "…" : text;
}

/* ---------- connection + API ---------- */

function setConnected(ok) {
  state.connected = ok;
  const badge = document.getElementById("conn");
  badge.textContent = ok ? "Connected" : "Reconnecting…";
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

/* ---------- routing ---------- */

function snapEpoch() {
  return state.epoch;
}

function stale(snap) {
  return snap !== state.epoch;
}

function show(view) {
  stopPollers();
  state.route = view;
  state.epoch += 1;
  document.querySelectorAll("#nav a").forEach((a) => {
    const active = a.dataset.route === view ||
      (view === "task" && a.dataset.route === "tasks");
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  document.body.classList.remove("nav-open");
  document.getElementById("scrim").classList.add("hidden");
  const host = document.getElementById("view");
  host.innerHTML = "";
  const tpl = document.getElementById("tpl-" + view);
  host.appendChild(tpl.content.cloneNode(true));
  document.title = ROUTES[view].title + " — Forge";
  ROUTES[view].render();
  host.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

function route() {
  const hash = window.location.hash || "#/dashboard";
  const parts = hash.replace(/^#\//, "").split("/");
  if (parts[0] === "tasks" && parts[1]) {
    state.taskId = parts[1];
    show("task");
  } else if (ROUTES[parts[0]] && parts[0] !== "task") {
    show(parts[0]);
  } else {
    show("dashboard");
  }
}

/* ---------- shared widgets ---------- */

function statusTone(status) {
  return {
    SUCCEEDED: "ok", FAILED: "bad", CANCELLED: "warn", ROLLED_BACK: "warn",
    RUNNING: "accent", WAITING_APPROVAL: "warn", PAUSED: "warn", QUEUED: "info",
  }[status] || "";
}

function statusBadge(status) {
  const tone = statusTone(status);
  const badge = el("span", "status-badge " + tone);
  badge.appendChild(el("span", "status-dot " + (tone || "") +
    (status === "RUNNING" ? " pulse" : "")));
  badge.appendChild(el("span", null, String(status).replace(/_/g, " ")));
  return badge;
}

function statusDot(status, pulse) {
  const tone = statusTone(status);
  return el("span", "status-dot " + tone + (pulse ? " pulse" : ""));
}

function errorState(box, title, err, retry) {
  box.innerHTML = "";
  const wrap = el("div", "inline-error");
  wrap.appendChild(el("h4", null, title));
  const detail = el("p", "reason", err && err.message ? err.message : String(err));
  wrap.appendChild(detail);
  if (retry) {
    const btn = el("button", "btn small", "Retry");
    btn.addEventListener("click", retry);
    wrap.appendChild(btn);
  }
  box.appendChild(wrap);
}

function emptyState(box, title, body, actionLabel, actionHref) {
  box.innerHTML = "";
  const wrap = el("div", "empty-state");
  wrap.appendChild(el("h4", null, title));
  if (body) wrap.appendChild(el("p", null, body));
  if (actionLabel && actionHref) {
    const link = el("a", "btn primary small", actionLabel);
    link.href = actionHref;
    wrap.appendChild(link);
  }
  box.appendChild(wrap);
}

function setApprBadge(count) {
  const badge = document.getElementById("nav-appr-count");
  if (!badge) return;
  if (count > 0) {
    badge.textContent = String(count);
    badge.classList.remove("hidden");
  } else {
    badge.classList.add("hidden");
  }
}

function kvTable(rows) {
  const table = el("table", "data-table");
  for (const [k, v, mono] of rows) {
    const tr = el("tr");
    tr.appendChild(el("td", null, k));
    tr.appendChild(el("td", mono ? "mono" : null, esc(v)));
    table.appendChild(tr);
  }
  return table;
}

function renderSecurityPosture(box, compact) {
  box.innerHTML = "";
  const grid = el("div", "sec-grid");
  for (const [name, label, _on] of SECURITY_POSTURE) {
    const item = el("div", "sec-item");
    item.appendChild(el("span", "status-dot ok"));
    item.appendChild(el("span", "sec-name", name));
    const st = el("span", "sec-state on", label.toUpperCase());
    item.appendChild(st);
    grid.appendChild(item);
  }
  box.appendChild(grid);
  if (!compact) {
    box.appendChild(el("p", "muted", "Guaranteed by the server architecture — this page only describes it."));
  }
}

/* ---------- human-readable events ---------- */

function prettyType(type) {
  return String(type).replace(/[._]/g, " ");
}

function fileCount(data) {
  const files = (data && (data.files || data.paths)) || [];
  return Array.isArray(files) ? files.length : 0;
}

const EVENT_TEXT = {
  "task.created": (d) => ({ title: "Task submitted", lines: [`${d.requirement_chars || "?"} chars · mode ${d.mode || "?"} · by ${d.actor || "?"}`], tone: "info" }),
  "task.queued": () => ({ title: "Task queued", lines: ["Waiting for a worker"], tone: "info" }),
  "task.started": (d) => ({ title: "Run started", lines: [`mode ${d.mode || "?"}`], tone: "accent" }),
  "task.paused": () => ({ title: "Run paused", lines: ["Paused at a stage boundary"], tone: "warn" }),
  "task.resumed": () => ({ title: "Run resumed", lines: ["Continuing the pipeline"], tone: "info" }),
  "task.cancel_requested": () => ({ title: "Cancel requested", lines: ["Finishing the current step, then rolling back"], tone: "warn" }),
  "task.cancelled": (d) => ({ title: "Run cancelled", lines: [d.rollback ? "Candidate changes were rolled back" : "Stopped"], tone: "warn" }),
  "task.completed": (d) => ({ title: "Run completed", lines: [`${fileCount(d)} file(s)${d.model ? ` · ${d.model}` : ""}${d.provider ? ` (${d.provider})` : ""}`], tone: "ok" }),
  "task.failed": (d) => ({ title: "Run failed", lines: [d.error ? String(d.error).slice(0, 220) : "See report", d.rollback ? "Rolled back exactly" : ""].filter(Boolean), tone: "bad" }),
  "stage.started": (d) => ({ title: `Stage: ${d.stage || "?"}`, lines: d.raw ? [`gate ${d.raw}`] : [], tone: "accent" }),
  "run.started": () => ({ title: "Supervisor engaged", lines: ["Autonomous loop started"], tone: "accent" }),
  "agent.selected": (d) => ({ title: "Agents selected", lines: [d.agents ? String(d.agents) : "Planner chose the crew"], tone: "" }),
  "model.selected": (d) => ({ title: "Model selected", lines: [`${d.model || "?"}${d.provider ? ` · ${d.provider} provider` : ""}`], tone: "info" }),
  "change.proposed": (d) => ({ title: "Changes proposed", lines: [`${fileCount(d)} file(s)`], tone: "" }),
  "permission.checked": (d) => {
    const v = String(d.decision || d.verdict || d.level || "").toLowerCase();
    const tone = v.includes("deny") || v.includes("block") ? "bad" : (v.includes("approv") ? "warn" : "ok");
    return { title: "Permission checked", lines: [`${d.operation || d.tool || "operation"} · ${v || "evaluated"}`], tone };
  },
  "changes.applied": (d) => ({ title: "Changes applied", lines: [`${fileCount(d)} file(s) written through the controlled layer`], tone: "" }),
  "tests.executed": (d) => {
    const ok = d.passed === true || d.passed === 1;
    return { title: ok ? "Tests passed" : "Tests executed", lines: [`attempt ${d.attempt || d.runs || 1} · exit ${d.exit_code !== undefined ? d.exit_code : "?"}${d.scope ? ` · ${d.scope}` : ""}`], tone: ok ? "ok" : "" };
  },
  "tests.failed": (d) => ({ title: "Tests failed", lines: [`attempt ${d.attempt || "?"} · exit ${d.exit_code !== undefined ? d.exit_code : "?"}${d.reason ? ` · ${d.reason}` : ""}`], tone: "bad" }),
  "repair.attempted": (d) => ({ title: "Repair attempted", lines: [`${fileCount(d)} file(s)${d.model ? ` · ${d.model}` : ""}`], tone: "warn" }),
  "review.completed": (d) => ({ title: "Review completed", lines: [`verdict ${d.verdict || "?"} · ${d.findings || 0} finding(s)`], tone: String(d.verdict || "").includes("APPROVE") ? "ok" : "warn" }),
  "security.completed": (d) => ({ title: "Security verification", lines: [d.passed ? `Passed · ${d.findings || 0} finding(s)` : `Blocked · ${d.findings || 0} finding(s)`], tone: d.passed ? "ok" : "bad" }),
  "benchmark.completed": (d) => ({ title: "Benchmark completed", lines: [`${d.passed || 0}/${d.total || 0} benchmarks passed`], tone: "" }),
  "acceptance.completed": (d) => {
    const gates = Array.isArray(d.failed_gates) ? d.failed_gates : [];
    return { title: d.accepted ? "Acceptance granted" : "Acceptance refused", lines: d.accepted ? ["All mandatory gates held"] : [`Failed: ${gates.join(", ") || "?"}`], tone: d.accepted ? "ok" : "bad" };
  },
  "git.commit": (d) => ({ title: "Commit created", lines: [d.message ? String(d.message).slice(0, 160) : (d.sha ? `commit ${d.sha}` : "Accepted files committed")], tone: "ok" }),
  "checkpoint.created": (d) => ({ title: "Checkpoint recorded", lines: [d.checkpoint_id ? `id ${d.checkpoint_id}` : "Pre-change snapshot stored"], tone: "" }),
  "rollback.requested": (d) => ({ title: "Rollback requested", lines: [`${fileCount(d)} file(s)${d.checkpoint_id ? ` · checkpoint ${d.checkpoint_id}` : ""}`], tone: "warn" }),
  "rollback.completed": (d) => ({ title: "Rollback completed", lines: [`${fileCount(d)} file(s) restored`, "Unrelated files left untouched"], tone: "warn" }),
  "approval.required": (d) => ({ title: "Approval required", lines: [`${d.operation || "?"} · ${fileCount(d)} file(s)`, d.agent ? `requested by ${d.agent}` : ""].filter(Boolean), tone: "warn" }),
  "approval.approved": (d) => ({ title: "Approval granted", lines: [`${d.operation || ""} · decided by ${d.decided_by || "operator"}`], tone: "ok" }),
  "approval.denied": (d) => ({ title: "Approval denied", lines: [`${d.operation || ""} · decided by ${d.decided_by || "operator"} · failed closed`], tone: "bad" }),
  "approval.expired": (d) => ({ title: "Approval expired", lines: ["No decision in time — failed closed"], tone: "warn" }),
};

function fallbackEvent(type, data) {
  const lines = [];
  if (data && typeof data === "object") {
    for (const [k, v] of Object.entries(data)) {
      if (v === null || v === undefined) continue;
      if (typeof v === "object") continue;
      lines.push(`${k}: ${String(v).slice(0, 140)}`);
      if (lines.length >= 3) break;
    }
  }
  return { title: prettyType(type), lines, tone: "" };
}

function eventCard(item, linkTask) {
  const data = (item && item.data) || {};
  const fmt = EVENT_TEXT[item.type] || fallbackEvent.bind(null, item.type);
  const info = fmt(data);
  const card = el("div", "activity-item" + (info.tone ? " tone-" + info.tone : ""));
  card.appendChild(el("div", "activity-title", info.title));
  if (info.lines.length) {
    const lines = el("div", "activity-lines");
    for (const line of info.lines) lines.appendChild(el("div", null, line));
    card.appendChild(lines);
  }
  const meta = el("div", "activity-meta");
  const time = el("span", null, fmtRel(item.timestamp));
  time.title = fmtAbs(item.timestamp);
  meta.appendChild(time);
  meta.appendChild(el("span", "mono", `#${item.seq} ${item.type}`));
  if (linkTask && item.task_id) {
    const link = el("a", "mono", shortId(item.task_id));
    link.href = "#/tasks/" + encodeURIComponent(item.task_id);
    meta.appendChild(link);
  }
  card.appendChild(meta);
  const details = document.createElement("details");
  details.className = "activity-raw";
  details.appendChild(el("summary", null, "raw event"));
  details.appendChild(el("pre", null, JSON.stringify(item, null, 1).slice(0, 2000)));
  card.appendChild(details);
  return card;
}

/* ---------- approvals ---------- */

function approvalFiles(approval) {
  const files = approval.files || approval.paths || [];
  return Array.isArray(files) ? files : [];
}

function approvalCard(approval, decide) {
  const card = el("div", "approval-card");
  card.appendChild(el("div", "appr-flag", "Action required"));
  const agent = approval.agent || "Agent";
  const op = approval.operation || "operation";
  card.appendChild(el("h4", null, `${agent} wants: ${op}`));
  if (approval.reason) {
    const why = el("div", "appr-why");
    why.appendChild(el("strong", null, "Why"));
    why.appendChild(el("span", null, esc(approval.reason)));
    card.appendChild(why);
  }
  const files = approvalFiles(approval);
  const fileBox = el("div", "appr-files");
  fileBox.appendChild(el("strong", null, `Files (${files.length})`));
  const ul = el("ul");
  if (!files.length) ul.appendChild(el("li", null, "–"));
  for (const path of files.slice(0, 12)) ul.appendChild(el("li", null, esc(path)));
  if (files.length > 12) ul.appendChild(el("li", null, `+${files.length - 12} more`));
  fileBox.appendChild(ul);
  card.appendChild(fileBox);
  const grid = el("dl", "appr-grid-kv");
  const rows = [
    ["Risk", esc(approval.risk || "–")],
    ["Operation", `${esc(approval.operation || "–")} (${esc(approval.resource || "–")})`],
    ["Tool", esc(approval.tool || "–")],
    ["Model", approval.model ? `${approval.model} (${approval.provider || "?"})` : "–"],
    ["Scope", ((approval.scopes || []).join(", ") || "–")],
    ["Expires", approval.expires_at ? `${fmtRel(approval.expires_at)} (${fmtAbs(approval.expires_at)})` : "–"],
  ];
  for (const [k, v] of rows) {
    grid.appendChild(el("dt", null, k));
    grid.appendChild(el("dd", null, v));
  }
  const taskRef = approval.task_ref || approval.task_id;
  if (taskRef) {
    grid.appendChild(el("dt", null, "Task"));
    const dd = el("dd");
    const link = el("a", null, shortId(taskRef));
    link.href = "#/tasks/" + encodeURIComponent(taskRef);
    dd.appendChild(link);
    grid.appendChild(dd);
  }
  card.appendChild(grid);
  const actions = el("div", "appr-actions");
  const deny = el("button", "btn danger small", "Deny");
  const allow = el("button", "btn primary small", "Allow");
  deny.addEventListener("click", () => decide(approval.id, false, card));
  allow.addEventListener("click", () => decide(approval.id, true, card));
  actions.appendChild(deny);
  actions.appendChild(allow);
  card.appendChild(actions);
  return card;
}

/* ---------- diff viewer ---------- */

function renderDiff(box, text, truncated) {
  box.innerHTML = "";
  if (!text || !String(text).trim()) {
    box.appendChild(el("div", "diff-empty", "No changes."));
    return;
  }
  const lines = String(text).split("\n");
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  let oldNo = 0;
  let newNo = 0;
  for (const line of lines) {
    const hunk = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    const row = el("div", "diff-row");
    if (hunk) {
      oldNo = Number(hunk[1]);
      newNo = Number(hunk[2]);
      row.classList.add("hunk");
      row.appendChild(el("span", "ln", "···"));
      row.appendChild(el("span", "lc", line));
    } else if (line.startsWith("+") && !line.startsWith("+++")) {
      row.classList.add("add");
      row.appendChild(el("span", "ln", String(newNo)));
      row.appendChild(el("span", "lc", line));
      newNo += 1;
    } else if (line.startsWith("-") && !line.startsWith("---")) {
      row.classList.add("del");
      row.appendChild(el("span", "ln", String(oldNo)));
      row.appendChild(el("span", "lc", line));
      oldNo += 1;
    } else if (line.startsWith("+++") || line.startsWith("---") ||
               line.startsWith("diff ") || line.startsWith("index ")) {
      row.classList.add("hunk");
      row.appendChild(el("span", "ln", ""));
      row.appendChild(el("span", "lc", line));
    } else {
      if (line.startsWith(" ")) {
        oldNo += 1;
        newNo += 1;
      }
      row.appendChild(el("span", "ln", line.startsWith(" ") ? String(newNo - 1) : ""));
      row.appendChild(el("span", "lc", line));
    }
    box.appendChild(row);
  }
  if (truncated) box.appendChild(el("div", "diff-empty", "…(truncated by the server)"));
}

/* ---------- dashboard ---------- */

function statCard(label, value, sub) {
  const card = el("div", "stat");
  card.appendChild(el("div", "stat-label", label));
  card.appendChild(el("div", "stat-value", value));
  if (sub) card.appendChild(el("div", "stat-sub", sub));
  return card;
}

async function renderDashboard() {
  const actor = state.session ? state.session.actor : "there";
  document.getElementById("d-greet").textContent = `${greet()}, ${actor}`;
  const load = async () => {
    const snap = snapEpoch();
    let data;
    try {
      data = await api("/api/v1/dashboard");
    } catch (err) {
      if (stale(snap)) return;
      errorState(document.getElementById("d-stats"), "Unable to load status", err, load);
      return;
    }
    if (stale(snap)) return;
    setApprBadge(data.approvals_waiting || 0);
    const stats = document.getElementById("d-stats");
    stats.innerHTML = "";
    const tasks = data.tasks || {};
    stats.appendChild(statCard("Running", esc(tasks.running || 0)));
    stats.appendChild(statCard("Queued", esc(tasks.queued || 0)));
    stats.appendChild(statCard("Waiting", esc(tasks.waiting_approval || 0), "approval"));
    stats.appendChild(statCard("Failed", esc(tasks.failed || 0)));
    const models = data.models || {};
    stats.appendChild(statCard("Models", `${models.healthy || 0} healthy`,
      `${models.degraded || 0} degraded · ${models.unavailable || 0} unavailable`));
    const workers = data.workers || {};
    stats.appendChild(statCard("Workers", `${workers.busy || 0} / ${workers.max_per_project || 0} busy`,
      workers.running ? "dispatcher live" : "dispatcher stopped"));
    stats.appendChild(statCard("Approvals", esc(data.approvals_waiting || 0), "waiting"));
    const feed = document.getElementById("d-activity");
    feed.innerHTML = "";
    const activity = data.recent_activity || [];
    if (!activity.length) {
      feed.appendChild(el("div", "muted", "No activity yet."));
    }
    for (const item of activity.slice(0, 8)) feed.appendChild(eventCard(item, true));
    renderSecurityPosture(document.getElementById("d-security"), true);
    loadActiveRun();
  };
  await load();
  state.pollers.push(setInterval(load, 5000));
}

async function loadActiveRun() {
  const snap = snapEpoch();
  const box = document.getElementById("d-active");
  if (!box) return;
  let task = null;
  let waiting = false;
  try {
    const running = await api("/api/v1/tasks?status=RUNNING&limit=1");
    task = (running.tasks || [])[0] || null;
    if (!task) {
      const pending = await api("/api/v1/tasks?status=WAITING_APPROVAL&limit=1");
      task = (pending.tasks || [])[0] || null;
      waiting = !!task;
    }
  } catch (err) {
    return; // dashboard stats already cover failure; stay quiet here
  }
  if (stale(snap)) return;
  box.innerHTML = "";
  if (!task) return;
  const card = el("div", "surface active-run" + (waiting ? " waiting" : ""));
  card.appendChild(el("div", "run-flag", waiting ? "Awaiting approval" : "Active run"));
  card.appendChild(el("h3", null, task.requirement || task.task_id));
  const strip = el("div", "run-stages");
  const idx = STAGES.indexOf(task.stage || "");
  for (const [i, stage] of STAGES.slice(0, 9).entries()) {
    const cls = i < idx ? "rs done" : (i === idx ? "rs now" : "rs");
    strip.appendChild(el("span", cls, (i < idx ? "✓ " : i === idx ? "● " : "○ ") + stage));
  }
  card.appendChild(strip);
  const progress = document.createElement("progress");
  progress.className = "run-progress";
  progress.max = STAGES.length - 1;
  progress.value = Math.max(0, idx);
  progress.setAttribute("aria-label", "Pipeline stage " + Math.max(0, idx + 1) + " of " + STAGES.length);
  card.appendChild(progress);
  const meta = el("div", "run-meta");
  const stage = el("span", null, `stage ${task.stage || "–"}`);
  meta.appendChild(stage);
  if (task.model) meta.appendChild(el("span", null, `${task.model}${task.provider ? " / " + task.provider : ""}`));
  const elapsed = el("span", null, "");
  elapsed.id = "run-elapsed";
  const tick = () => {
    elapsed.textContent = task.started_at
      ? `${waiting ? "Waiting for" : "Running for"} ${fmtDur(Date.now() / 1000 - task.started_at)}`
      : (waiting ? "Waiting for operator" : "Starting…");
  };
  tick();
  state.pollers.push(setInterval(tick, 1000));
  meta.appendChild(elapsed);
  card.appendChild(meta);
  const actions = el("div", "btnrow");
  const open = el("a", "btn small", "Open task");
  open.href = "#/tasks/" + encodeURIComponent(task.task_id);
  actions.appendChild(open);
  if (!waiting) {
    const pause = el("button", "btn small", "Pause");
    pause.addEventListener("click", async () => {
      pause.disabled = true;
      try {
        await api(`/api/v1/tasks/${task.task_id}/pause`,
          { method: "POST", body: { expected_version: task.version } });
        loadActiveRun();
      } catch (err) {
        pause.disabled = false;
      }
    });
    actions.appendChild(pause);
  }
  const cancel = el("button", "btn danger small", "Cancel");
  cancel.addEventListener("click", async () => {
    cancel.disabled = true;
    try {
      await api(`/api/v1/tasks/${task.task_id}/cancel`,
        { method: "POST", body: { expected_version: task.version } });
      loadActiveRun();
    } catch (err) {
      cancel.disabled = false;
    }
  });
  actions.appendChild(cancel);
  card.appendChild(actions);
  box.appendChild(card);
}

/* ---------- tasks ---------- */

const TASK_FILTERS = [
  ["ALL", "All", () => true],
  ["RUNNING", "Running", (t) => t.status === "RUNNING"],
  ["QUEUED", "Queued", (t) => t.status === "QUEUED"],
  ["WAITING", "Waiting", (t) => t.status === "WAITING_APPROVAL" || t.status === "PAUSED"],
  ["SUCCEEDED", "Succeeded", (t) => t.status === "SUCCEEDED"],
  ["FAILED", "Failed", (t) => t.status === "FAILED"],
  ["CANCELLED", "Cancelled", (t) => t.status === "CANCELLED" || t.status === "ROLLED_BACK"],
];

function renderTaskList() {
  const list = document.getElementById("task-list");
  if (!list) return;
  list.innerHTML = "";
  const needle = state.cache.search.trim().toLowerCase();
  const filter = TASK_FILTERS.find(([key]) => key === state.cache.filter) || TASK_FILTERS[0];
  const tasks = state.cache.tasks.filter(filter[2]).filter((task) => {
    if (!needle) return true;
    return (task.requirement || "").toLowerCase().includes(needle) ||
      (task.task_id || "").toLowerCase().includes(needle);
  });
  if (!tasks.length) {
    if (!state.cache.tasks.length) {
      emptyState(list, "No tasks yet.", "Describe what you want Forge to build above.", "", "");
    } else {
      list.appendChild(el("div", "muted", "No tasks match this filter."));
    }
    return;
  }
  for (const task of tasks) {
    const row = el("a", "taskrow");
    row.href = "#/tasks/" + encodeURIComponent(task.task_id);
    row.appendChild(statusDot(task.status, task.status === "RUNNING"));
    const main = el("div", "taskrow-main");
    main.appendChild(el("div", "taskrow-title", task.requirement || task.task_id));
    const bits = [shortId(task.task_id), task.stage || "–", task.mode || "–", "v" + task.version];
    if (task.model) bits.push(`${task.model}${task.provider ? "/" + task.provider : ""}`);
    bits.push(fmtRel(task.updated_at || task.created_at));
    main.appendChild(el("div", "taskrow-meta", bits.join(" · ")));
    row.appendChild(main);
    row.appendChild(statusBadge(task.status));
    list.appendChild(row);
  }
}

async function renderTasks() {
  document.getElementById("composer-project").textContent = state.session.project_id;
  document.getElementById("composer-profile").textContent = state.session.profile;
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
  const filterBox = document.getElementById("task-filters");
  for (const [key, label] of TASK_FILTERS) {
    const btn = el("button", null, label);
    btn.type = "button";
    btn.setAttribute("aria-pressed", key === state.cache.filter ? "true" : "false");
    btn.addEventListener("click", () => {
      state.cache.filter = key;
      filterBox.querySelectorAll("button").forEach((other) => {
        other.setAttribute("aria-pressed",
          other.textContent === label ? "true" : "false");
      });
      renderTaskList();
    });
    filterBox.appendChild(btn);
  }
  const search = document.getElementById("task-search");
  search.value = state.cache.search;
  search.addEventListener("input", () => {
    state.cache.search = search.value;
    renderTaskList();
  });
  const load = async () => {
    const snap = snapEpoch();
    try {
      const data = await api("/api/v1/tasks?limit=50");
      if (stale(snap)) return;
      state.cache.tasks = data.tasks || [];
      renderTaskList();
    } catch (err) {
      if (stale(snap)) return;
      errorState(document.getElementById("task-list"), "Unable to load tasks", err, load);
    }
  };
  await load();
  state.pollers.push(setInterval(load, 4000));
}

/* ---------- task detail ---------- */

async function renderTask() {
  const taskId = state.taskId;
  state.cursor = 0;
  state.histMax = 0;
  state.seenSeq = new Set();
  state.events = [];
  state.cache.task = null;

  const loadTask = async () => {
    const snap = snapEpoch();
    let data;
    try {
      data = await api("/api/v1/tasks/" + encodeURIComponent(taskId));
    } catch (err) {
      if (stale(snap)) return null;
      document.getElementById("t-error").textContent = err.message;
      return null;
    }
    if (stale(snap)) return null;
    const task = data.task;
    state.cache.task = task;
    document.getElementById("t-title").textContent = task.requirement || ("Task " + task.task_id);
    const badges = document.getElementById("t-badges");
    badges.innerHTML = "";
    badges.appendChild(statusBadge(task.status));
    if (task.stage) {
      const stage = el("span", "status-badge info", "stage: " + task.stage);
      badges.appendChild(stage);
    }
    const meta = document.getElementById("t-meta");
    meta.innerHTML = "";
    const bits = [task.task_id, "v" + task.version, "mode " + (task.mode || "–"),
      "actor " + (task.actor || "–"), "attempts " + (task.attempts || 0)];
    if (task.model) bits.push(`${task.model} (${task.provider || "?"})`);
    bits.push("created " + fmtRel(task.created_at));
    if (task.finished_at) bits.push("finished " + fmtRel(task.finished_at));
    meta.appendChild(el("div", null, bits.join("  ·  ")));
    document.getElementById("t-stage-label").textContent =
      task.stage ? `now: ${task.stage}` : "";
    renderActions(task);
    renderTimeline(task);
    renderDetails(task);
    if (TERMINAL.includes(task.status)) {
      loadVerification(taskId);
      loadReport(taskId);
    }
    loadCheckpoints(task);
    return task;
  };

  const task = await loadTask();
  if (!task) return;
  await loadHistory(taskId);
  renderTimeline(state.cache.task || task);
  openStream(taskId);
  state.pollers.push(setInterval(async () => {
    const current = await loadTask();
    if (current && TERMINAL.includes(current.status)) {
      stopPollers();
      openStream(taskId);
      setTimeout(() => { if (state.eventSource) state.eventSource.close(); }, 5000);
    }
  }, 3000));
}

function renderActions(task) {
  const box = document.getElementById("t-actions");
  box.innerHTML = "";
  const terminal = TERMINAL.includes(task.status);
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
              "Rollback needs approval: see the Approval center (request " +
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
  if (!ol) return;
  ol.innerHTML = "";
  const seen = new Set(state.events.filter((e) => e.type === "stage.started")
    .map((e) => (e.data && e.data.stage) || ""));
  const terminal = TERMINAL.includes(task.status);
  const waiting = task.status === "WAITING_APPROVAL";
  const failed = task.status === "FAILED" || task.status === "CANCELLED";
  for (const stage of STAGES) {
    const li = el("li", "pipeline-node");
    li.appendChild(el("span", "node-dot"));
    const body = el("div");
    body.appendChild(el("div", "node-label", stage));
    li.appendChild(body);
    const marker = el("span", "node-state", "Pending");
    const isCurrent = task.stage === stage && !terminal;
    if (seen.has(stage) && !isCurrent) {
      li.classList.add("done");
      marker.textContent = "✓ Done";
    } else if (isCurrent && failed) {
      li.classList.add("failed");
      marker.textContent = "✕ Stopped";
    } else if (isCurrent && waiting) {
      li.classList.add("waiting");
      marker.textContent = "! Waiting";
    } else if (isCurrent) {
      li.classList.add("active");
      marker.textContent = "● Active";
    } else if (terminal && task.status === "SUCCEEDED" && STAGES.indexOf(stage) <= STAGES.indexOf(task.stage || "")) {
      li.classList.add("done");
      marker.textContent = "✓ Done";
    }
    li.appendChild(marker);
    ol.appendChild(li);
  }
}

function renderDetails(task) {
  const box = document.getElementById("t-details");
  box.innerHTML = "";
  box.appendChild(kvTable([
    ["actor", task.actor || "–"],
    ["attempts", task.attempts || 0],
    ["files", (task.files || []).join(", ") || "–", true],
    ["checkpoint", task.checkpoint_id || "–", true],
    ["model", task.model ? `${task.model} (${task.provider || "?"})` : "–"],
    ["rollback", String(task.rollback)],
    ["error", task.error ? String(task.error).slice(0, 300) : "–"],
  ]));
}

async function loadVerification(taskId) {
  const snap = snapEpoch();
  try {
    const data = await api(`/api/v1/tasks/${encodeURIComponent(taskId)}/verification`);
    if (stale(snap)) return;
    const box = document.getElementById("t-verification");
    if (!box) return;
    box.innerHTML = "";
    const rows = [];
    for (const [k, v] of Object.entries(data)) {
      if (k === "task_id" || k === "status") continue;
      rows.push([prettyType(k), typeof v === "object" ? JSON.stringify(v) : esc(v)]);
    }
    if (!rows.length) {
      box.appendChild(el("div", "muted", "No verification gates recorded."));
      return;
    }
    box.appendChild(kvTable(rows));
  } catch (err) { /* keep quiet; task may still run */ }
}

async function loadReport(taskId) {
  const snap = snapEpoch();
  try {
    const data = await api(`/api/v1/tasks/${encodeURIComponent(taskId)}/report`);
    if (stale(snap)) return;
    const box = document.getElementById("t-report");
    if (!box) return;
    box.innerHTML = "";
    const report = data.report || {};
    const rows = [];
    for (const key of ["final_status", "stages", "model", "provider",
        "files_changed", "tests_run", "retries", "duration_seconds",
        "acceptance", "error"]) {
      if (report[key] === undefined) continue;
      rows.push([prettyType(key),
        typeof report[key] === "object" ? JSON.stringify(report[key]) : esc(report[key])]);
    }
    if (!rows.length) {
      box.appendChild(el("div", "muted", "No report recorded."));
      return;
    }
    box.appendChild(kvTable(rows));
  } catch (err) { /* keep quiet */ }
}

async function loadCheckpoints(task) {
  const snap = snapEpoch();
  try {
    const data = await api(`/api/v1/tasks/${encodeURIComponent(task.task_id)}/checkpoints`);
    if (stale(snap)) return;
    const box = document.getElementById("t-checkpoints");
    if (!box) return;
    box.innerHTML = "";
    if (!data.checkpoints.length) {
      box.appendChild(el("div", "muted", "No checkpoints recorded."));
      return;
    }
    for (const cp of data.checkpoints) {
      const row = el("div", "feeditem",
        `${cp.checkpoint_id} · ${cp.status} · ${cp.affected_paths.length} paths`);
      box.appendChild(row);
    }
  } catch (err) { /* keep quiet */ }
}

async function loadHistory(taskId) {
  const snap = snapEpoch();
  try {
    const data = await api(`/api/v1/tasks/${encodeURIComponent(taskId)}/events?after=0&limit=500`);
    if (stale(snap)) return;
    state.histMax = data.latest || 0;
    for (const item of data.events) pushEvent(item);
    state.cursor = data.latest || 0;
  } catch (err) {
    document.getElementById("t-error").textContent = err.message;
  }
}

const MAX_FEED_ITEMS = 800;

function pushEvent(item) {
  if (!item || state.seenSeq.has(item.seq)) return;
  state.seenSeq.add(item.seq);
  state.events.push(item);
  if (item.seq > state.cursor) state.cursor = item.seq;
  const feed = document.getElementById("t-events");
  if (feed) {
    feed.appendChild(eventCard(item, false));
    while (feed.children.length > MAX_FEED_ITEMS) feed.removeChild(feed.firstChild);
    feed.scrollTop = feed.scrollHeight;
  }
  if (item.type === "stage.started" && state.cache.task &&
      item.seq > state.histMax) {
    state.cache.task = Object.assign({}, state.cache.task,
      { stage: (item.data && item.data.stage) || state.cache.task.stage });
    renderTimeline(state.cache.task);
  }
}

function openStream(taskId) {
  const label = document.getElementById("t-stream-state");
  const url = `/api/v1/tasks/${encodeURIComponent(taskId)}/events/stream?after=${state.cursor}`;
  const source = new EventSource(url);
  state.eventSource = source;
  source.onopen = () => { if (label) label.textContent = "(live)"; };
  source.onerror = () => { if (label) label.textContent = "(reconnecting…)"; };
  source.onmessage = (msg) => {
    try {
      pushEvent(JSON.parse(msg.data));
    } catch (err) { /* ignore malformed frames */ }
  };
  source.addEventListener("end", () => source.close());
}

/* ---------- projects ---------- */

async function renderProjects() {
  const snap = snapEpoch();
  const list = document.getElementById("p-list");
  try {
    const data = await api("/api/v1/projects");
    if (stale(snap)) return;
    list.innerHTML = "";
    if (!data.projects.length) {
      emptyState(list, "No projects registered.", "Start the server with a project root to begin.", "", "");
    }
    for (const project of data.projects) {
      const card = el("div", "project-card");
      card.appendChild(el("h4", null, project.name || project.project_id));
      card.appendChild(el("div", "sub mono", project.project_id));
      card.appendChild(el("div", "sub mono", project.root || ""));
      if (state.session && project.project_id === state.session.project_id) {
        const row = el("div", "badge-row");
        row.appendChild(el("span", "status-badge ok", "● current session"));
        card.appendChild(row);
      }
      list.appendChild(card);
    }
  } catch (err) {
    errorState(list, "Unable to load projects", err, renderProjects);
  }
  const current = document.getElementById("p-current");
  try {
    const data = await api(`/api/v1/projects/${encodeURIComponent(state.session.project_id)}`);
    if (stale(snap)) return;
    current.innerHTML = "";
    current.appendChild(kvTable([
      ["name", data.name || "–"],
      ["project id", data.project_id || "–", true],
      ["root", data.root || "–", true],
    ]));
    const counts = el("div", "section-header");
    counts.appendChild(el("h3", null, "Task counts"));
    current.appendChild(counts);
    const table = el("table", "data-table");
    for (const [k, v] of Object.entries(data.counts || {})) {
      const tr = el("tr");
      tr.appendChild(el("td", null, prettyType(k)));
      tr.appendChild(el("td", null, esc(v)));
      table.appendChild(tr);
    }
    current.appendChild(table);
    if (data.active_task) {
      const head = el("div", "section-header");
      head.appendChild(el("h3", null, "Active task"));
      current.appendChild(head);
      const link = el("a", null, data.active_task.requirement || data.active_task.task_id);
      link.href = "#/tasks/" + encodeURIComponent(data.active_task.task_id);
      current.appendChild(link);
    }
    if ((data.recent_runs || []).length) {
      const head = el("div", "section-header");
      head.appendChild(el("h3", null, "Recent runs"));
      current.appendChild(head);
      const ul = el("ul", "file-list");
      for (const run of data.recent_runs) {
        const li = el("li");
        const link = el("a", null, `${shortId(run.task_id)} · ${run.status} · ${fmtRel(run.updated_at)}`);
        link.href = "#/tasks/" + encodeURIComponent(run.task_id);
        li.appendChild(link);
        ul.appendChild(li);
      }
      current.appendChild(ul);
    }
    if (data.git && data.git.available) {
      const head = el("div", "section-header");
      head.appendChild(el("h3", null, "Git"));
      current.appendChild(head);
      current.appendChild(el("div", "muted",
        `${data.git.branch} · ${data.git.head} · ${data.git.working_tree ? "changes present" : "working tree clean"}`));
    }
  } catch (err) {
    errorState(current, "Unable to load current project", err, renderProjects);
  }
}

/* ---------- models ---------- */

function fmtContext(n) {
  const num = Number(n);
  if (!num) return "–";
  if (num >= 1000) return (num / 1000).toFixed(num % 1000 ? 1 : 0) + "k";
  return String(num);
}

async function renderModels() {
  const snap = snapEpoch();
  try {
    const data = await api("/api/v1/models");
    if (stale(snap)) return;
    const policyBox = document.getElementById("m-policy");
    policyBox.innerHTML = "";
    const policy = data.routing_policy || {};
    const keys = Object.keys(policy);
    if (!keys.length) {
      policyBox.appendChild(el("div", "muted", "Default routing policy."));
    } else {
      policyBox.appendChild(kvTable(keys.slice(0, 12).map((k) => {
        const v = policy[k];
        return [prettyType(k), typeof v === "object" ? JSON.stringify(v).slice(0, 120) : esc(v)];
      })));
    }
    const box = document.getElementById("m-list");
    box.innerHTML = "";
    const models = data.models || [];
    if (!models.length) {
      emptyState(box, "No models currently available.", "Check the Model Fabric configuration.", "", "");
    }
    for (const model of models) {
      const card = el("div", "model-card");
      card.appendChild(el("h4", null, model.name || "?"));
      card.appendChild(el("div", "sub",
        `${model.provider || "?"} · ${fmtContext(model.context_window)} context`));
      const health = (model.health || {}).status || "unknown";
      const line = el("div", "healthline");
      const tone = health === "healthy" ? "ok" : (health === "degraded" ? "warn" : "bad");
      line.appendChild(el("span", "status-dot " + tone));
      line.appendChild(el("span", null, health));
      card.appendChild(line);
      const caps = el("div", "cap-row");
      if (model.local) caps.appendChild(el("span", "cap local", "local"));
      else caps.appendChild(el("span", "cap remote", "remote"));
      if (model.free) caps.appendChild(el("span", "cap free", "free"));
      for (const cap of (model.capabilities || []).slice(0, 8)) {
        caps.appendChild(el("span", "cap", cap));
      }
      card.appendChild(caps);
      const dl = el("dl");
      dl.appendChild(metricRow("Latency", model.latency_ms !== undefined && model.latency_ms !== null ? model.latency_ms + " ms" : "–"));
      dl.appendChild(metricRow("Reliability", model.reliability !== undefined && model.reliability !== null ? Math.round(Number(model.reliability) * 100) + "%" : "–"));
      card.appendChild(dl);
      box.appendChild(card);
    }
    const routing = document.getElementById("m-routing");
    routing.innerHTML = "";
    const entries = data.recent_routing || [];
    if (!entries.length) {
      routing.appendChild(el("div", "muted", "No routing decisions recorded yet."));
    } else {
      const table = el("table", "data-table");
      const head = el("tr");
      for (const h of ["model", "capability", "result", "latency", "tokens"]) {
        head.appendChild(el("th", null, h));
      }
      table.appendChild(head);
      for (const entry of entries.slice(0, 20)) {
        const tr = el("tr");
        tr.appendChild(el("td", "mono", esc(entry.model)));
        tr.appendChild(el("td", null, esc(entry.capability)));
        const ok = entry.success === true && !entry.failure;
        const result = el("td");
        result.appendChild(el("span", "status-dot " + (ok ? "ok" : "bad")));
        result.appendChild(el("span", null, ok ? " ok" : " failed"));
        if (entry.error) result.title = String(entry.error).slice(0, 200);
        tr.appendChild(result);
        tr.appendChild(el("td", null, entry.latency !== undefined ? entry.latency + " ms" : "–"));
        const tokens = entry.tokens || {};
        tr.appendChild(el("td", "mono",
          tokens.input !== undefined ? `${tokens.input}/${tokens.output}` : "–"));
        table.appendChild(tr);
      }
      routing.appendChild(table);
    }
  } catch (err) {
    if (stale(snap)) return;
    errorState(document.getElementById("m-list"), "Unable to load models", err, renderModels);
  }
  try {
    const data = await api("/api/v1/providers");
    if (stale(snap)) return;
    const box = document.getElementById("m-providers");
    box.innerHTML = "";
    const providers = data.providers || [];
    const health = data.health || {};
    if (!providers.length) {
      box.appendChild(el("div", "muted", "No providers registered."));
    }
    for (const provider of providers) {
      const card = el("div", "model-card");
      card.appendChild(el("h4", null, provider.display_name || provider.name));
      card.appendChild(el("div", "sub", `kind: ${provider.kind || "?"}`));
      const caps = el("div", "cap-row");
      caps.appendChild(el("span", provider.local ? "cap local" : "cap remote",
        provider.local ? "local" : "remote"));
      if (provider.free) caps.appendChild(el("span", "cap free", "free"));
      for (const cap of (provider.capabilities || []).slice(0, 6)) {
        caps.appendChild(el("span", "cap", cap));
      }
      card.appendChild(caps);
      const h = health[provider.name];
      card.appendChild(el("div", "muted",
        "health: " + (h && h.status ? h.status : "unknown")));
      box.appendChild(card);
    }
  } catch (err) {
    if (stale(snap)) return;
    errorState(document.getElementById("m-providers"), "Unable to load providers", err, renderModels);
  }
}

function metricRow(label, value) {
  const row = el("div", "metric");
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  dd.textContent = value;
  row.appendChild(dt);
  row.appendChild(dd);
  return row;
}

/* ---------- permissions ---------- */

function levelBadge(level) {
  const map = {
    safe: ["Allowed", "ok"], approval_required: ["Approval", "warn"],
    blocked: ["Denied", "bad"], unknown: ["Unknown", ""],
  };
  const [label, tone] = map[level] || ["Unknown", ""];
  const badge = el("span", "status-badge " + tone);
  badge.appendChild(el("span", "status-dot " + tone));
  badge.appendChild(el("span", null, label));
  return badge;
}

async function renderPermissions() {
  const load = async () => {
    const snap = snapEpoch();
    let data;
    try {
      data = await api("/api/v1/permissions");
    } catch (err) {
      if (stale(snap)) return;
      errorState(document.getElementById("pm-effective"), "Unable to load permissions", err, load);
      return;
    }
    if (stale(snap)) return;
    setApprBadge((data.pending_approvals || []).length);
    const banner = document.getElementById("pm-banner");
    banner.innerHTML = "";
    const [title, blurb] = PROFILE_BLURBS[data.profile] || [String(data.profile || "?").toUpperCase() + " MODE", ""];
    const card = el("div", "surface");
    card.appendChild(el("div", "run-flag", title));
    if (blurb) card.appendChild(el("p", "muted", blurb));
    banner.appendChild(card);

    const eff = document.getElementById("pm-effective");
    eff.innerHTML = "";
    const table = el("table", "data-table");
    const head = el("tr");
    head.appendChild(el("th", null, "operation"));
    head.appendChild(el("th", null, "status"));
    table.appendChild(head);
    for (const [op, level] of Object.entries(data.effective || {})) {
      const tr = el("tr");
      tr.appendChild(el("td", null, OP_LABELS[op] || prettyType(op)));
      const td = el("td");
      td.appendChild(levelBadge(level));
      tr.appendChild(td);
      table.appendChild(tr);
    }
    eff.appendChild(table);

    const box = document.getElementById("pm-approvals");
    box.innerHTML = "";
    const pending = data.pending_approvals || [];
    if (!pending.length) {
      emptyState(box, "All clear.", "No actions are waiting for your approval.", "", "");
    }
    for (const approval of pending) {
      box.appendChild(approvalCard(approval, decide));
    }

    const rules = document.getElementById("pm-rules");
    rules.innerHTML = "";
    const policyRules = data.policy_rules || [];
    if (!policyRules.length) {
      rules.appendChild(el("div", "muted", "No fine-grained policy rules attached."));
    }
    for (const rule of policyRules) {
      const row = el("div", "feeditem");
      const effect = el("strong", null, String(rule.effect || "?").toUpperCase() + " ");
      row.appendChild(effect);
      row.appendChild(el("span", null,
        `${rule.operation || "?"} on ${rule.resource || "?"}${rule.scope ? " · " + rule.scope : ""} (${rule.id || "?"})`));
      rules.appendChild(row);
    }
  };
  const decide = async (id, allow, card) => {
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
  const snap = snapEpoch();
  const projectId = encodeURIComponent(state.session.project_id);
  try {
    const data = await api(`/api/v1/projects/${projectId}/git`);
    if (stale(snap)) return;
    const box = document.getElementById("g-state");
    box.innerHTML = "";
    if (!data.available) {
      emptyState(box, "Git unavailable.", data.reason || "Not a git repository.", "", "");
    } else {
      const rows = [
        ["branch", data.branch || "–", true],
        ["HEAD", data.head || "–", true],
        ["working tree", data.working_tree ? "changes present" : "clean"],
      ];
      box.appendChild(kvTable(rows));
      const files = data.changed_files || [];
      if (files.length) {
        const head = el("div", "section-header");
        head.appendChild(el("h3", null, `Changed files (${files.length})`));
        box.appendChild(head);
        const ul = el("ul", "file-list");
        for (const path of files.slice(0, 40)) ul.appendChild(el("li", null, esc(path)));
        if (files.length > 40) ul.appendChild(el("li", null, `+${files.length - 40} more`));
        box.appendChild(ul);
      }
      const commits = data.recent_commits || [];
      if (commits.length) {
        const head = el("div", "section-header");
        head.appendChild(el("h3", null, "Recent commits"));
        box.appendChild(head);
        const ul = el("ul", "commit-list");
        for (const commit of commits) ul.appendChild(el("li", null, esc(commit)));
        box.appendChild(ul);
      }
    }
  } catch (err) {
    if (stale(snap)) return;
    errorState(document.getElementById("g-state"), "Unable to load repository", err, renderGit);
  }
  for (const [id, staged] of [["g-diff", false], ["g-diff-staged", true]]) {
    try {
      const data = await api(
        `/api/v1/projects/${projectId}/git/diff?staged=${staged}`);
      if (stale(snap)) return;
      renderDiff(document.getElementById(id), data.diff, data.truncated);
    } catch (err) {
      if (stale(snap)) return;
      renderDiff(document.getElementById(id), "", false);
      document.getElementById(id).appendChild(el("div", "diff-empty", err.message));
    }
  }
}

/* ---------- activity ---------- */

async function renderActivity() {
  const load = async () => {
    const snap = snapEpoch();
    try {
      const data = await api("/api/v1/dashboard");
      if (stale(snap)) return;
      const list = document.getElementById("act-list");
      list.innerHTML = "";
      const activity = data.recent_activity || [];
      if (!activity.length) {
        emptyState(list, "No activity yet.", "Submit a task and watch Forge work.", "Create a task", "#/tasks");
        return;
      }
      for (const item of activity) list.appendChild(eventCard(item, true));
    } catch (err) {
      if (stale(snap)) return;
      errorState(document.getElementById("act-list"), "Unable to load activity", err, load);
    }
  };
  await load();
  state.pollers.push(setInterval(load, 6000));
}

/* ---------- approvals center ---------- */

async function renderApprovals() {
  const load = async () => {
    const snap = snapEpoch();
    let data;
    try {
      data = await api("/api/v1/approvals");
    } catch (err) {
      if (stale(snap)) return;
      errorState(document.getElementById("ap-list"), "Unable to load approvals", err, load);
      return;
    }
    if (stale(snap)) return;
    const approvals = data.approvals || [];
    setApprBadge(approvals.length);
    const box = document.getElementById("ap-list");
    box.innerHTML = "";
    if (!approvals.length) {
      emptyState(box, "All clear.", "No actions are waiting for your approval.", "", "");
      return;
    }
    for (const approval of approvals) {
      box.appendChild(approvalCard(approval, decide));
    }
  };
  const decide = async (id, allow, card) => {
    const buttons = card ? card.querySelectorAll("button") : [];
    buttons.forEach((btn) => { btn.disabled = true; });
    try {
      await api(`/api/v1/approvals/${encodeURIComponent(id)}/${allow ? "approve" : "deny"}`,
        { method: "POST", body: {} });
    } catch (err) {
      buttons.forEach((btn) => { btn.disabled = false; });
      const note = el("p", "error", err.message);
      if (card) card.appendChild(note);
      return;
    }
    load();
  };
  await load();
  state.pollers.push(setInterval(load, 4000));
}

/* ---------- system ---------- */

async function renderSystem() {
  const snap = snapEpoch();
  try {
    const health = await api("/api/v1/health");
    if (stale(snap)) return;
    const box = document.getElementById("sys-backend");
    box.innerHTML = "";
    const rows = [];
    for (const [label, key, mono] of [["status", "status"], ["auth mode", "auth_mode"],
        ["worker", "worker"], ["projects", "projects"]]) {
      if (health[key] === undefined) continue;
      rows.push([label, esc(health[key]), !!mono]);
    }
    box.appendChild(kvTable(rows.length ? rows : [["status", "unknown"]]));
  } catch (err) {
    if (stale(snap)) return;
    errorState(document.getElementById("sys-backend"), "Backend unreachable", err, renderSystem);
  }
  try {
    const data = await api("/api/v1/dashboard");
    if (stale(snap)) return;
    const workers = data.workers || {};
    const box = document.getElementById("sys-workers");
    box.innerHTML = "";
    box.appendChild(kvTable([
      ["dispatcher", workers.running ? "live" : "stopped"],
      ["busy", `${workers.busy || 0} / ${workers.max_per_project || 0}`],
      ["project", data.project_id || "–", true],
    ]));
  } catch (err) {
    if (stale(snap)) return;
    errorState(document.getElementById("sys-workers"), "Unable to load workers", err, renderSystem);
  }
  const session = document.getElementById("sys-session");
  session.innerHTML = "";
  if (state.session) {
    session.appendChild(kvTable([
      ["actor", state.session.actor || "–"],
      ["project", state.session.project_id || "–", true],
      ["profile", state.session.profile || "–"],
    ]));
  }
  renderSecurityPosture(document.getElementById("sys-security"), false);
}

/* ---------- desktop (A35) ---------- */

const DESK_RESULT_TONES = { ALLOW: "ok", DENY: "bad", REQUIRE_APPROVAL: "warn" };

function deskResultBox(result) {
  const box = document.getElementById("desk-result");
  box.innerHTML = "";
  box.classList.remove("hidden");
  const tone = DESK_RESULT_TONES[result.decision] || "";
  const rows = [
    ["decision", result.decision, tone],
    ["executed", result.executed ? "yes" : "no"],
    ["risk", result.risk || "–"],
    ["approval required", result.approval_required ? "yes" : "no"],
  ];
  if (result.approval_request_id) {
    rows.push(["approval id", result.approval_request_id, true]);
  }
  if (result.error) {
    rows.push(["error", result.error.kind + ": " + result.error.message, "bad"]);
  }
  box.appendChild(kvTable(rows));
  const note = el("p", "muted");
  note.textContent = result.executed
    ? "Action executed on the simulated desktop."
    : "Nothing was executed on the desktop.";
  box.appendChild(note);
}

async function renderDesktop() {
  const snap = snapEpoch();
  // Provider + profile header.
  try {
    const caps = await api("/api/v1/desktop/capabilities");
    if (stale(snap)) return;
    const profileBox = document.getElementById("desk-profile");
    profileBox.innerHTML = "";
    profileBox.appendChild(kvTable([
      ["session profile", state.session ? state.session.profile : "–"],
      ["desktop profile", caps.profile || "–"],
    ]));
    const providerBox = document.getElementById("desk-provider");
    providerBox.innerHTML = "";
    providerBox.appendChild(kvTable([
      ["provider", caps.provider || "–", true],
      ["status", caps.status || "–"],
    ]));
    const note = el("p", "muted");
    note.textContent = (caps.note || "").trim();
    providerBox.appendChild(note);
    // Capability matrix.
    const list = document.getElementById("desk-capabilities");
    list.innerHTML = "";
    const table = el("table", "data-table");
    const head = el("tr");
    for (const label of ["action", "risk", "profile", "executable"]) {
      head.appendChild(el("th", null, label));
    }
    table.appendChild(head);
    const select = document.getElementById("desk-action");
    select.innerHTML = "";
    for (const item of caps.capabilities || []) {
      const row = el("tr");
      row.appendChild(el("td", null, item.action));
      row.appendChild(el("td", null, item.risk || "–"));
      row.appendChild(el("td", null, item.profile || "–"));
      row.appendChild(el("td", null, item.executable ? "yes" : "no"));
      table.appendChild(row);
      const option = el("option", null, item.action);
      option.value = item.action;
      select.appendChild(option);
    }
    list.appendChild(table);
  } catch (err) {
    if (stale(snap)) return;
    errorState(document.getElementById("desk-capabilities"),
      "Unable to load desktop capabilities", err, renderDesktop);
  }
  // Observation state.
  try {
    const statePayload = await api("/api/v1/desktop/state");
    if (stale(snap)) return;
    const box = document.getElementById("desk-state");
    box.innerHTML = "";
    const provider = (statePayload.provider || {});
    const providerPayload = provider.provider || {};
    const rows = [
      ["provider healthy", provider.healthy ? "yes" : "no"],
      ["simulation", providerPayload.simulation ? "yes" : "no"],
    ];
    const obs = statePayload.observations || {};
    const focus = obs.active_window && obs.active_window.observation;
    if (focus && focus.title) {
      rows.push(["active window", focus.title + " (" + focus.app + ")", true]);
    }
    const windows = obs.windows && obs.windows.observation;
    if (windows && windows.windows) {
      rows.push(["windows", windows.windows.length]);
    }
    const proc = obs.processes && obs.processes.observation;
    if (proc && proc.processes) {
      rows.push(["processes", proc.processes.length]);
    }
    box.appendChild(kvTable(rows));
  } catch (err) {
    if (stale(snap)) return;
    errorState(document.getElementById("desk-state"),
      "Unable to load desktop state", err, renderDesktop);
  }
  // Pending desktop approvals for this session.
  try {
    const approvals = await api("/api/v1/desktop/approvals");
    if (stale(snap)) return;
    const box = document.getElementById("desk-approvals");
    box.innerHTML = "";
    const items = approvals.approvals || [];
    if (!items.length) {
      const empty = el("p", "muted empty-state");
      empty.textContent = "No pending desktop approvals.";
      box.appendChild(empty);
    } else {
      for (const approval of items) {
        const card = el("div", "approval-card");
        const title = el("div", "section-header");
        title.appendChild(el("h4", null,
          (approval.operation || "desktop") + " → " +
          (approval.scopes || []).join(", ")));
        card.appendChild(title);
        card.appendChild(el("p", null, approval.reason || ""));
        card.appendChild(el("p", "muted",
          "risk: " + (approval.risk || "–") + " · approver must not be the requesting agent"));
        const actions = el("div", "row-actions");
        const approve = el("button", "btn small primary", "Approve");
        const deny = el("button", "btn small", "Deny");
        approve.addEventListener("click", async () => {
          try {
            const decision = await api("/api/v1/desktop/approvals/" +
              encodeURIComponent(approval.id) + "/approve",
              { method: "POST", body: {} });
            if (decision && decision.token_id) {
              state.deskToken = decision.token_id;
            }
            renderDesktop();
          } catch (failure) {
            errorState(box, "Approve failed", failure, renderDesktop);
          }
        });
        deny.addEventListener("click", async () => {
          try {
            await api("/api/v1/desktop/approvals/" +
              encodeURIComponent(approval.id) + "/deny",
              { method: "POST", body: {} });
            renderDesktop();
          } catch (failure) {
            errorState(box, "Deny failed", failure, renderDesktop);
          }
        });
        actions.appendChild(approve);
        actions.appendChild(deny);
        card.appendChild(actions);
        box.appendChild(card);
      }
    }
  } catch (err) {
    if (stale(snap)) return;
    errorState(document.getElementById("desk-approvals"),
      "Unable to load desktop approvals", err, renderDesktop);
  }
  // Action form (re-bound every render; the template is re-cloned each time).
  document.getElementById("desk-refresh").addEventListener("click", renderDesktop);
  document.getElementById("desk-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const resultBox = document.getElementById("desk-result");
    resultBox.innerHTML = "";
    resultBox.classList.add("hidden");
    let params = {};
    const paramsText = document.getElementById("desk-params").value.trim();
    if (paramsText) {
      try {
        params = JSON.parse(paramsText);
      } catch (err) {
        resultBox.classList.remove("hidden");
        resultBox.appendChild(el("p", "error", "Parameters must be valid JSON."));
        return;
      }
    }
    const body = {
      action: document.getElementById("desk-action").value,
      target: document.getElementById("desk-target").value.trim(),
      params: params,
      reason: document.getElementById("desk-reason").value.trim(),
    };
    if (state.deskToken) body.approval_id = state.deskToken;
    try {
      const result = await api("/api/v1/desktop/act",
        { method: "POST", body: body });
      if (result.executed) state.deskToken = null;
      deskResultBox(result);
      renderDesktop();
    } catch (err) {
      resultBox.classList.remove("hidden");
      errorState(resultBox, "Desktop action failed", err, renderDesktop);
    }
  });
}

/* ---------- command palette ---------- */

const PALETTE_COMMANDS = [
  ["Go to Overview", "view", () => { window.location.hash = "#/dashboard"; }],
  ["Go to Tasks", "view", () => { window.location.hash = "#/tasks"; }],
  ["Go to Projects", "view", () => { window.location.hash = "#/projects"; }],
  ["Go to Models", "view", () => { window.location.hash = "#/models"; }],
  ["Go to Permissions", "view", () => { window.location.hash = "#/permissions"; }],
  ["Go to Git", "view", () => { window.location.hash = "#/git"; }],
  ["Go to Activity", "view", () => { window.location.hash = "#/activity"; }],
  ["View approvals", "view", () => { window.location.hash = "#/approvals"; }],
  ["Go to Desktop", "view", () => { window.location.hash = "#/desktop"; }],
  ["Go to System", "view", () => { window.location.hash = "#/system"; }],
  ["Create task", "action", () => {
    window.location.hash = "#/tasks";
    setTimeout(() => {
      const input = document.getElementById("new-task-req");
      if (input) input.focus();
    }, 60);
  }],
];

function paletteMatches() {
  const needle = document.getElementById("palette-input").value.trim().toLowerCase();
  if (!needle) return PALETTE_COMMANDS.slice();
  return PALETTE_COMMANDS.filter(([label]) => label.toLowerCase().includes(needle));
}

function renderPaletteList() {
  const list = document.getElementById("palette-list");
  list.innerHTML = "";
  const items = paletteMatches();
  state.palette.items = items;
  if (state.palette.selected >= items.length) state.palette.selected = 0;
  if (!items.length) {
    list.appendChild(el("div", "muted", "No matching command."));
    return;
  }
  items.forEach(([label, kind], i) => {
    const btn = el("button", "palette-item" + (i === state.palette.selected ? " selected" : ""), label);
    btn.type = "button";
    btn.setAttribute("role", "option");
    btn.setAttribute("aria-selected", i === state.palette.selected ? "true" : "false");
    btn.appendChild(el("span", "pkind", kind));
    btn.addEventListener("click", () => {
      closePalette();
      items[i][2]();
    });
    btn.addEventListener("mousemove", () => {
      if (state.palette.selected !== i) {
        state.palette.selected = i;
        renderPaletteList();
      }
    });
    list.appendChild(btn);
  });
}

function openPalette() {
  state.palette.open = true;
  state.palette.selected = 0;
  document.getElementById("palette").classList.remove("hidden");
  const input = document.getElementById("palette-input");
  input.value = "";
  renderPaletteList();
  input.focus();
}

function closePalette() {
  state.palette.open = false;
  document.getElementById("palette").classList.add("hidden");
}

function initPalette() {
  document.getElementById("palette-open").addEventListener("click", openPalette);
  document.getElementById("palette").addEventListener("click", (ev) => {
    if (ev.target.id === "palette") closePalette();
  });
  const input = document.getElementById("palette-input");
  input.addEventListener("input", () => {
    state.palette.selected = 0;
    renderPaletteList();
  });
  input.addEventListener("keydown", (ev) => {
    const items = state.palette.items;
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      state.palette.selected = (state.palette.selected + 1) % Math.max(1, items.length);
      renderPaletteList();
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      state.palette.selected =
        (state.palette.selected - 1 + items.length) % Math.max(1, items.length);
      renderPaletteList();
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      const cmd = items[state.palette.selected];
      if (cmd) {
        closePalette();
        cmd[2]();
      }
    } else if (ev.key === "Escape") {
      closePalette();
    }
  });
  document.addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "k") {
      ev.preventDefault();
      if (state.palette.open) closePalette();
      else openPalette();
    } else if (ev.key === "Escape" && state.palette.open) {
      closePalette();
    }
  });
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
  if (state.entered) return;
  state.entered = true;
  document.getElementById("login").classList.add("hidden");
  document.getElementById("app").classList.remove("hidden");
  document.getElementById("logout").classList.remove("hidden");
  document.getElementById("project-chip").textContent = state.session.project_id;
  document.getElementById("whoami").textContent =
    `${state.session.actor} @ ${state.session.project_id}`;
  document.getElementById("profile-chip").textContent = state.session.profile;
  document.getElementById("logout").addEventListener("click", async () => {
    try {
      await api("/api/v1/sessions/me", { method: "DELETE" });
    } catch (err) { /* ignore */ }
    window.location.hash = "#/dashboard";
    window.location.reload();
  });
  const toggle = document.getElementById("menu-toggle");
  const scrim = document.getElementById("scrim");
  toggle.addEventListener("click", () => {
    const open = !document.body.classList.contains("nav-open");
    document.body.classList.toggle("nav-open", open);
    scrim.classList.toggle("hidden", !open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  });
  scrim.addEventListener("click", () => {
    document.body.classList.remove("nav-open");
    scrim.classList.add("hidden");
    toggle.setAttribute("aria-expanded", "false");
  });
  initPalette();
  window.addEventListener("hashchange", route);
  route();
}

document.addEventListener("DOMContentLoaded", bootstrap);
