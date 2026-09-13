# Python 3.8 and Windows 7 32-bit support

Forge's declared interpreter floor is **Python 3.8**
(`requires-python = ">=3.8"` in `pyproject.toml`), and the reference legacy
target is **Windows 7 32-bit**. This document states what that actually means,
what is pinned to make it true, and which parts are known limitations rather
than promises.

The forensic audit behind these decisions is
[`PYTHON38-COMPAT-AUDIT.md`](PYTHON38-COMPAT-AUDIT.md).

## Why 3.8 is the floor

CPython 3.8 is the last series that runs on Windows 7 and the last with 32-bit
Windows installers. Targeting Windows 7 32-bit therefore *means* targeting
Python 3.8 — there is no newer interpreter to use there. 32-bit also means a
2 GiB user address space, so large in-memory operations need to stay bounded
(Forge's output caps, e.g. `MAX_OUTPUT` in `forge/compute/remote.py`, matter
more on this target than on a 64-bit host).

## The dependency problem, and how it is solved

Every runtime and test dependency has, by now, shipped a release that no longer
supports Python 3.8. Verified against PyPI `Requires-Python`:

| Project | Latest | Latest requires | Newest that admits 3.8 |
|---|---|---|---|
| `pydantic` | 2.13.5 | `>=3.9` | **2.10.6** |
| `fastapi` | 0.141.1 | `>=3.10` | **0.124.4** |
| `uvicorn` | 0.52.4 | `>=3.10` | **0.33.0** |
| `pytest` | 9.1.1 | `>=3.10` | **8.3.5** |
| `PyYAML` | 6.0.3 | `>=3.8` | 6.0.3 (still fine) |
| `httpx` | 0.28.1 | `>=3.8` | 0.28.1 (still fine) |

Three layers keep this from breaking:

1. **`pyproject.toml` uses `python_version` markers.** Each dependency that
   dropped 3.8 appears twice: a `python_version < '3.9'` line capped just below
   the release that dropped 3.8, and a `python_version >= '3.9'` line left
   uncapped within its major. Python 3.11+ is therefore *not* held back by the
   legacy floor.
2. **`requirements/py38.txt` / `py38-dev.txt` freeze the resolved set** for
   reproducible 3.8 installs. These are what CI and the Windows 7 instructions
   use. Regeneration is documented at the top of `py38.txt`.
3. **`tests/test_python38_compat.py` enforces it offline.** It parses
   `pyproject.toml` and both requirement files and fails if a 3.8-applicable
   requirement loses its ceiling, or if the lock drifts above the last
   3.8-capable release.

## Source compatibility, and how it stays that way

| Gate | Where |
|---|---|
| 3.8 grammar for every file (`ast` `feature_version`) | `tests/test_python38_compat.py` |
| Banned 3.9+/3.10+/3.11+ stdlib and `typing` names | `tests/test_python38_compat.py` |
| New-style annotations without `from __future__ import annotations` | `tests/test_python38_compat.py` |
| Runtime subscripts/unions outside annotations (`list[int]`, `X \| None`) | `tests/test_python38_compat.py` |
| `MINIMUM_PYTHON` matches `requires-python` | `tests/test_python38_compat.py` |
| Dependency ceilings and the 3.8 lock | `tests/test_python38_compat.py` |
| Independent minimum-version scan | `vermin -t=3.8-` in CI |

Two notes on the annotation rules. `forge/api/` is held to a stricter standard
than the rest of the tree: FastAPI and pydantic *evaluate* endpoint and field
annotations at import time, so `X | Y` or `list[X]` there is a `TypeError` on
3.8 even with the future import — that layer must spell them
`Optional`/`List`. And the runtime-construct check is deliberately narrow: it
flags `X | None` and `list[...] | Y` but not a bare `Name | Name`, because that
cannot be told apart from a real bitwise OR (regex flags, set unions and chmod
mode bits all occur in this codebase).

## Windows portability

Windows defines neither `signal.SIGKILL` nor `os.killpg`, and the
`AttributeError` they raise is not caught by the `except OSError` handlers that
normally surround process management — so the obvious POSIX spellings crash
rather than returning a structured failure. All platform-specific signalling
goes through **`forge/core/portability.py`**:

* `stop_signal(op)` — returns a signal the platform actually defines plus an
  honest label. On Windows both `terminate` and `kill` map to `SIGTERM`, which
  `os.kill` delivers as `TerminateProcess`; there is no graceful terminate
  through `os.kill` on Windows and Forge does not pretend otherwise.
* `terminate_process_group(proc)` — signals the child's process group only when
  the child *leads* that group (which is what `start_new_session=True` gives
  the ssh client in `forge/compute/remote.py`). Signalling a group the child
  merely belongs to would kill the caller, so in that case it falls back to
  `terminate()` / `kill()` on the process itself.

`tests/test_portability.py` exercises both paths — including the Windows branch,
by forcing the capability flags off — and statically fails any module outside
the helper that names a Windows-absent `signal` or `os` member. That static
check runs on Linux, so CI catches a regression without needing a Windows
runner.

## The desktop bundle

The desktop app (`forge/desktop_app`, launched by `launch_desktop.py`) reaches
`PyYAML` for configuration and the optional, `try`-guarded `modal` backend — and
nothing else third-party. Critically it never reaches `pydantic`, which only
`forge/api/schemas.py` imports.

That matters because **`pydantic_core` is a Rust extension, and Rust 1.78
(2024-05-02) raised the `*-pc-windows-msvc` targets — `i686` included — to
require Windows 10.** Its cp38 win32 wheel is therefore not expected to load on
Windows 7. `scripts/build_desktop.py` excludes `fastapi`, `starlette`,
`uvicorn`, `pydantic`, `pydantic_core` and `modal` by default so the shipped
executable carries no Windows 10 floor; `--full` puts them back.
`tests/test_build_desktop.py` asserts the exclusion is safe by re-walking the
import graph, so it cannot silently become a broken build.

## Known limitations (stated, not hidden)

* **PyInstaller** declares `<3.16,>=3.8` so 3.8 builds are supported, and it
  ships 32-bit Windows bootloaders — but its own documentation says it "should
  work on Windows 7 or newer" while only *officially* supporting Windows 8+.
  Verify on the actual machine.
* **TLS 1.2** must be enabled on Windows 7 for the Ollama/OpenAI HTTPS calls to
  succeed. It is supported but not always enabled by default.
* **Four test modules need a POSIX shell** (`tests/test_a32_gates.py`,
  `tests/test_a48_compute_security.py`, `tests/test_desktop_local_provider.py`,
  `tests/test_media_blender.py` — `#!/bin/sh` fixtures, `sleep`, `chmod +x`) and
  do not run on Windows. They are covered by the Linux CI legs; the Windows job
  is a smoke gate and says so in the workflow.
* **2 GiB address space** on 32-bit. Forge's output and text caps are what keep
  this safe; do not raise them casually.
* Python 3.8 itself is past upstream end-of-life (October 2024) and receives no
  further security fixes. Supporting it is a compatibility decision for this
  target, not a claim that it is a maintained runtime.
