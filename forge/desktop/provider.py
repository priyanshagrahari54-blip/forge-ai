"""Desktop provider protocol and a deterministic fake desktop (A35).

:class:`DesktopProvider` is the backend-agnostic boundary every real
desktop bridge must implement (platform integration, remote agent
service, or another plugin). :class:`FakeDesktopProvider` is a fully
deterministic, scriptable in-memory desktop *simulation* used by tests,
local development, and the sandboxed computer-use E2E environment. It is
honest about being a simulation — it never claims to control a real
machine, and it contains no credential material.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Protocol, runtime_checkable


class DesktopProviderError(Exception):
    """The desktop provider could not serve the request."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@runtime_checkable
class DesktopProvider(Protocol):
    """Backend-agnostic desktop capability surface.

    Every method returns a JSON-serializable observation dict. Providers
    must never embed credentials or secrets in observations.
    """

    def healthy(self) -> bool: ...

    def screenshot(self) -> dict[str, Any]: ...

    def read_screen(self) -> dict[str, Any]: ...

    def list_windows(self) -> dict[str, Any]: ...

    def window(self, target: str, params: dict[str, Any]) -> dict[str, Any]: ...

    def mouse_move(self, target: str, params: dict[str, Any]) -> dict[str, Any]: ...

    def mouse_click(self, target: str, params: dict[str, Any]) -> dict[str, Any]: ...

    def keyboard(self, target: str, params: dict[str, Any]) -> dict[str, Any]: ...

    def launch(self, target: str, params: dict[str, Any]) -> dict[str, Any]: ...

    def file_select(self, target: str, params: dict[str, Any]) -> dict[str, Any]: ...

    def file_access(self, target: str, params: dict[str, Any]) -> dict[str, Any]: ...

    def clipboard(self, target: str, params: dict[str, Any]) -> dict[str, Any]: ...

    def app_action(self, target: str, params: dict[str, Any]) -> dict[str, Any]: ...

    def list_processes(self) -> dict[str, Any]: ...

    def process(self, target: str, params: dict[str, Any]) -> dict[str, Any]: ...

    def system_info(self) -> dict[str, Any]: ...

    def snapshot(self) -> dict[str, Any]: ...


class FakeDesktopProvider:
    """Deterministic, scriptable desktop simulation (MOCK/TEST ONLY).

    State modeled: screen size, windows (title/app/bounds/focus), an
    applications registry, launched processes, a clipboard, a bounded
    desktop-files area, an input event log, and fixed system facts.
    All mutations are deterministic and observable. ``fail_next`` /
    ``disconnect`` exist to exercise the agent's failure/recovery paths.
    """

    name = "fake"
    simulation = True

    def __init__(self, **kwargs: Any) -> None:
        self.screen_width = 1920
        self.screen_height = 1080
        self.desktop_root = "/home/user/Desktop"
        self.apps: dict[str, str] = {
            "notepad": "/usr/bin/notepad",
            "calculator": "/usr/bin/calculator",
            "browser": "/usr/bin/browser",
        }
        for key, value in kwargs.items():
            if key in ("screen_width", "screen_height", "desktop_root",
                       "apps"):
                setattr(self, key, value)
        self._lock = threading.RLock()
        self._connected = True
        self._fail_next: list[str] = []
        self._fail_all = False
        self.clipboard_text = ""
        self.input_log: list[dict[str, Any]] = []
        self.processes: list[dict[str, Any]] = [
            {"pid": 1, "name": "init", "user": "root"},
            {"pid": 4242, "name": "desktop-agent", "user": "user"},
        ]
        self._next_pid = 5000
        self.windows: dict[str, dict[str, Any]] = {
            "desktop": {"title": "Desktop", "app": "shell",
                        "x": 0, "y": 0, "w": self.screen_width,
                        "h": self.screen_height, "visible": True,
                        "focused": False},
        }
        self.focused = "desktop"
        self.files: dict[str, str] = {
            "readme.txt": "Welcome to the fake desktop.\n",
            "notes/report.txt": "Q3 report draft\n",
        }

    # -- health / lifecycle ------------------------------------------------

    def healthy(self) -> bool:
        return self._connected

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False

    def reconnect(self) -> None:
        with self._lock:
            self._connected = True

    def fail_next(self, action: str) -> None:
        with self._lock:
            self._fail_next.append(action)

    def fail_all(self, *, enabled: bool = True) -> None:
        with self._lock:
            self._fail_all = enabled

    # -- observations -------------------------------------------------------

    def screenshot(self) -> dict[str, Any]:
        with self._lock:
            self._require_connected("screenshot")
            rows = []
            focused = self.windows[self.focused]
            rows.append(f"FOCUS:{focused['title']} APP:{focused['app']}")
            rows.append(f"SCREEN:{self.screen_width}x{self.screen_height}")
            for name, win in sorted(self.windows.items()):
                if win["visible"] and name != self.focused:
                    rows.append(f"WINDOW:{name} TITLE:{win['title']}")
            return {"ok": True, "action": "screenshot",
                    "focused": self.focused, "rows": rows,
                    "captured_at": time.time()}

    def read_screen(self) -> dict[str, Any]:
        with self._lock:
            self._require_connected("read_screen")
            win = self.windows[self.focused]
            return {"ok": True, "action": "read_screen",
                    "active_window": self.focused,
                    "title": win["title"], "app": win["app"],
                    "bounds": {"x": win["x"], "y": win["y"],
                               "w": win["w"], "h": win["h"]},
                    "window_count": len(self.windows)}

    def list_windows(self) -> dict[str, Any]:
        with self._lock:
            self._require_connected("window_list")
            return {"ok": True, "action": "window_list",
                    "focused": self.focused,
                    "windows": [
                        {"name": name, **{k: v for k, v in win.items()
                                          if k in ("title", "app", "visible")}}
                        for name, win in sorted(self.windows.items())
                    ]}

    def list_processes(self) -> dict[str, Any]:
        with self._lock:
            self._require_connected("process_list")
            return {"ok": True, "action": "process_list",
                    "processes": [dict(p) for p in self.processes]}

    def process(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require_connected("process")
            pid = params.get("pid")
            for proc in self.processes:
                if pid is not None and proc["pid"] == pid:
                    return {"ok": True, "action": "process",
                            "process": dict(proc)}
                if pid is None and proc["name"] == target:
                    return {"ok": True, "action": "process",
                            "process": dict(proc)}
            return {"ok": False, "action": "process", "kind": "not_found",
                    "error": f"no process {pid or target!r}"}

    def system_info(self) -> dict[str, Any]:
        with self._lock:
            self._require_connected("system_info")
            return {"ok": True, "action": "system_info",
                    "simulation": True,
                    "os": "fakeos 1.0", "hostname": "fake-desktop",
                    "cpu": "FakeCPU", "memory": "16 GB",
                    "screen": f"{self.screen_width}x{self.screen_height}"}

    def file_select(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require_connected("file_select")
            pattern = params.get("pattern", "")
            matches = sorted(
                path for path in self.files
                if not pattern or pattern in path)
            return {"ok": True, "action": "file_select",
                    "pattern": pattern, "files": matches}

    # -- actuation -----------------------------------------------------------

    def window(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require_connected("window")
            op = params.get("op")
            name = target or params.get("window", self.focused)
            if name not in self.windows:
                return {"ok": False, "action": "window", "kind": "not_found",
                        "error": f"no window {name!r}"}
            win = self.windows[name]
            if op == "close":
                del self.windows[name]
                if self.focused == name:
                    self.focused = "desktop"
            elif op in ("focus", "restore"):
                win["visible"] = True
                self.focused = name
            elif op == "minimize":
                win["visible"] = False
            elif op in ("move", "resize"):
                for key in ("x", "y", "w", "h"):
                    if key in params:
                        win[key] = params[key]
            self._log("window", target, {"op": op})
            return {"ok": True, "action": "window", "op": op, "window": name,
                    "focused": self.focused}

    def mouse_move(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require_connected("mouse_move")
            x, y = params["x"], params["y"]
            if not (0 <= x <= self.screen_width and
                    0 <= y <= self.screen_height):
                return {"ok": False, "action": "mouse_move",
                        "kind": "out_of_bounds",
                        "error": f"({x}, {y}) outside screen"}
            self._log("mouse_move", target, {"x": x, "y": y})
            return {"ok": True, "action": "mouse_move", "x": x, "y": y}

    def mouse_click(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require_connected("mouse_click")
            x, y = params["x"], params["y"]
            button = params.get("button", "left")
            double = bool(params.get("double", False))
            if not (0 <= x <= self.screen_width and
                    0 <= y <= self.screen_height):
                return {"ok": False, "action": "mouse_click",
                        "kind": "out_of_bounds",
                        "error": f"({x}, {y}) outside screen"}
            if target and target in self.windows and button == "left":
                self.windows[target]["visible"] = True
                self.focused = target
            self._log("mouse_click", target,
                      {"x": x, "y": y, "button": button, "double": double})
            return {"ok": True, "action": "mouse_click", "x": x, "y": y,
                    "button": button, "double": double, "focused": self.focused}

    def keyboard(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require_connected("keyboard")
            text = params.get("text", "")
            keys = params.get("keys", [])
            if target and target in self.windows and target != self.focused:
                return {"ok": False, "action": "keyboard", "kind": "unfocused",
                        "error": f"window {target!r} is not focused"}
            self._log("keyboard", target, {"text": text, "keys": list(keys)})
            return {"ok": True, "action": "keyboard", "text": text,
                    "keys": list(keys), "focused": self.focused}

    def launch(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require_connected("launch")
            if not target:
                return {"ok": False, "action": "launch", "kind": "invalid",
                        "error": "no application specified"}
            executable = self.apps.get(target, f"/usr/bin/{target}")
            args = list(params.get("args", []))
            pid = self._next_pid
            self._next_pid += 1
            self.processes.append({"pid": pid, "name": target,
                                   "user": "user", "args": args})
            self.windows[target] = {"title": f"{target} — untitled",
                                    "app": target, "x": 120, "y": 120,
                                    "w": 800, "h": 600, "visible": True,
                                    "focused": True}
            self.focused = target
            self._log("launch", target, {"args": args})
            return {"ok": True, "action": "launch", "app": target,
                    "pid": pid, "executable": executable, "args": args}

    def clipboard(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require_connected("clipboard")
            op = params.get("op")
            if op == "write":
                self.clipboard_text = params.get("content", "")
                self._log("clipboard", target, {"op": "write"})
                return {"ok": True, "action": "clipboard", "op": "write",
                        "chars": len(self.clipboard_text)}
            self._log("clipboard", target, {"op": "read"})
            return {"ok": True, "action": "clipboard", "op": "read",
                    "content": self.clipboard_text}

    def file_access(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require_connected("file_access")
            mode = params.get("mode")
            path = target
            if mode == "write":
                self.files[path] = params.get("content", "")
                self._log("file_access", target, {"mode": mode})
                return {"ok": True, "action": "file_access", "mode": mode,
                        "path": path, "chars": len(self.files[path])}
            if mode == "delete":
                existed = self.files.pop(path, None) is not None
                self._log("file_access", target, {"mode": mode})
                return {"ok": existed, "action": "file_access",
                        "mode": mode, "path": path,
                        "error": "" if existed else "file not found"}
            content = self.files.get(path)
            self._log("file_access", target, {"mode": mode})
            if content is None:
                return {"ok": False, "action": "file_access",
                        "kind": "not_found", "error": f"no file {path!r}"}
            return {"ok": True, "action": "file_access", "mode": mode,
                    "path": path, "content": content}

    def app_action(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require_connected("app_action")
            action = params.get("action", "")
            if target not in self.windows:
                return {"ok": False, "action": "app_action",
                        "kind": "not_found",
                        "error": f"application {target!r} is not running"}
            if action == "close_window":
                del self.windows[target]
                if self.focused == target:
                    self.focused = "desktop"
            elif action == "save":
                self.windows[target]["title"] = f"{target} — saved"
            self._log("app_action", target, {"action": action})
            return {"ok": True, "action": "app_action", "app": target,
                    "app_action": action, "focused": self.focused}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "connected": self._connected,
                "focused": self.focused,
                "window_count": len(self.windows),
                "windows": sorted(self.windows),
                "processes": [p["name"] for p in self.processes],
                "clipboard_chars": len(self.clipboard_text),
                "files": sorted(self.files),
                "input_events": len(self.input_log),
                "simulation": True,
            }

    # -- internals -----------------------------------------------------------

    def _require_connected(self, action: str) -> None:
        with self._lock:
            if self._fail_all:
                raise DesktopProviderError(
                    "unavailable", "desktop provider failing all actions")
            if self._fail_next and self._fail_next[0] == action:
                self._fail_next.pop(0)
                raise DesktopProviderError(
                    "unavailable", f"injected failure for {action!r}")
        if not self._connected:
            raise DesktopProviderError(
                "disconnected", "desktop provider is disconnected")

    def _log(self, action: str, target: str, params: dict[str, Any]) -> None:
        self.input_log.append(
            {"action": action, "target": target, "params": dict(params),
             "at": time.time()})
