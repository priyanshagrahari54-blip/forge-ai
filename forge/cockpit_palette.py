"""Command palette catalog (A67): one canonical source for the cockpit.

The palette lists cockpit views and safe quick actions. Targets are
hash routes only — never commands, so palette navigation carries no
execution power by itself.
"""
from __future__ import annotations

import re
from typing import Any

VIEWS: tuple[dict[str, str], ...] = (
    {"label": "Go to Overview", "target": "dashboard"},
    {"label": "Go to Tasks", "target": "tasks"},
    {"label": "Go to Projects", "target": "projects"},
    {"label": "Go to Models", "target": "models"},
    {"label": "Go to Permissions", "target": "permissions"},
    {"label": "Go to Git", "target": "git"},
    {"label": "Go to Activity", "target": "activity"},
    {"label": "Go to Approvals", "target": "approvals"},
    {"label": "Go to Desktop", "target": "desktop"},
    {"label": "Go to Voice", "target": "voice"},
    {"label": "Go to Memory", "target": "memory"},
    {"label": "Go to Orchestrations", "target": "orchestrations"},
    {"label": "Go to Vision", "target": "vision"},
    {"label": "Go to Computer Use", "target": "computer"},
    {"label": "Go to Agents", "target": "agents"},
    {"label": "Go to Security", "target": "security"},
    {"label": "Go to Settings", "target": "settings"},
    {"label": "Go to Conversation", "target": "conversation"},
    {"label": "Go to Research", "target": "research"},
    {"label": "Go to Self-Improvement", "target": "selfimprove"},
    {"label": "Go to Compute", "target": "compute"},
    {"label": "Go to Agent Builder", "target": "agentbuilder"},
    {"label": "Go to System", "target": "system"},
)

ACTIONS: tuple[dict[str, str], ...] = (
    {"label": "Create task", "target": "tasks", "hint": "focus new-task"},
    {"label": "View approvals", "target": "approvals",
     "hint": "pending decisions"},
    {"label": "Refresh current view", "target": "", "hint": "client-only"},
)

_ROUTE = re.compile(r"^[a-z][a-z0-9]*$")


def palette_entries() -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for view in VIEWS:
        entries.append({"label": view["label"], "kind": "view",
                        "target": view["target"]})
    for action in ACTIONS:
        entries.append({"label": action["label"], "kind": "action",
                        "target": action["target"],
                        "hint": action.get("hint", "")})
    return entries


def palette_payload() -> dict[str, Any]:
    entries = palette_entries()
    return {"entries": entries,
            "count": len(entries),
            "note": "Targets are cockpit hash routes only; navigation "
                    "carries no execution power."}


def validate_entries(entries: list[dict[str, str]]) -> None:
    """Defense in depth for the catalog itself."""
    for entry in entries:
        target = entry.get("target", "")
        if entry.get("kind") == "view" and not _ROUTE.match(target):
            raise ValueError(f"bad palette target {target!r}")
        if entry.get("kind") not in ("view", "action"):
            raise ValueError(
                f"bad palette kind {entry.get('kind')!r}")
