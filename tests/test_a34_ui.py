"""Browser cockpit UI contract tests (A34 UI upgrade).

The UI may evolve visually, but its contracts must not drift: required DOM
hooks, navigation backed by real templates/routes, no credentials or ambient
authority, CSP-compatible markup (no inline handlers/styles), navigation-only
command palette, full status coverage, and live backend endpoints behind
every view.
"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import login, make_client, make_plane  # noqa: E402

WEB = Path(__file__).parent.parent / "forge" / "cockpit" / "web"

REQUIRED_HOOKS = """login login-form login-actor login-project login-profile
login-error nav conn whoami logout view new-task-form new-task-req
new-task-error task-list t-title t-meta t-actions t-error t-timeline
t-details t-verification t-checkpoints t-report t-stream-state t-events
p-list p-current m-list m-providers m-routing pm-effective pm-approvals
pm-rules g-state g-diff g-diff-staged""".split()

NEW_VIEW_HOOKS = ["palette", "palette-input", "palette-list", "act-list",
                  "ap-list", "sys-backend", "sys-workers", "sys-session",
                  "sys-security", "task-filters", "task-search", "d-stats",
                  "d-active", "d-activity", "d-security", "t-badges"]

STATUSES = ["QUEUED", "RUNNING", "PAUSED", "WAITING_APPROVAL", "SUCCEEDED",
            "FAILED", "CANCELLED", "ROLLED_BACK"]

EVENT_TYPES = """task.created task.queued task.started task.paused
task.resumed task.cancel_requested task.cancelled task.completed task.failed
stage.started run.started agent.selected model.selected change.proposed
permission.checked changes.applied tests.executed tests.failed
repair.attempted review.completed security.completed benchmark.completed
acceptance.completed git.commit checkpoint.created rollback.requested
rollback.completed approval.required approval.approved approval.denied
approval.expired""".split()

DESIGN_CLASSES = ["surface", "stat", "status-dot", "status-badge", "pipeline",
                  "pipeline-node", "activity-item", "approval-card", "metric",
                  "data-table", "empty-state", "command-bar", "section-header"]


def _read(name: str) -> str:
    return (WEB / name).read_text()


def test_ui_required_dom_hooks_present():
    html = _read("index.html")
    for hook in REQUIRED_HOOKS + NEW_VIEW_HOOKS:
        assert f'id="{hook}"' in html, hook


def test_ui_nav_routes_have_templates_and_renderers():
    html = _read("index.html")
    js = _read("app.js")
    routes = set(re.findall(r'data-route="(\w+)"', html))
    templates = set(re.findall(r'<template id="tpl-(\w+)"', html))
    assert routes, "no nav routes found"
    assert routes <= templates, routes - templates
    for route in routes:
        assert re.search(rf"\b{route}:\s*\{{\s*render:", js), route


def test_ui_no_fake_pages():
    """Every hash route the UI links to must be a real ROUTES entry."""
    html = _read("index.html")
    js = _read("app.js")
    linked = set(re.findall(r'href="#/(\w+)"', html))
    linked |= set(re.findall(r'location\.hash = "#/(\w+)', js))
    routes_block = js.split("const ROUTES", 1)[1].split("};", 1)[0]
    defined = set(re.findall(r"^\s*(\w+):\s*\{\s*render:", routes_block,
                             re.MULTILINE))
    assert linked <= defined, linked - defined


def test_ui_markup_has_unique_ids_and_balanced_tags():
    seen: set[str] = set()
    dupes: set[str] = set()

    class Checker(HTMLParser):
        VOID = {"meta", "link", "input", "br", "img", "circle", "rect",
                "path", "hr"}

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack: list[str] = []
            self.errors: list[str] = []

        def handle_starttag(self, tag, attrs):
            for key, value in attrs:
                if key == "id":
                    if value in seen:
                        dupes.add(value)
                    seen.add(value)
            if tag not in self.VOID and tag not in (
                    "circle", "rect", "path"):
                self.stack.append(tag)

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)
            if self.stack and self.stack[-1] == tag:
                self.stack.pop()

        def handle_endtag(self, tag):
            if tag in self.VOID:
                return
            if self.stack and self.stack[-1] == tag:
                self.stack.pop()
            else:
                self.errors.append(f"mismatch: </{tag}>")

    checker = Checker()
    checker.feed(_read("index.html"))
    assert not dupes, dupes
    assert not checker.errors, checker.errors[:5]
    assert not checker.stack, checker.stack


def test_ui_no_inline_handlers_styles_or_scripts():
    """CSP compatibility: script-src/style-src 'self' only."""
    html = _read("index.html")
    assert re.search(r"\son\w+\s*=", html) is None
    assert "style=" not in html
    assert "<style" not in html
    assert "javascript:" not in html
    for match in re.finditer(r"<script([^>]*)>", html):
        assert 'src="' in match.group(1), match.group(0)
    js = _read("app.js")
    assert 'createElement("style")' not in js
    assert "createElement('style')" not in js
    assert "<style" not in js
    assert ".style" not in js
    assert "cssText" not in js
    assert 'setAttribute("style"' not in js


def test_ui_no_storage_ambient_authority_or_external_calls():
    blob = _read("index.html") + _read("app.js") + _read("styles.css")
    lowered = blob.lower()
    for pattern in [r"localstorage", r"sessionstorage", r"\beval\s*\(",
                    r"new\s+websocket", r"https?://", r"localhost",
                    r"127\.0\.0\.1", r"api[_-]?key", r"apikey",
                    r"secret\s*[:=]", r"document\.write"]:
        assert re.search(pattern, lowered) is None, pattern


def test_ui_single_fetch_helper_same_origin_only():
    js = _read("app.js")
    assert js.count("fetch(") == 1
    calls = re.findall(r"\bapi\(\s*[`\"']([^`\"']+)", js)
    assert calls, "no api() call sites found"
    for call in calls:
        assert call.startswith("/api/"), call
    assert "new EventSource(url)" in js
    assert re.search(r"const url = `/api/v1/tasks/", js)


def test_ui_csrf_header_on_mutations():
    js = _read("app.js")
    assert "X-Requested-With" in js
    assert "forge-cockpit" in js


def test_ui_command_palette_navigation_only():
    js = _read("app.js")
    block = js.split("const PALETTE_COMMANDS", 1)[1].split("];", 1)[0]
    assert "api(" not in block
    assert "POST" not in block
    assert block.count("location.hash") >= 9
    for label in ["Go to Overview", "Go to Tasks", "Go to Projects",
                  "Go to Models", "Go to Permissions", "Go to Git",
                  "View approvals", "Create task"]:
        assert label in block, label
    assert "palette-input" in js and "palette-list" in js


def test_ui_approval_cards_keep_full_context():
    js = _read("app.js")
    for field in ["approval.reason", "approval.risk", "approval.operation",
                  "approval.resource", "approval.agent", "approval.scopes",
                  "approval.expires_at", "approval.model",
                  "approval.provider"]:
        assert field in js, field
    assert "/approvals/${encodeURIComponent(id)}/${allow" in js
    css = _read("styles.css")
    assert ".approval-card" in css


def test_ui_task_creation_contract():
    js = _read("app.js")
    assert 'getElementById("new-task-form")' in js
    assert 'getElementById("new-task-req")' in js
    assert 'getElementById("new-task-error")' in js
    assert re.search(r'api\("/api/v1/tasks",\s*\{\s*method:\s*"POST",\s*'
                     r"body:\s*\{\s*requirement:", js)


def test_ui_status_rendering_covers_all_statuses():
    js = _read("app.js")
    tone_block = js.split("function statusTone", 1)[1].split("}", 1)[0]
    for status in STATUSES:
        assert status in tone_block, status
    assert "status-badge" in js and "status-dot" in js
    # Filters map every status somewhere (waiting/cancelled group aliases).
    filter_block = js.split("const TASK_FILTERS", 1)[1].split("];", 1)[0]
    for status in STATUSES:
        assert status in filter_block, status


def test_ui_event_catalog_has_human_readers():
    js = _read("app.js")
    assert "const EVENT_TEXT" in js
    for event_type in EVENT_TYPES:
        assert f'"{event_type}"' in js, event_type
    assert "activity-item" in js and "activity-title" in js
    # Raw payload stays available behind an expander, not as the UI.
    assert '"raw event"' in js


def test_ui_design_system_classes_defined():
    css = _read("styles.css")
    for cls in DESIGN_CLASSES:
        assert f".{cls}" in css, cls
    for var in ["--bg0", "--bg1", "--text", "--muted", "--accent", "--ok",
                "--warn", "--bad", "--mono"]:
        assert var in css, var
    used = set(re.findall(r"var\((--[\w-]+)\)", css))
    defined = set(re.findall(r"(--[\w-]+)\s*:", css))
    assert used <= defined, used - defined


def test_ui_responsive_and_motion_rules():
    css = _read("styles.css")
    assert "@media (max-width: 1100px)" in css
    assert "@media (max-width: 900px)" in css
    assert "@media (max-width: 760px)" in css
    assert "prefers-reduced-motion" in css
    assert ":focus-visible" in css
    assert ".nav-open" in css


def test_ui_views_backed_by_real_endpoints(tmp_path):
    plane = make_plane(tmp_path)
    client = make_client(plane)
    with client:
        _, _, headers = login(client)
        approvals = client.get("/api/v1/approvals", headers=headers)
        assert approvals.status_code == 200
        assert "approvals" in approvals.json()
        running = client.get("/api/v1/tasks?status=RUNNING&limit=1",
                             headers=headers)
        assert running.status_code == 200
        assert "tasks" in running.json()
        waiting = client.get(
            "/api/v1/tasks?status=WAITING_APPROVAL&limit=1",
            headers=headers)
        assert waiting.status_code == 200
        dashboard = client.get("/api/v1/dashboard", headers=headers)
        body = dashboard.json()
        for key in ("tasks", "models", "workers", "recent_activity",
                    "approvals_waiting"):
            assert key in body, key
        for key in ("queued", "running", "waiting_approval", "failed"):
            assert key in body["tasks"], key
        health = client.get("/api/v1/health", headers=headers)
        assert health.json()["status"] == "ok"
        assert "auth_mode" in health.json()
