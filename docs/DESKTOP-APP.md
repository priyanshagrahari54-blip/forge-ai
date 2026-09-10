# Forge AI Desktop (Python, v1.0.0)

A native desktop app for Forge AI — Windows, macOS, and Linux — written in
Python with stdlib Tkinter. **Zero extra dependencies**: if Python runs,
the app runs. No server, no browser, no port to configure.

> **Python version note.** There is no "Python 8" — CPython releases go
> 3.9 → 3.10 → 3.11 → 3.12 → 3.13. Forge (including this app) requires
> **Python 3.11+** (`pyproject.toml`). On Windows install Python 3.11 or
> newer from python.org and tick "Add python.exe to PATH".

This package (`forge/desktop_app/`) is the *application you run*. It is
separate from `forge/desktop/`, which is the *desktop-control agent*
(Forge driving the OS desktop).

## Launch

```bash
forge desktop                        # after pip install -e .
python -m forge.desktop_app          # same, no install needed
python launch_desktop.py             # double-clickable launcher

# With projects pre-registered:
forge desktop --project demo=C:/code/demo --db C:/forge/desktop.db
```

On first run the app asks for a project folder. Everything else —
tasks, approvals, models — lives in the window.

Headless machine (no display / no tkinter)? Use the equivalents:

```bash
forge serve    # browser cockpit on http://127.0.0.1:8000
forge run "add a hello endpoint" --approve
forge doctor   # diagnose the model layer
```

## What you get

* **Run tasks** — type a requirement, pick a mode (`assisted` default,
  `autonomous`, `safe`), press Run. Pause / resume / cancel / retry from
  the task list.
* **Live log** — every stage, test, repair, and gate streams into the log
  tab as the pipeline runs.
* **Approvals that can't be missed** — in `assisted` mode each write pops
  an orange "Needs your approval" panel with Approve / Deny. A task in
  `WAITING_APPROVAL` is paused, not stuck.
* **Files & report** — double-click a changed file to view it (bounded,
  secret-redacted); the Report tab shows the outcome or the error.
* **Model readiness pill** — green `READY: ...` when a code model can
  serve tasks, red `NO MODEL - tasks will fail` when not. `Models >
  Model readiness` runs Doctor; the Setup guide walks through Ollama.

## Connecting a model (required before tasks can succeed)

Forge refuses to fake code: with no model, tasks fail honestly. Pick one:

**Ollama (free, local, recommended)**

1. Install from <https://ollama.com>, then `ollama serve`
2. `ollama pull llama3.2` (or set `OLLAMA_MODEL` to what you pulled)
3. Models > Model readiness → should say READY

**OpenAI (paid, optional)** — set `OPENAI_API_KEY`, restart the app.

Configuration sources (same as `forge run`): `.forge/models.yaml` or
`.forge/models.json` in the working directory, layered over `OLLAMA_URL`,
`OLLAMA_MODEL`, `OPENAI_API_KEY`, `OPENAI_MODEL`.

## Architecture

```text
Tk window (forge/desktop_app/app.py)      <- main thread only (widgets)
   |  queue.Queue snapshots / decisions
poll thread  ->  DesktopBackend (backend.py)  ->  ControlPlane (in-process)
```

* `backend.py` is headless and fully tested (`tests/test_desktop_app_backend.py`):
  projects, sessions, tasks, approvals, readiness, bounded file reads,
  and `poll_snapshot()` which never raises (per-section errors included).
* `app.py` is a thin view: it never touches the ControlPlane directly.
  Widget paths are exercised with a stub-tkinter suite
  (`tests/test_desktop_app_gui_stub.py`).
* Closing the window cancels in-flight tasks (bounded wait) and stops the
  plane — quitting never hangs on an approval timeout.

## Building a distributable (.exe / .app)

Build **on the OS you ship for**. Install PyInstaller once:

```bash
pip install pyinstaller
python scripts/build_desktop.py --check     # preview the command
python scripts/build_desktop.py             # dist/ForgeDesktop/ (one folder)
python scripts/build_desktop.py --onefile   # single ForgeDesktop.exe (Windows)
```

Ship the whole `dist/ForgeDesktop` folder (or the single file). The
target machine needs no Python — but it still needs a model (Ollama or an
API key) for tasks to succeed.

## Troubleshooting

| Symptom | Cause → fix |
|---|---|
| Red `NO MODEL` pill / every task fails | No reachable model → Setup guide: `ollama serve` + `ollama pull llama3.2`, or set `OPENAI_API_KEY` |
| Task sits in `WAITING_APPROVAL` | Assisted mode is waiting on you → Approve/Deny in the orange panel |
| `Cannot open the desktop window` | No display or no tkinter → install python3-tk (Linux) or use `forge serve` / `forge run` |
| Window opens but tasks list won't load | Check the status bar; project folder may have moved → re-add it via File > Open project folder |
