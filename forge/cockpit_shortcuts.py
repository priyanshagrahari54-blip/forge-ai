"""Cockpit shortcuts (A68): canonical keyboard navigation catalog.

Shortcut targets are hash routes (or pure-UI actions like the help
overlay and the palette) — navigation only, no execution power.
"""
from __future__ import annotations

import re
from typing import Any

from forge.cockpit_palette import VIEWS, validate_entries

_CHORD = re.compile(r"^[a-z0-9+? ]{1,16}$")

# Prefix-chord navigation: press the prefix, then the key.
CHORDS: tuple[dict[str, str], ...] = (
    {"keys": "g d", "label": "Go to Overview", "target": "dashboard"},
    {"keys": "g t", "label": "Go to Tasks", "target": "tasks"},
    {"keys": "g p", "label": "Go to Projects", "target": "projects"},
    {"keys": "g m", "label": "Go to Models", "target": "models"},
    {"keys": "g a", "label": "Go to Approvals", "target": "approvals"},
    {"keys": "g v", "label": "Go to Conversation", "target": "conversation"},
    {"keys": "g s", "label": "Go to Settings", "target": "settings"},
    {"keys": "g b", "label": "Go to Agent Builder", "target": "agentbuilder"},
)

# Immediate keys (no chord).
KEYS: tuple[dict[str, str], ...] = (
    {"keys": "ctrl+k", "label": "Open command palette", "target": "",
     "hint": "client-only"},
    {"keys": "?", "label": "Toggle shortcuts help", "target": "",
     "hint": "client-only"},
    {"keys": "n", "label": "Focus the new-task box", "target": "tasks",
     "hint": "focus new-task"},
    {"keys": "escape", "label": "Close overlays", "target": "",
     "hint": "client-only"},
)


def shortcut_entries() -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for chord in CHORDS:
        entries.append({"keys": chord["keys"], "label": chord["label"],
                        "kind": "chord", "target": chord["target"]})
    for key in KEYS:
        entries.append({"keys": key["keys"], "label": key["label"],
                        "kind": "key", "target": key["target"],
                        "hint": key.get("hint", "")})
    return entries


def shortcut_payload() -> dict[str, Any]:
    entries = shortcut_entries()
    return {"entries": entries, "count": len(entries),
            "note": "Shortcuts navigate cockpit views or toggle UI; they "
                    "never execute anything."}


def validate_shortcuts(entries: list[dict[str, str]]) -> None:
    seen: set[str] = set()
    view_targets = {entry["target"] for entry in
                    [{"target": view["target"]} for view in VIEWS]}
    for entry in entries:
        keys = entry.get("keys", "")
        if not _CHORD.match(keys):
            raise ValueError(f"bad shortcut keys {keys!r}")
        if keys in seen:
            raise ValueError(f"duplicate shortcut {keys!r}")
        seen.add(keys)
        if entry.get("kind") not in ("chord", "key"):
            raise ValueError(f"bad shortcut kind {entry.get('kind')!r}")
        target = entry.get("target", "")
        if entry["kind"] == "chord":
            if target not in view_targets:
                raise ValueError(f"chord target {target!r} is not a view")
