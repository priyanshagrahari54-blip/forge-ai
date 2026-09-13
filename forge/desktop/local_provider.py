"""Real local-machine desktop provider (no simulation).

:class:`LocalDesktopProvider` implements the :class:`DesktopProvider`
protocol against the actual host:

* **Always real** (stdlib only): ``system_info``, ``list_processes`` /
  ``process`` (Linux ``/proc``, macOS ``ps``, Windows ``tasklist``),
  ``file_select`` / ``file_access`` (confined under ``file_root``),
  ``launch`` (no shell, argv only, bare names resolved on ``PATH``).
* **Real when a backend exists, honest otherwise**: screenshots
  (scrot/ImageMagick/grim/screencapture), windows/mouse/keyboard
  (xdotool on X11), clipboard (xclip/xsel/wl-clipboard/pbcopy).
  Without a backend each method returns ``ok: False`` with
  ``kind: "unsupported"`` and the missing tool named — never fake
  coordinates, windows, or pixels.

There is no OCR here: ``read_screen`` reports window focus/geometry
and says so. Policy, approvals, audit, and risk gating stay upstream
in :class:`DesktopAgent`; this provider only executes and observes.
Destructive self-targets are refused outright (PID 1, our own PID).
"""
from __future__ import annotations

import csv
import os
import platform
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from forge.core.portability import stop_signal

MAX_FILE_BYTES = 1_000_000
MAX_TEXT_CHARS = 2_000
MAX_ARGS = 32
MAX_ARG_CHARS = 4_096
MAX_PROCESSES = 500
MAX_FILES = 200

_PNG_SIGNATURE = (b"\x89PNG\r\n\x1a\n")


def _backend(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _run(argv: list[str], *, timeout: float,
         input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, input=input_bytes, capture_output=True,
                          timeout=timeout, check=False)


def _png_size(data: bytes) -> tuple[int, int] | None:
    if (len(data) >= 24 and data[:8] == _PNG_SIGNATURE
            and data[12:16] == b"IHDR"):
        width, height = struct.unpack(">II", data[16:24])
        if width and height:
            return width, height
    return None


class LocalDesktopProvider:
    """DesktopProvider backed by the real local machine."""

    name = "local"
    simulation = False

    def __init__(self, *, file_root: str | Path | None = None,
                 timeout: float = 30.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if file_root is None:
            desktop = Path.home() / "Desktop"
            file_root = desktop if desktop.is_dir() else Path.cwd()
        self.file_root = Path(file_root).resolve()
        self.file_root.mkdir(parents=True, exist_ok=True)
        self.timeout = float(timeout)
        self._launched: dict[int, dict[str, Any]] = {}

    # -- backend detection ---------------------------------------------------

    def backends(self) -> dict[str, Any]:
        """Report the detected GUI backends (re-probed every call)."""
        system = platform.system()
        display = os.environ.get("DISPLAY", "")
        session_type = os.environ.get("XDG_SESSION_TYPE", "")
        wayland = bool(os.environ.get("WAYLAND_DISPLAY")) or (
            session_type == "wayland")
        x11 = bool(display) and not wayland
        screenshot = None
        if system == "Darwin":
            screenshot = _backend("screencapture")
        elif system == "Windows":
            screenshot = None
        elif wayland:
            screenshot = _backend("grim")
        else:
            screenshot = _backend("scrot", "import", "xwd")
        input_tool = None
        windows_tool = None
        if x11:
            input_tool = _backend("xdotool")
            windows_tool = input_tool
        clipboard = None
        if system == "Darwin":
            clipboard = _backend("pbcopy", "pbpaste")
        elif wayland:
            clipboard = _backend("wl-copy", "wl-paste")
        elif system != "Windows":
            clipboard = _backend("xclip", "xsel")
        return {"system": system, "x11": x11, "wayland": wayland,
                "display": display or None,
                "screenshot": screenshot, "input": input_tool,
                "windows": windows_tool, "clipboard": clipboard}

    def _unsupported(self, action: str, need: str) -> dict[str, Any]:
        return {"ok": False, "action": action, "kind": "unsupported",
                "simulation": False,
                "error": (f"no {need} backend on this machine "
                          f"({platform.system()}); refusing to fake it")}

    # -- health ------------------------------------------------------------------

    def healthy(self) -> bool:
        return True

    # -- observations --------------------------------------------------------------

    def screenshot(self) -> dict[str, Any]:
        import base64

        backend = self.backends()["screenshot"]
        if backend is None:
            return self._unsupported("screenshot", "screenshot")
        try:
            with tempfile.NamedTemporaryFile(
                    suffix=".png", delete=False) as handle:
                path = handle.name
            try:
                tool = Path(backend).name
                if tool == "screencapture":
                    proc = _run([backend, "-x", "-t", "png", path],
                                timeout=self.timeout)
                elif tool == "grim":
                    proc = _run([backend, path], timeout=self.timeout)
                elif tool == "import":
                    proc = _run([backend, "-window", "root", path],
                                timeout=self.timeout)
                else:
                    proc = _run([backend, path], timeout=self.timeout)
                if proc.returncode != 0:
                    return {"ok": False, "action": "screenshot",
                            "kind": "backend_error", "backend": backend,
                            "error": (proc.stderr or b"")[:300].decode(
                                "utf-8", errors="replace")}
                data = Path(path).read_bytes()
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        except subprocess.TimeoutExpired:
            return {"ok": False, "action": "screenshot",
                    "kind": "timeout", "backend": backend,
                    "error": f"screenshot exceeded {self.timeout:.0f}s"}
        except OSError as exc:
            return {"ok": False, "action": "screenshot",
                    "kind": "backend_error", "backend": backend,
                    "error": str(exc)[:300]}
        size = _png_size(data)
        if size is None:
            return {"ok": False, "action": "screenshot",
                    "kind": "backend_error", "backend": backend,
                    "error": "backend did not produce a PNG image"}
        width, height = size
        return {"ok": True, "action": "screenshot", "format": "png",
                "width": width, "height": height,
                "bytes": len(data), "backend": backend,
                "image_b64": base64.b64encode(data).decode("ascii"),
                "captured_at": time.time()}

    def read_screen(self) -> dict[str, Any]:
        info: dict[str, Any] = {"ok": True, "action": "read_screen",
                                "ocr_available": False, "regions": [],
                                "text": "",
                                "note": ("no OCR engine bundled; window "
                                         "focus/geometry only")}
        windows_tool = self.backends()["windows"]
        if windows_tool is None:
            info["active_window"] = None
            info["window_count"] = None
            return info
        try:
            active = _run([windows_tool, "getactivewindow",
                           "getwindowname"], timeout=self.timeout)
            title = (active.stdout or b"").decode(
                "utf-8", errors="replace").strip()
            info["active_window"] = title or None
            listing = self.list_windows()
            if listing.get("ok"):
                info["window_count"] = len(listing["windows"])
        except (OSError, subprocess.TimeoutExpired) as exc:
            info["active_window"] = None
            info["backend_error"] = str(exc)[:200]
        return info

    def list_windows(self) -> dict[str, Any]:
        windows_tool = self.backends()["windows"]
        if windows_tool is None:
            return self._unsupported("window_list", "window manager")
        try:
            proc = _run([windows_tool, "search", "--onlyvisible",
                         "--name", "."], timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "action": "window_list",
                    "kind": "timeout",
                    "error": "window search timed out"}
        except OSError as exc:
            return {"ok": False, "action": "window_list",
                    "kind": "backend_error", "error": str(exc)[:200]}
        if proc.returncode != 0:
            return {"ok": False, "action": "window_list",
                    "kind": "backend_error",
                    "error": (proc.stderr or b"")[:300].decode(
                        "utf-8", errors="replace")}
        ids = (proc.stdout or b"").decode().split()[:50]
        windows = []
        for window_id in ids:
            try:
                name = _run([windows_tool, "getwindowname", window_id],
                            timeout=self.timeout)
                title = (name.stdout or b"").decode(
                    "utf-8", errors="replace").strip()
            except (OSError, subprocess.TimeoutExpired):
                title = ""
            windows.append({"id": window_id, "title": title,
                            "visible": True})
        focused = ""
        try:
            active = _run([windows_tool, "getactivewindow"],
                          timeout=self.timeout)
            focused = (active.stdout or b"").decode().strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        return {"ok": True, "action": "window_list", "focused": focused,
                "windows": windows, "backend": windows_tool}

    def list_processes(self) -> dict[str, Any]:
        try:
            processes = self._read_processes()
        except OSError as exc:
            return {"ok": False, "action": "process_list",
                    "kind": "backend_error", "error": str(exc)[:300]}
        if processes is None:
            return self._unsupported("process_list", "process table")
        return {"ok": True, "action": "process_list",
                "processes": processes}

    def process(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        op = str(params.get("op", "poll"))
        if op not in ("poll", "terminate", "kill"):
            return {"ok": False, "action": "process", "kind": "invalid",
                    "error": f"unknown process op {op!r}"}
        pid = params.get("pid")
        if pid is None and target:
            found = self._find_process(target)
            if found is None:
                return {"ok": False, "action": "process",
                        "kind": "not_found",
                        "error": f"no process named {target!r}"}
            pid = found["pid"]
        if not isinstance(pid, int) or pid <= 0:
            return {"ok": False, "action": "process", "kind": "invalid",
                    "error": f"invalid pid {pid!r}"}
        running = self._pid_running(pid)
        if op == "poll":
            return {"ok": True, "action": "process", "pid": pid,
                    "running": running}
        if pid == 1 or pid == os.getpid():
            return {"ok": False, "action": "process", "kind": "refused",
                    "error": f"refusing to signal pid {pid}"}
        if not running:
            return {"ok": False, "action": "process", "kind": "not_found",
                    "error": f"pid {pid} is not running"}
        # Windows has no signal.SIGKILL, so the naive ternary raised
        # AttributeError there (uncaught by the OSError handlers below).
        signum, signalled = stop_signal(op)
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            return {"ok": False, "action": "process", "kind": "not_found",
                    "error": f"pid {pid} is not running"}
        except PermissionError:
            return {"ok": False, "action": "process", "kind": "denied",
                    "error": f"no permission to signal pid {pid}"}
        except OSError as exc:
            return {"ok": False, "action": "process",
                    "kind": "backend_error", "error": str(exc)[:200]}
        return {"ok": True, "action": "process", "pid": pid,
                "signalled": signalled}

    def system_info(self) -> dict[str, Any]:
        backends = self.backends()
        try:
            hostname = socket.gethostname()
        except OSError:
            hostname = ""
        return {"ok": True, "action": "system_info",
                "simulation": False,
                "os": f"{platform.system()} {platform.release()}",
                "machine": platform.machine(),
                "hostname": hostname, "pid": os.getpid(),
                "cpu_count": os.cpu_count(), "cwd": os.getcwd(),
                "python": platform.python_version(),
                "display": backends["display"],
                "backends": {key: bool(value) for key, value in
                             backends.items()
                             if key in ("screenshot", "input", "windows",
                                        "clipboard")}}

    # -- files (confined, real) ---------------------------------------------------------

    def _confine(self, target: str) -> Path:
        if not target or not isinstance(target, str):
            raise ValueError("file target must be a non-empty string")
        candidate = (self.file_root / target).resolve()
        try:
            candidate.relative_to(self.file_root)
        except ValueError:
            raise ValueError(f"path escapes file root: {target!r}")
        return candidate

    def file_select(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        _ = target
        pattern = str(params.get("pattern", ""))
        matches: list[str] = []
        try:
            entries = sorted(self.file_root.rglob("*"))[:MAX_FILES * 4]
        except OSError as exc:
            return {"ok": False, "action": "file_select",
                    "kind": "backend_error", "error": str(exc)[:200]}
        for entry in entries:
            if not entry.is_file() or entry.is_symlink():
                continue
            relative = entry.relative_to(self.file_root).as_posix()
            if not pattern or pattern in relative:
                matches.append(relative)
            if len(matches) >= MAX_FILES:
                break
        return {"ok": True, "action": "file_select", "pattern": pattern,
                "root": str(self.file_root), "files": matches,
                "truncated": len(matches) >= MAX_FILES}

    def file_access(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        mode = str(params.get("mode", "read"))
        if mode not in ("read", "write", "delete", "list"):
            return {"ok": False, "action": "file_access", "kind": "invalid",
                    "error": f"unknown file mode {mode!r}"}
        try:
            path = self._confine(target)
        except ValueError as exc:
            return {"ok": False, "action": "file_access",
                    "kind": "invalid", "error": str(exc)}
        if mode == "list":
            if not path.is_dir():
                return {"ok": False, "action": "file_access",
                        "kind": "not_found",
                        "error": f"not a directory: {target!r}"}
            try:
                names = sorted(item.name for item in path.iterdir()
                               )[:MAX_FILES]
            except OSError as exc:
                return {"ok": False, "action": "file_access",
                        "kind": "backend_error", "error": str(exc)[:200]}
            return {"ok": True, "action": "file_access", "mode": mode,
                    "path": target, "entries": names}
        if mode == "write":
            content = params.get("content", "")
            if not isinstance(content, str):
                return {"ok": False, "action": "file_access",
                        "kind": "invalid",
                        "error": "write content must be a string"}
            data = content.encode("utf-8")
            if len(data) > MAX_FILE_BYTES:
                return {"ok": False, "action": "file_access",
                        "kind": "too_large",
                        "error": f"write exceeds {MAX_FILE_BYTES} bytes"}
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            except OSError as exc:
                return {"ok": False, "action": "file_access",
                        "kind": "backend_error", "error": str(exc)[:200]}
            return {"ok": True, "action": "file_access", "mode": mode,
                    "path": target, "chars": len(content)}
        if mode == "delete":
            try:
                if path.is_symlink() or not path.is_file():
                    return {"ok": False, "action": "file_access",
                            "mode": mode, "path": target,
                            "error": "file not found"}
                path.unlink()
            except OSError as exc:
                return {"ok": False, "action": "file_access",
                        "kind": "backend_error", "error": str(exc)[:200]}
            return {"ok": True, "action": "file_access", "mode": mode,
                    "path": target}
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return {"ok": False, "action": "file_access",
                    "kind": "not_found", "error": f"no file {target!r}"}
        except OSError as exc:
            return {"ok": False, "action": "file_access",
                    "kind": "backend_error", "error": str(exc)[:200]}
        truncated = len(data) > MAX_FILE_BYTES
        text = data[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
        return {"ok": True, "action": "file_access", "mode": mode,
                "path": target, "content": text,
                "bytes": len(data), "truncated": truncated}

    # -- actuation -----------------------------------------------------------------------------

    def window(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        op = str(params.get("op", "focus"))
        if op not in ("focus", "minimize", "close", "move", "resize",
                      "restore"):
            return {"ok": False, "action": "window", "kind": "invalid",
                    "error": f"unknown window op {op!r}"}
        name = target or str(params.get("window", ""))
        if not name:
            return {"ok": False, "action": "window", "kind": "invalid",
                    "error": "no window specified"}
        windows_tool = self.backends()["windows"]
        if windows_tool is None:
            return self._unsupported("window", "window manager")
        window_id = self._window_id(windows_tool, name)
        if window_id is None:
            return {"ok": False, "action": "window", "kind": "not_found",
                    "error": f"no window {name!r}"}
        argv: list[str] | None = None
        if op == "focus":
            argv = [windows_tool, "windowactivate", window_id]
        elif op == "minimize":
            argv = [windows_tool, "windowminimize", window_id]
        elif op in ("restore",):
            argv = [windows_tool, "windowmap", window_id, "windowactivate",
                   window_id]
        elif op == "close":
            argv = [windows_tool, "windowkill", window_id]
        elif op in ("move", "resize"):
            try:
                x = int(params.get("x", 0))
                y = int(params.get("y", 0))
            except (TypeError, ValueError):
                return {"ok": False, "action": "window",
                        "kind": "invalid",
                        "error": f"window {op} needs integer x/y"}
            if op == "move":
                argv = [windows_tool, "windowmove", window_id, str(x),
                       str(y)]
            else:
                try:
                    w = int(params["w"])
                    h = int(params["h"])
                except (KeyError, TypeError, ValueError):
                    return {"ok": False, "action": "window",
                            "kind": "invalid",
                            "error": "window resize needs integer w/h"}
                argv = [windows_tool, "windowsize", window_id, str(w),
                       str(h)]
        assert argv is not None
        try:
            proc = _run(argv, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "action": "window", "kind": "timeout",
                    "error": f"window {op} timed out"}
        except OSError as exc:
            return {"ok": False, "action": "window",
                    "kind": "backend_error", "error": str(exc)[:200]}
        if proc.returncode != 0:
            return {"ok": False, "action": "window",
                    "kind": "backend_error",
                    "error": (proc.stderr or b"")[:300].decode(
                        "utf-8", errors="replace")}
        return {"ok": True, "action": "window", "op": op,
                "window": window_id, "backend": windows_tool}

    def _window_id(self, windows_tool: str, name: str) -> str | None:
        try:
            proc = _run([windows_tool, "search", "--name", name],
                        timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        ids = (proc.stdout or b"").decode().split()
        return ids[0] if ids else None

    def _screen_size(self, input_tool: str) -> tuple[int, int] | None:
        try:
            proc = _run([input_tool, "getdisplaygeometry"],
                        timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        parts = (proc.stdout or b"").decode().split()
        if len(parts) != 2:
            return None
        try:
            width, height = int(parts[0]), int(parts[1])
        except ValueError:
            return None
        if width <= 0 or height <= 0:
            return None
        return width, height

    def mouse_move(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        _ = target
        try:
            x = int(params["x"])
            y = int(params["y"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "action": "mouse_move",
                    "kind": "invalid",
                    "error": "mouse_move needs integer x/y"}
        input_tool = self.backends()["input"]
        if input_tool is None:
            return self._unsupported("mouse_move", "input")
        size = self._screen_size(input_tool)
        if size is not None and not (0 <= x < size[0] and 0 <= y < size[1]):
            return {"ok": False, "action": "mouse_move",
                    "kind": "out_of_bounds",
                    "error": f"({x}, {y}) outside {size[0]}x{size[1]}"}
        try:
            proc = _run([input_tool, "mousemove", str(x), str(y)],
                        timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "action": "mouse_move", "kind": "timeout",
                    "error": "mouse_move timed out"}
        except OSError as exc:
            return {"ok": False, "action": "mouse_move",
                    "kind": "backend_error", "error": str(exc)[:200]}
        if proc.returncode != 0:
            return {"ok": False, "action": "mouse_move",
                    "kind": "backend_error",
                    "error": (proc.stderr or b"")[:300].decode(
                        "utf-8", errors="replace")}
        return {"ok": True, "action": "mouse_move", "x": x, "y": y,
                "backend": input_tool}

    def mouse_click(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        _ = target
        try:
            x = int(params["x"])
            y = int(params["y"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "action": "mouse_click",
                    "kind": "invalid",
                    "error": "mouse_click needs integer x/y"}
        button = str(params.get("button", "left"))
        if button not in ("left", "middle", "right"):
            return {"ok": False, "action": "mouse_click",
                    "kind": "invalid",
                    "error": f"unknown button {button!r}"}
        double = bool(params.get("double", False))
        input_tool = self.backends()["input"]
        if input_tool is None:
            return self._unsupported("mouse_click", "input")
        size = self._screen_size(input_tool)
        if size is not None and not (0 <= x < size[0] and 0 <= y < size[1]):
            return {"ok": False, "action": "mouse_click",
                    "kind": "out_of_bounds",
                    "error": f"({x}, {y}) outside {size[0]}x{size[1]}"}
        number = {"left": "1", "middle": "2", "right": "3"}[button]
        argv = [input_tool, "mousemove", str(x), str(y), "click"]
        if double:
            argv += ["--repeat", "2", "--delay", "50"]
        argv.append(number)
        try:
            proc = _run(argv, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "action": "mouse_click",
                    "kind": "timeout", "error": "mouse_click timed out"}
        except OSError as exc:
            return {"ok": False, "action": "mouse_click",
                    "kind": "backend_error", "error": str(exc)[:200]}
        if proc.returncode != 0:
            return {"ok": False, "action": "mouse_click",
                    "kind": "backend_error",
                    "error": (proc.stderr or b"")[:300].decode(
                        "utf-8", errors="replace")}
        return {"ok": True, "action": "mouse_click", "x": x, "y": y,
                "button": button, "double": double,
                "backend": input_tool}

    def keyboard(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        _ = target
        text = params.get("text", "")
        keys = params.get("keys", [])
        if not isinstance(text, str) or not isinstance(keys, list):
            return {"ok": False, "action": "keyboard", "kind": "invalid",
                    "error": "keyboard needs text str and keys list"}
        if len(text) > MAX_TEXT_CHARS:
            return {"ok": False, "action": "keyboard", "kind": "too_large",
                    "error": f"text exceeds {MAX_TEXT_CHARS} chars"}
        clean_keys: list[str] = []
        for key in keys[:32]:
            if not isinstance(key, str) or not key:
                return {"ok": False, "action": "keyboard",
                        "kind": "invalid",
                        "error": f"invalid key {key!r}"}
            if any(char.isspace() for char in key) or len(key) > 32:
                return {"ok": False, "action": "keyboard",
                        "kind": "invalid",
                        "error": f"invalid key {key!r}"}
            clean_keys.append(key)
        input_tool = self.backends()["input"]
        if input_tool is None:
            return self._unsupported("keyboard", "input")
        if text:
            try:
                proc = _run([input_tool, "type", "--delay", "0", "--",
                             text], timeout=self.timeout)
            except subprocess.TimeoutExpired:
                return {"ok": False, "action": "keyboard",
                        "kind": "timeout", "error": "typing timed out"}
            except OSError as exc:
                return {"ok": False, "action": "keyboard",
                        "kind": "backend_error", "error": str(exc)[:200]}
            if proc.returncode != 0:
                return {"ok": False, "action": "keyboard",
                        "kind": "backend_error",
                        "error": (proc.stderr or b"")[:300].decode(
                            "utf-8", errors="replace")}
        if clean_keys:
            try:
                proc = _run([input_tool, "key", *clean_keys],
                            timeout=self.timeout)
            except subprocess.TimeoutExpired:
                return {"ok": False, "action": "keyboard",
                        "kind": "timeout",
                        "error": "key press timed out"}
            except OSError as exc:
                return {"ok": False, "action": "keyboard",
                        "kind": "backend_error", "error": str(exc)[:200]}
            if proc.returncode != 0:
                return {"ok": False, "action": "keyboard",
                        "kind": "backend_error",
                        "error": (proc.stderr or b"")[:300].decode(
                            "utf-8", errors="replace")}
        return {"ok": True, "action": "keyboard",
                "typed_chars": len(text), "keys": clean_keys,
                "backend": input_tool}

    def launch(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        if not target or not isinstance(target, str):
            return {"ok": False, "action": "launch", "kind": "invalid",
                    "error": "no application specified"}
        args = params.get("args", [])
        if not isinstance(args, list) or any(
                not isinstance(item, str) for item in args):
            return {"ok": False, "action": "launch", "kind": "invalid",
                    "error": "args must be a list of strings"}
        if len(args) > MAX_ARGS or any(len(item) > MAX_ARG_CHARS
                                       for item in args):
            return {"ok": False, "action": "launch", "kind": "too_large",
                    "error": "too many or too long args"}
        if "/" in target or "\\" in target:
            candidate = Path(target)
            if not (candidate.is_file() and os.access(candidate, os.X_OK)):
                return {"ok": False, "action": "launch",
                        "kind": "not_found",
                        "error": f"not executable: {target!r}"}
            executable = str(candidate)
        else:
            resolved = shutil.which(target)
            if resolved is None:
                return {"ok": False, "action": "launch",
                        "kind": "not_found",
                        "error": f"not on PATH: {target!r}"}
            executable = resolved
        try:
            proc = subprocess.Popen(
                [executable, *args], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except OSError as exc:
            return {"ok": False, "action": "launch",
                    "kind": "backend_error", "error": str(exc)[:200]}
        self._launched[proc.pid] = {"app": target, "args": list(args),
                                    "at": time.time()}
        return {"ok": True, "action": "launch", "app": target,
                "pid": proc.pid, "executable": executable,
                "args": list(args)}

    def clipboard(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        _ = target
        op = str(params.get("op", "read"))
        if op not in ("read", "write"):
            return {"ok": False, "action": "clipboard", "kind": "invalid",
                    "error": f"unknown clipboard op {op!r}"}
        tool = self.backends()["clipboard"]
        if tool is None:
            return self._unsupported("clipboard", "clipboard")
        name = Path(tool).name
        payload: bytes | None = None
        if op == "write":
            content = params.get("content", "")
            if not isinstance(content, str):
                return {"ok": False, "action": "clipboard",
                        "kind": "invalid",
                        "error": "content must be a string"}
            payload = content.encode("utf-8")
            if len(payload) > MAX_FILE_BYTES:
                return {"ok": False, "action": "clipboard",
                        "kind": "too_large",
                        "error": "clipboard content too large"}
        argv = self._clipboard_argv(tool, name, op)
        if argv is None:
            return self._unsupported("clipboard", "clipboard")
        try:
            proc = _run(argv, input_bytes=payload, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "action": "clipboard", "kind": "timeout",
                    "error": "clipboard timed out"}
        except OSError as exc:
            return {"ok": False, "action": "clipboard",
                    "kind": "backend_error", "error": str(exc)[:200]}
        if proc.returncode != 0:
            return {"ok": False, "action": "clipboard",
                    "kind": "backend_error",
                    "error": (proc.stderr or b"")[:300].decode(
                        "utf-8", errors="replace")}
        if op == "write":
            return {"ok": True, "action": "clipboard", "op": op,
                    "chars": len(payload or b""), "backend": tool}
        content = (proc.stdout or b"")[:MAX_FILE_BYTES].decode(
            "utf-8", errors="replace")
        return {"ok": True, "action": "clipboard", "op": op,
                "content": content, "backend": tool}

    @staticmethod
    def _clipboard_argv(tool: str, name: str, op: str) -> list[str] | None:
        if name in ("pbcopy", "pbpaste"):
            binary = shutil.which("pbcopy" if op == "write" else "pbpaste")
            return [binary] if binary else None
        if name in ("wl-copy", "wl-paste"):
            binary = shutil.which("wl-copy" if op == "write" else "wl-paste")
            return [binary] if binary else None
        if name == "xclip":
            argv = [tool, "-selection", "clipboard"]
            return argv if op == "write" else [*argv, "-o"]
        if name == "xsel":
            argv = [tool, "--clipboard"]
            return [*argv, "--input"] if op == "write" else [*argv, "--output"]
        return None

    def app_action(self, target: str, params: dict[str, Any]) -> dict[str, Any]:
        action = str(params.get("action", ""))
        if action == "close_window":
            return self.window(target, {"op": "close"})
        if action == "activate":
            return self.window(target, {"op": "focus"})
        return {"ok": False, "action": "app_action", "kind": "unsupported",
                "error": (f"unknown app action {action!r}; supported: "
                          "close_window, activate")}

    def snapshot(self) -> dict[str, Any]:
        backends = self.backends()
        processes = self.list_processes()
        try:
            top_level = sum(1 for _ in self.file_root.iterdir())
        except OSError:
            top_level = -1
        windows = self.list_windows()
        return {
            "connected": True,
            "simulation": False,
            "provider": self.name,
            "backends": {key: bool(value) for key, value in
                         backends.items()
                         if key in ("screenshot", "input", "windows",
                                    "clipboard")},
            "window_count": (len(windows["windows"])
                             if windows.get("ok") else None),
            "process_count": (len(processes["processes"])
                              if processes.get("ok") else None),
            "file_root": str(self.file_root),
            "file_root_entries": top_level,
            "launched_by_provider": len(self._launched),
        }

    # -- process table internals ------------------------------------------------------

    def _read_processes(self) -> list[dict[str, Any]] | None:
        system = platform.system()
        if system == "Linux" and Path("/proc").is_dir():
            return self._read_proc()
        if system == "Darwin":
            return self._read_ps()
        if system == "Windows":
            return self._read_tasklist()
        return None

    def _read_proc(self) -> list[dict[str, Any]]:
        processes: list[dict[str, Any]] = []
        for entry in sorted(Path("/proc").iterdir()):
            if not entry.name.isdigit():
                continue
            try:
                name = (entry / "comm").read_text(
                    encoding="utf-8", errors="replace").strip()
                cmdline = (entry / "cmdline").read_bytes().replace(
                    b"\0", b" ").decode("utf-8", errors="replace").strip()
                uid = (entry / "status").read_text(
                    encoding="utf-8",
                    errors="replace").split("Uid:")[1].split()[0]
            except (OSError, IndexError):
                continue  # process exited mid-read
            processes.append({"pid": int(entry.name), "name": name,
                              "user": self._user_name(uid),
                              "cmdline": cmdline[:300]})
            if len(processes) >= MAX_PROCESSES:
                break
        return processes

    @staticmethod
    def _user_name(uid: str) -> str:
        try:
            import pwd

            return pwd.getpwuid(int(uid)).pw_name
        except (ImportError, KeyError, ValueError):
            return uid

    def _read_ps(self) -> list[dict[str, Any]] | None:
        ps = shutil.which("ps")
        if ps is None:
            return None
        proc = _run([ps, "-ax", "-o", "pid=,user=,comm="],
                    timeout=self.timeout)
        if proc.returncode != 0:
            return []
        processes = []
        for line in (proc.stdout or b"").decode(
                "utf-8", errors="replace").splitlines():
            parts = line.split(None, 2)
            if len(parts) != 3:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            processes.append({"pid": pid, "user": parts[1],
                              "name": parts[2].strip(), "cmdline": ""})
            if len(processes) >= MAX_PROCESSES:
                break
        return processes

    def _read_tasklist(self) -> list[dict[str, Any]] | None:
        tasklist = shutil.which("tasklist")
        if tasklist is None:
            return None
        proc = _run([tasklist, "/FO", "CSV", "/NH"], timeout=self.timeout)
        if proc.returncode != 0:
            return []
        processes = []
        reader = csv.reader((proc.stdout or b"").decode(
            "utf-8", errors="replace").splitlines())
        for row in reader:
            if len(row) < 2:
                continue
            try:
                pid = int(row[1])
            except ValueError:
                continue
            processes.append({"pid": pid, "name": row[0], "user": "",
                              "cmdline": ""})
            if len(processes) >= MAX_PROCESSES:
                break
        return processes

    def _find_process(self, name: str) -> dict[str, Any] | None:
        listing = self.list_processes()
        if not listing.get("ok"):
            return None
        lowered = name.lower()
        for proc in listing["processes"]:
            if proc.get("name", "").lower() == lowered:
                return proc
        return None

    @staticmethod
    def _pid_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
