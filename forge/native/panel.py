"""Native AI status panel rendering (A81 layer 16) — toolkit-free.

The desktop app (``forge.desktop_app.app``) and any future UI render the
engine status through :func:`format_native_status`, a pure text builder with
no tkinter import — so the exact panel content is unit-testable on headless
machines (including the Windows 7 client, where Tk may be absent).

The panel shows precisely the fields A81 requires: engine state, active
reasoning backend, model backend, task state, current stage, verification
state, and retry state — plus the free-vs-neural capability labels. Unknown
is always shown as unknown; a historical snapshot is labeled as historical,
never presented as live.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def format_native_status(native: Optional[Dict[str, Any]],
                         error: str = "") -> str:
    """Render the panel text for one project's Native AI status payload."""
    if error:
        return "Native AI status unavailable: %s" % error
    if not native:
        return "Native AI: no status received for this project."
    persisted = dict(native.get("persisted") or {})
    model = dict(native.get("model_backend") or {})
    lines: List[str] = ["Forge Native AI Engine — "
                        + str(native.get("root", ""))]
    if not persisted.get("available"):
        lines.append("")
        lines.append("  " + str(persisted.get("reason", "no snapshot yet")))
        lines.append("  Start one:  forge native-ai \"<task>\" --root .")
        return "\n".join(lines)
    snap = dict(persisted.get("snapshot") or {})
    task = dict(snap.get("task") or {})
    stage = dict(snap.get("stage") or {})
    verification = dict(snap.get("verification") or {})
    retry = dict(snap.get("retry") or {})
    reasoning = dict(snap.get("reasoning_backend") or {})
    failed = verification.get("failed") or []
    lines += [
        "  engine state:   %s" % snap.get("engine_state", "-"),
        "  task:           %s — %s" % (task.get("state", "none"),
                                       (task.get("text") or "")[:70]),
        "  current stage:  %s (%s/%s)" % (stage.get("kind") or "-",
                                          stage.get("index", 0),
                                          stage.get("total", 0)),
        "  reasoning:      structural=%s generative=%s" % (
            reasoning.get("name", "-"),
            reasoning.get("generative_backend")
            or "none (NEURAL_REQUIRED)"),
        "  model backend:  " + (
            "registered: %s (%s)" % (", ".join(model.get("models", [])),
                                     model.get("live", "unverified"))
            if model.get("real_model") else model.get("detail", "-")),
        "  verification:   %s%s" % (verification.get("status", "PENDING"),
                                    "  failed: " + ", ".join(failed)
                                    if failed else ""),
        "  retry state:    %s/%s%s" % (retry.get("cycle", 0),
                                       retry.get("max", 0),
                                       "  last: " + str(
                                           retry.get("last_reason") or "-")),
        "  files changed:  %s" % (", ".join(snap.get("files_changed", []))
                                  or "-"),
        "  snapshot age:   %ss%s" % (persisted.get("age_seconds", "?"),
                                     "" if persisted.get("fresh")
                                     else " (historical)"),
        "",
        "  Capabilities [free] run with no neural model; [model] need one:",
    ]
    for capability in native.get("capabilities", []):
        mark = "model" if capability.get("requires_neural") else "free "
        label = capability.get("label", capability.get("id", ""))
        lines.append("    [%s] %s" % (mark, str(label)[:76]))
    return "\n".join(lines)
