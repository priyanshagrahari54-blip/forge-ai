"""Structured desktop action vocabulary and request validation (A35).

Every desktop capability is expressed as one :class:`DesktopActionKind`
with validated parameters carried in :class:`DesktopRequest`. Unknown or
malformed requests fail closed at validation, before any policy or
execution layer is consulted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: A33 policy operations each action kind maps onto. Every action is
#: authorized through the A33 :class:`PermissionPolicy` using exactly
#: these operations — the Desktop Agent adds no second permission system.
POLICY_OPERATIONS: dict[str, str] = {
    "screenshot": "screenshot",
    "read_screen": "read_screen",
    "window_list": "window",
    "window": "window",
    "mouse_move": "mouse_move",
    "mouse_click": "mouse_click",
    "keyboard": "keyboard",
    "launch": "launch",
    "file_select": "file_access",
    "file_access": "file_access",
    "clipboard": "clipboard",
    "app_action": "launch",
    "process_list": "process",
    "process": "process",
    "system_info": "system_info",
}

#: Observation-only kinds: they mutate nothing on the target desktop.
OBSERVATION_KINDS = frozenset({
    "screenshot", "read_screen", "window_list", "process_list",
    "process", "system_info", "file_select",
})

#: Kinds that can change desktop state.
ACTUATION_KINDS = frozenset({
    "window", "mouse_move", "mouse_click", "keyboard", "launch",
    "file_access", "clipboard", "app_action",
})

MAX_TARGET_LEN = 300
MAX_KEYBOARD_TEXT = 2000
MAX_CLIPBOARD_CONTENT = 10_000
MAX_LAUNCH_ARGS = 16
MAX_COORDINATE = 20_000
_MAX_DEPTH = 6  # params nesting depth bound


class DesktopActionKind(str, Enum):
    SCREENSHOT = "screenshot"
    READ_SCREEN = "read_screen"
    WINDOW_LIST = "window_list"
    WINDOW = "window"
    MOUSE_MOVE = "mouse_move"
    MOUSE_CLICK = "mouse_click"
    KEYBOARD = "keyboard"
    LAUNCH = "launch"
    FILE_SELECT = "file_select"
    FILE_ACCESS = "file_access"
    CLIPBOARD = "clipboard"
    APP_ACTION = "app_action"
    PROCESS_LIST = "process_list"
    PROCESS = "process"
    SYSTEM_INFO = "system_info"


class ValidationError(ValueError):
    """A desktop request failed structural validation (fail closed)."""


@dataclass(frozen=True)
class DesktopRequest:
    """WHAT on WHERE, by WHOM, WHY, under WHICH task/session.

    ``action``     : one :class:`DesktopActionKind`
    ``target``     : what the action operates on (window title, app name,
                     process name, path, …)
    ``params``     : validated, JSON-serializable action parameters
    ``agent``      : the acting agent identity (required)
    ``task_id``    : the task this action serves (empty = interactive)
    ``session_id`` : the cockpit/bridge session binding for approvals
                     (tokens are non-transferable to other sessions)
    ``reason``     : human-readable justification (shown on approvals)
    """

    action: DesktopActionKind | str
    target: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    agent: str = ""
    task_id: str = ""
    session_id: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", DesktopActionKind(self.action))

    @property
    def approval_task_id(self) -> str:
        """The task binding used for approval filing/redemption."""
        return self.task_id or self.session_id

    @property
    def kind(self) -> DesktopActionKind:
        return self.action  # type: ignore[return-value]

    def policy_operation(self) -> str:
        """The A33 policy operation this action is authorized under."""
        return POLICY_OPERATIONS[self.kind.value]

    def is_observation(self) -> bool:
        return self.kind.value in OBSERVATION_KINDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.kind.value,
            "target": self.target,
            "params": dict(self.params),
            "agent": self.agent,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Structural validation
# ---------------------------------------------------------------------------

def _valid_target(target: str) -> tuple[bool, str]:
    if not isinstance(target, str) or len(target) > MAX_TARGET_LEN:
        return False, f"target must be a string of at most {MAX_TARGET_LEN} characters"
    if any(ord(ch) < 32 and ch not in "\t" for ch in target):
        return False, "target contains control characters"
    return True, ""


def _bounded_int(value: Any, *, low: int, high: int,
                 name: str) -> tuple[int | None, str]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None, f"{name} must be an integer"
    if not (low <= value <= high):
        return None, f"{name} must be within {low}..{high}"
    return value, ""


def _bounded_text(value: Any, *, limit: int, name: str,
                  allow_empty: bool = False) -> tuple[str | None, str]:
    if not isinstance(value, str):
        return None, f"{name} must be a string"
    if not allow_empty and not value:
        return None, f"{name} must not be empty"
    if len(value) > limit:
        return None, f"{name} exceeds {limit} characters"
    if any(ord(ch) < 32 and ch not in "\t\n" for ch in value):
        return None, f"{name} contains control characters"
    return value, ""


def _params_ok(params: Any) -> tuple[bool, str]:
    if not isinstance(params, dict):
        return False, "params must be an object"
    todo = [(params, 0)]
    while todo:
        node, depth = todo.pop()
        if depth > _MAX_DEPTH:
            return False, "params nesting too deep"
        if isinstance(node, dict):
            for key in node:
                if not isinstance(key, str) or len(key) > 100:
                    return False, "param keys must be short strings"
            todo.extend((value, depth + 1) for value in node.values())
        elif isinstance(node, list):
            if len(node) > 64:
                return False, "param list too long"
            todo.extend((item, depth + 1) for item in node)
        elif isinstance(node, (str, int, float, bool)) or node is None:
            continue
        else:
            return False, "params contain an unsupported value type"
    return True, ""


def _validate_window(params: dict, target: str = "") -> tuple[bool, str]:
    op = params.get("op")
    if op not in ("focus", "minimize", "maximize", "restore", "close",
                  "move", "resize"):
        return False, "window op must be one of focus/minimize/maximize/"
        "restore/close/move/resize"
    for key in ("x", "y", "w", "h"):
        if key in params:
            value, err = _bounded_int(params[key], low=0, high=MAX_COORDINATE,
                                      name=f"window {key}")
            if err:
                return False, err
    return True, ""


def _validate_keyboard(params: dict, target: str = "") -> tuple[bool, str]:
    text = params.get("text")
    keys = params.get("keys")
    if text is None and keys is None:
        return False, "keyboard requires text or keys"
    if text is not None:
        _, err = _bounded_text(text, limit=MAX_KEYBOARD_TEXT, name="text",
                               allow_empty=True)
        if err:
            return False, err
    if keys is not None:
        if not isinstance(keys, list) or not keys or len(keys) > 32:
            return False, "keys must be a non-empty list of at most 32 names"
        for key in keys:
            if not isinstance(key, str) or not key or len(key) > 64 \
                    or any(ord(ch) < 32 for ch in key):
                return False, "key names must be short printable strings"
    return True, ""


def _validate_launch(params: dict, target: str = "") -> tuple[bool, str]:
    args = params.get("args", [])
    if not isinstance(args, list) or len(args) > MAX_LAUNCH_ARGS:
        return False, f"launch args must be a list of at most {MAX_LAUNCH_ARGS}"
    for arg in args:
        if not isinstance(arg, str) or not arg or len(arg) > 300:
            return False, "launch args must be short non-empty strings"
        if any(token in arg for token in (";", "&&", "||", "|", "`", "$(",
                                          "\n", "\r")):
            return False, "launch args contain shell metacharacters"
    return True, ""


def _validate_clipboard(params: dict, target: str = "") -> tuple[bool, str]:
    op = params.get("op")
    if op not in ("read", "write"):
        return False, "clipboard op must be read or write"
    if op == "write":
        content, err = _bounded_text(
            params.get("content"), limit=MAX_CLIPBOARD_CONTENT,
            name="content", allow_empty=True)
        if err:
            return False, err
    return True, ""


def _validate_file_access(params: dict, target: str = "") -> tuple[bool, str]:
    mode = params.get("mode")
    if mode not in ("read", "write", "delete"):
        return False, "file_access mode must be read, write, or delete"
    path = params.get("path") or target
    if not isinstance(path, str) or not path or len(path) > 500:
        return False, "file_access path must be a short non-empty string"
    if ".." in path.split("/") or ".." in path.split("\\") \
            or path.startswith("/") or re_abs(path):
        return False, "file_access path must be relative"
    if mode == "write":
        content = params.get("content")
        if not isinstance(content, str) or len(content) > 1_000_000:
            return False, "file_access write content must be a bounded string"
    return True, ""


def re_abs(path: str) -> bool:
    import re
    return bool(re.match(r"^[A-Za-z]:[\\/]", path))


def _validate_mouse(params: dict, target: str = "") -> tuple[bool, str]:
    for key, name in (("x", "x"), ("y", "y")):
        value, err = _bounded_int(params.get(key), low=0, high=MAX_COORDINATE,
                                  name=name)
        if err:
            return False, err
    return True, ""


def _validate_click(params: dict, target: str = "") -> tuple[bool, str]:
    ok, err = _validate_mouse(params)
    if not ok:
        return False, err
    button = params.get("button", "left")
    if button not in ("left", "right", "middle"):
        return False, "click button must be left, right, or middle"
    if "double" in params and not isinstance(params["double"], bool):
        return False, "click double must be a boolean"
    return True, ""





def _validate_app_action(params: dict, target: str = "") -> tuple[bool, str]:
    action = params.get("action")
    _, err = _bounded_text(action or "", limit=100, name="action")
    if err or not action:
        return False, "app_action requires a short action name"
    return True, ""


def _validate_process(params: dict, target: str = "") -> tuple[bool, str]:
    if "pid" not in params:
        return True, ""  # name-based lookup (target carries the name)
    return _bounded_int(params["pid"], low=1, high=1 << 30, name="pid")


def _validate_file_select(params: dict, target: str = "") -> tuple[bool, str]:
    pattern = params.get("pattern", "")
    _, err = _bounded_text(pattern, limit=300, name="pattern",
                           allow_empty=True)
    return (False, err) if err else (True, "")


def validate_request(request: DesktopRequest) -> tuple[bool, str]:
    """Structurally validate a desktop request. Fail closed on any doubt."""
    if not request.agent or not isinstance(request.agent, str) \
            or len(request.agent) > 128:
        return False, "request has no valid agent identity"
    ok, err = _valid_target(request.target)
    if not ok:
        return False, err
    if len(request.reason) > 500:
        return False, "reason is too long"
    ok, err = _params_ok(request.params)
    if not ok:
        return False, err
    validator = _VALIDATORS.get(request.kind.value)
    if validator is not None:
        return validator(request.params, request.target)
    return True, ""


_VALIDATORS: dict[str, Any] = {
    "window": _validate_window,
    "mouse_move": _validate_mouse,
    "mouse_click": _validate_click,
    "keyboard": _validate_keyboard,
    "launch": _validate_launch,
    "clipboard": _validate_clipboard,
    "file_access": _validate_file_access,
    "app_action": _validate_app_action,
    "process": _validate_process,
    "file_select": _validate_file_select,
}
