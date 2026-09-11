"""Tkinter user interface for the Forge desktop app.

Thin view over :mod:`forge.desktop_app.backend`: widgets never touch the
ControlPlane directly. A background thread polls the backend and hands
snapshots to the main thread through a queue; all widget updates happen in
``_drain_queue`` via ``after()``.

Requires stdlib ``tkinter`` plus a display. On servers without either, use
``forge serve`` (browser cockpit) or ``forge run`` (terminal) instead.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk
from typing import Any

from forge.desktop_app.backend import BackendError, DesktopBackend

APP_TITLE = "Forge AI Desktop"
POLL_SECONDS = 1.2
DRAIN_MS = 300
MAX_LOG_LINES = 3000

STATUS_COLORS = {
    "QUEUED": "#8a6d00",
    "RUNNING": "#0b5fff",
    "PAUSED": "#8a6d00",
    "WAITING_APPROVAL": "#b35400",
    "SUCCEEDED": "#1a7f37",
    "FAILED": "#c0392b",
    "CANCELLED": "#6e7781",
    "ROLLED_BACK": "#6e7781",
}

MODES = ("assisted", "autonomous", "safe")

OLLAMA_URL = "https://ollama.com"


def _slug(text: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in text.strip().lower())
    return (keep.strip("-") or "project")[:32]


def _short(task_id: str) -> str:
    return task_id if len(task_id) <= 18 else task_id[:16] + ".."


class ForgeDesktopApp(tk.Tk):
    """Main window. Owns widgets; the backend owns all Forge state."""

    def __init__(self, backend: DesktopBackend,
                 initial_projects: dict[str, str] | None = None) -> None:
        super().__init__()
        self.backend = backend
        self.title(APP_TITLE)
        self.geometry("1180x760")
        self.minsize(900, 600)

        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._poller: threading.Thread | None = None
        self._tasks: list[dict[str, Any]] = []
        self._task_ids: list[str] = []
        self._selected_task = ""
        self._event_cursor = 0
        self._log_lines = 0
        self._current_project = tk.StringVar()
        self._mode = tk.StringVar(value="assisted")
        self._readiness: dict[str, Any] = {}
        self._debug = False

        self._build_menu()
        self._build_toolbar()
        self._build_main()
        self._build_statusbar()

        projects = dict(initial_projects or {})
        if projects:
            self._start_backend(projects)
        else:
            self.after(200, self._first_run)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- construction --------------------------------------------------------

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Open project folder...",
                              command=self._open_folder)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        models_menu = tk.Menu(menubar, tearoff=False)
        models_menu.add_command(label="Model readiness (Doctor)",
                                command=self._show_doctor_async)
        models_menu.add_command(label="Models & providers",
                                command=self._show_models_async)
        menubar.add_cascade(label="Models", menu=models_menu)

        agents_menu = tk.Menu(menubar, tearoff=False)
        agents_menu.add_command(label="Agent Manager...",
                                command=self._open_agent_manager)
        menubar.add_cascade(label="Agents", menu=agents_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="Setup guide", command=self._show_setup)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.config(menu=menubar)

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill=tk.X)

        ttk.Label(bar, text="Project:").pack(side=tk.LEFT)
        self._project_box = ttk.Combobox(bar, textvariable=self._current_project,
                                         state="readonly", width=22)
        self._project_box.pack(side=tk.LEFT, padx=(4, 12))
        self._project_box.bind("<<ComboboxSelected>>", self._on_project_changed)

        ttk.Label(bar, text="Mode:").pack(side=tk.LEFT)
        ttk.Combobox(bar, textvariable=self._mode, values=list(MODES),
                     state="readonly", width=12).pack(side=tk.LEFT, padx=(4, 12))

        self._ready_pill = tk.Label(bar, text=" starting... ",
                                    bg="#6e7781", fg="white", padx=8, pady=2)
        self._ready_pill.pack(side=tk.LEFT, padx=(0, 12))

        ttk.Button(bar, text="Doctor", command=self._show_doctor_async).pack(side=tk.LEFT)
        ttk.Button(bar, text="Open folder", command=self._open_folder).pack(side=tk.LEFT, padx=(6, 0))

        req = ttk.Frame(self, padding=(8, 0, 8, 6))
        req.pack(fill=tk.X)
        ttk.Label(req, text="Task:").pack(side=tk.LEFT)
        self._requirement = ttk.Entry(req)
        self._requirement.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 6))
        self._requirement.bind("<Return>", lambda _e: self._submit())
        ttk.Button(req, text="Run task", command=self._submit).pack(side=tk.LEFT)

    def _build_main(self) -> None:
        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))

        left = ttk.Frame(paned, padding=4)
        ttk.Label(left, text="Tasks").pack(anchor=tk.W)
        self._task_list = tk.Listbox(left, width=34, activestyle="dotbox")
        self._task_list.pack(fill=tk.BOTH, expand=True, pady=(2, 4))
        self._task_list.bind("<<ListboxSelect>>", self._on_task_selected)
        btns = ttk.Frame(left)
        btns.pack(fill=tk.X)
        for label, action in (("Pause", self._pause_selected),
                              ("Resume", self._resume_selected),
                              ("Cancel", self._cancel_selected),
                              ("Retry", self._retry_selected)):
            ttk.Button(btns, text=label, command=action).pack(side=tk.LEFT, padx=1)
        paned.add(left, weight=1)

        right = ttk.Frame(paned, padding=4)
        self._detail_title = ttk.Label(right, text="No task selected",
                                       font=("", 10, "bold"))
        self._detail_title.pack(anchor=tk.W)
        self._detail_sub = ttk.Label(right, text="", foreground="#555")
        self._detail_sub.pack(anchor=tk.W, pady=(0, 4))

        self._approval_frame = ttk.LabelFrame(right, text="Needs your approval (0)",
                                              padding=6)

        self._notebook = ttk.Notebook(right)
        self._notebook.pack(fill=tk.BOTH, expand=True)

        log_tab = ttk.Frame(self._notebook, padding=4)
        self._log = tk.Text(log_tab, wrap=tk.WORD, state=tk.DISABLED,
                            height=20, bg="#0d1117", fg="#e6edf3",
                            insertbackground="#e6edf3")
        scroll = ttk.Scrollbar(log_tab, command=self._log.yview)
        self._log.configure(yscrollcommand=scroll.set)
        self._log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._notebook.add(log_tab, text="Live log")

        files_tab = ttk.Frame(self._notebook, padding=4)
        self._file_list = tk.Listbox(files_tab, activestyle="dotbox")
        self._file_list.pack(fill=tk.BOTH, expand=True)
        self._file_list.bind("<Double-Button-1>", self._open_selected_file)
        self._notebook.add(files_tab, text="Files")

        report_tab = ttk.Frame(self._notebook, padding=4)
        self._report = tk.Text(report_tab, wrap=tk.WORD, state=tk.DISABLED,
                               height=20, bg="#f6f8fa")
        report_scroll = ttk.Scrollbar(report_tab, command=self._report.yview)
        self._report.configure(yscrollcommand=report_scroll.set)
        self._report.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        report_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._notebook.add(report_tab, text="Report")
        paned.add(right, weight=3)

    def _build_statusbar(self) -> None:
        self._status = ttk.Label(self, text="Not started", relief=tk.SUNKEN,
                                 anchor=tk.W, padding=(6, 2))
        self._status.pack(fill=tk.X)

    # -- lifecycle ------------------------------------------------------------

    def _start_backend(self, projects: dict[str, str]) -> None:
        try:
            started = self.backend.start(projects)
        except BackendError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            self._set_status(f"Start failed: {exc}")
            return
        ids = [p["id"] for p in started]
        self._project_box["values"] = ids
        if ids:
            self._current_project.set(ids[0])
        self._set_status(f"Forge started ({len(ids)} project(s))")
        self._ready_pill.configure(text=" checking models... ", bg="#8a6d00")
        self._begin_polling()
        self._refresh_readiness_async(first=True)

    def _begin_polling(self) -> None:
        self.after(DRAIN_MS, self._drain_queue)
        self._poller = threading.Thread(target=self._poll_loop, daemon=True,
                                        name="forge-desktop-poll")
        self._poller.start()

    def _poll_loop(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                project = self._current_project.get()
                if project and self.backend.running:
                    snapshot = self.backend.poll_snapshot(
                        project, selected_task_id=self._selected_task,
                        event_cursor=self._event_cursor)
                    self._queue.put(("snapshot", snapshot))
                    failures = 0
            except Exception as exc:  # never let the poller die silently
                failures += 1
                if failures <= 3:
                    self._queue.put(("poll_error", str(exc)))
            self._stop.wait(POLL_SECONDS)

    def _drain_queue(self) -> None:
        if self._stop.is_set():
            return
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "snapshot":
                    self._apply_snapshot(payload)
                elif kind == "poll_error":
                    self._set_status(f"Refresh hiccup: {payload}")
                elif kind == "readiness":
                    self._readiness = payload
                    self._render_readiness_pill()
                elif kind == "doctor":
                    self._show_doctor_window(payload)
                elif kind == "models":
                    self._show_models_window(payload)
                elif kind == "notice":
                    self._set_status(str(payload))
                elif kind == "error":
                    messagebox.showerror(APP_TITLE, str(payload))
        except queue.Empty:
            pass
        finally:
            self.after(DRAIN_MS, self._drain_queue)

    def _on_close(self) -> None:
        self._stop.set()
        try:
            self.backend.stop()
        except Exception:
            pass
        self.destroy()

    # -- snapshots -------------------------------------------------------------

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        if snapshot.get("project_id") != self._current_project.get():
            return
        errors = snapshot.get("errors", {})
        if errors.get("tasks"):
            self._set_status(f"Tasks: {errors['tasks']}")
            return
        self._render_tasks(snapshot.get("tasks", []))
        self._render_approvals(snapshot.get("approvals", []))
        selected = snapshot.get("selected")
        if selected:
            self._render_selected(selected)
        for event in snapshot.get("events", []):
            self._append_event(event)
        self._event_cursor = snapshot.get("event_cursor", self._event_cursor)

    def _render_tasks(self, tasks: list[dict[str, Any]]) -> None:
        ids = [t["id"] for t in tasks]
        if ids == self._task_ids:
            # Same membership: refresh labels in place (status changes).
            for index, task in enumerate(tasks):
                self._task_list.delete(index)
                self._task_list.insert(index, self._task_label(task))
                self._task_list.itemconfig(index, fg=self._status_color(task))
            self._tasks = tasks
            self._restore_selection()
            return
        self._tasks = tasks
        self._task_ids = ids
        self._task_list.delete(0, tk.END)
        for task in tasks:
            self._task_list.insert(tk.END, self._task_label(task))
            self._task_list.itemconfig(tk.END, fg=self._status_color(task))
        self._restore_selection()

    @staticmethod
    def _task_label(task: dict[str, Any]) -> str:
        req = (task.get("requirement") or "")[:42]
        return f"[{task.get('status', '?')}] {_short(task.get('id', ''))} {req}"

    @staticmethod
    def _status_color(task: dict[str, Any]) -> str:
        return STATUS_COLORS.get(task.get("status", ""), "#000000")

    def _restore_selection(self) -> None:
        if self._selected_task and self._selected_task in self._task_ids:
            index = self._task_ids.index(self._selected_task)
            self._task_list.selection_clear(0, tk.END)
            self._task_list.selection_set(index)

    def _render_selected(self, task: dict[str, Any]) -> None:
        title = f"{_short(task['id'])} - {task.get('status')}"
        self._detail_title.configure(text=title)
        sub = (f"stage={task.get('stage', '-')}"
               f"  mode={task.get('mode', '-')}"
               f"  model={task.get('model') or '-'}"
               f"  provider={task.get('provider') or '-'}")
        self._detail_sub.configure(text=sub)
        self._file_list.delete(0, tk.END)
        for path in task.get("files", []):
            self._file_list.insert(tk.END, path)
        error = task.get("error") or ""
        if error and task.get("status") in ("FAILED", "CANCELLED", "ROLLED_BACK"):
            self._set_report_text(f"ERROR\n{error}\n")
        elif task.get("status") == "SUCCEEDED":
            files = "\n".join(f"  - {p}" for p in task.get("files", [])) or "  (none)"
            self._set_report_text(f"SUCCEEDED\nChanged files:\n{files}\n")
        self._set_status(f"{title} ({task.get('stage', '')})")

    def _render_approvals(self, approvals: list[dict[str, Any]]) -> None:
        for child in self._approval_frame.winfo_children():
            child.destroy()
        if not approvals:
            self._approval_frame.pack_forget()
            return
        self._approval_frame.configure(text=f"Needs your approval ({len(approvals)})")
        # Place above the notebook.
        self._approval_frame.pack(fill=tk.X, pady=(0, 4), before=self._notebook)
        for item in approvals[:5]:
            row = ttk.Frame(self._approval_frame)
            row.pack(fill=tk.X, pady=1)
            label = item.get("label") or item.get("operation") or "approval"
            reason = item.get("reason") or ""
            text = f"{label} - {reason}"[:110] if reason else str(label)[:110]
            ttk.Label(row, text=text).pack(side=tk.LEFT, fill=tk.X, expand=True)
            approval_id = item.get("id", "")
            ttk.Button(row, text="Approve",
                       command=lambda i=approval_id: self._decide(i, True)).pack(side=tk.RIGHT, padx=2)
            ttk.Button(row, text="Deny",
                       command=lambda i=approval_id: self._decide(i, False)).pack(side=tk.RIGHT)

    def _append_event(self, event: dict[str, Any]) -> None:
        etype = event.get("event_type", event.get("type", "event"))
        data = event.get("data", {})
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                pass
        summary = ""
        if isinstance(data, dict):
            for key in ("stage", "status", "error", "files", "detail",
                        "checkpoint_id", "model"):
                if data.get(key):
                    value = data[key]
                    summary = f"{key}={value}" if not isinstance(value, list) else f"{key}={len(value)} files"
                    break
        stamp = time.strftime("%H:%M:%S")
        line = f"{stamp}  {etype}  {summary}".rstrip() + "\n"
        self._log.configure(state=tk.NORMAL)
        self._log.insert(tk.END, line)
        self._log_lines += 1
        if self._log_lines > MAX_LOG_LINES:
            self._log.delete("1.0", f"{self._log_lines - MAX_LOG_LINES}.0")
            self._log_lines = MAX_LOG_LINES
        self._log.see(tk.END)
        self._log.configure(state=tk.DISABLED)

    def _set_report_text(self, text: str) -> None:
        self._report.configure(state=tk.NORMAL)
        self._report.delete("1.0", tk.END)
        self._report.insert(tk.END, text)
        self._report.configure(state=tk.DISABLED)

    def _set_status(self, text: str) -> None:
        self._status.configure(text=text[:220])

    # -- actions -----------------------------------------------------------------

    def _current_project_id(self) -> str:
        return self._current_project.get()

    def _submit(self) -> None:
        project = self._current_project_id()
        requirement = self._requirement.get().strip()
        if not project:
            messagebox.showinfo(APP_TITLE, "Open a project folder first (File menu).")
            return
        if not requirement:
            messagebox.showinfo(APP_TITLE, "Describe the task first.")
            return
        try:
            run = self.backend.submit_task(project, requirement, mode=self._mode.get())
        except BackendError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self._requirement.delete(0, tk.END)
        self._selected_task = run["id"]
        self._event_cursor = 0
        self._log.configure(state=tk.NORMAL)
        self._log.delete("1.0", tk.END)
        self._log.configure(state=tk.DISABLED)
        self._log_lines = 0
        self._set_report_text("")
        self._set_status(f"Task {run['id']} submitted ({run['status']})")

    def _mutate_selected(self, operation: str, verb: str) -> None:
        project = self._current_project_id()
        if not self._selected_task:
            messagebox.showinfo(APP_TITLE, "Select a task first.")
            return
        try:
            run = getattr(self.backend, f"{operation}_task")(project, self._selected_task)
        except BackendError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        if operation == "retry":
            self._selected_task = run["id"]
            self._event_cursor = 0
        self._set_status(f"{verb}: {run['id']} -> {run['status']}")

    def _pause_selected(self) -> None:
        self._mutate_selected("pause", "Pause requested")

    def _resume_selected(self) -> None:
        self._mutate_selected("resume", "Resumed")

    def _cancel_selected(self) -> None:
        if messagebox.askyesno(APP_TITLE, "Cancel the selected task?"):
            self._mutate_selected("cancel", "Cancelled")

    def _retry_selected(self) -> None:
        self._mutate_selected("retry", "Retried as")

    def _decide(self, approval_id: str, approved: bool) -> None:
        project = self._current_project_id()
        try:
            self.backend.decide_approval(project, approval_id, approved)
        except BackendError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self._set_status(f"Approval {approval_id[:8]}... "
                         f"{'approved' if approved else 'denied'}")

    def _on_task_selected(self, _event=None) -> None:
        selection = self._task_list.curselection()
        if not selection:
            return
        task_id = self._task_ids[selection[0]]
        if task_id != self._selected_task:
            self._selected_task = task_id
            self._event_cursor = 0
            self._log.configure(state=tk.NORMAL)
            self._log.delete("1.0", tk.END)
            self._log.configure(state=tk.DISABLED)
            self._log_lines = 0
            self._set_report_text("")

    def _on_project_changed(self, _event=None) -> None:
        self._selected_task = ""
        self._event_cursor = 0
        self._task_ids = []
        self._set_report_text("")

    def _open_folder(self) -> None:
        folder = filedialog.askdirectory(title="Open project folder")
        if not folder:
            return
        project_id = _slug(folder.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
        if not self.backend.running:
            self._start_backend({project_id: folder})
            return
        try:
            project = self.backend.add_project(project_id, folder)
        except BackendError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        values = list(self._project_box["values"]) + [project["id"]]
        self._project_box["values"] = values
        self._current_project.set(project["id"])
        self._on_project_changed()
        self._set_status(f"Project added: {project['id']}")

    def _open_selected_file(self, _event=None) -> None:
        selection = self._file_list.curselection()
        if not selection:
            return
        rel_path = self._file_list.get(selection[0])
        try:
            data = self.backend.read_project_file(self._current_project_id(), rel_path)
        except BackendError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        viewer = tk.Toplevel(self)
        viewer.title(rel_path)
        viewer.geometry("800x600")
        text = tk.Text(viewer, wrap=tk.NONE, bg="#0d1117", fg="#e6edf3",
                       insertbackground="#e6edf3")
        scroll = ttk.Scrollbar(viewer, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        body = data["content"] + ("\n\n[... truncated ...]" if data["truncated"] else "")
        text.insert(tk.END, body)
        text.configure(state=tk.DISABLED)

    # -- readiness / models ---------------------------------------------------------

    def _refresh_readiness_async(self, first: bool = False) -> None:
        def work() -> None:
            try:
                readiness = self.backend.model_readiness()
            except BackendError as exc:
                self._queue.put(("notice", f"Readiness check failed: {exc}"))
                return
            self._queue.put(("readiness", readiness))
            if first and not readiness.get("ready"):
                self._queue.put(("notice", "No working model - see Models > Setup guide"))
        threading.Thread(target=work, daemon=True).start()

    def _render_readiness_pill(self) -> None:
        if self._readiness.get("ready"):
            models = ", ".join(self._readiness.get("usable_models", [])) or "ready"
            self._ready_pill.configure(text=f" READY: {models[:60]} ", bg="#1a7f37")
        else:
            self._ready_pill.configure(text=" NO MODEL - tasks will fail ", bg="#c0392b")

    def _show_doctor_async(self) -> None:
        if not self.backend.running:
            messagebox.showinfo(APP_TITLE, "Start the backend first (open a project folder).")
            return

        def work() -> None:
            try:
                readiness = self.backend.model_readiness()
            except BackendError as exc:
                self._queue.put(("error", str(exc)))
                return
            self._queue.put(("readiness", readiness))
            self._queue.put(("doctor", readiness))

        threading.Thread(target=work, daemon=True).start()

    def _show_doctor_window(self, readiness: dict[str, Any]) -> None:
        window = tk.Toplevel(self)
        window.title("Forge Doctor - model readiness")
        window.geometry("640x480")
        text = tk.Text(window, wrap=tk.WORD, padx=8, pady=8)
        text.pack(fill=tk.BOTH, expand=True)
        lines = []
        if readiness.get("ready"):
            lines.append("READY - these models can serve tasks:")
            for name in readiness.get("usable_models", []):
                lines.append(f"  - {name}")
        else:
            lines.append("NOT READY - tasks cannot produce code yet.")
        lines.append("")
        for check in readiness.get("checks", []):
            mark = "ok" if check.get("ok") else "FAIL"
            lines.append(f"[{mark}] {check.get('name')}: {check.get('detail', '')}")
            if not check.get("ok") and check.get("remediation"):
                lines.append(f"       fix: {check.get('remediation')}")
            lines.append("")
        text.insert(tk.END, "\n".join(lines))
        text.configure(state=tk.DISABLED)
        ttk.Button(window, text="Setup guide", command=self._show_setup).pack(pady=(0, 8))

    def _show_models_async(self) -> None:
        if not self.backend.running:
            messagebox.showinfo(APP_TITLE, "Start the backend first (open a project folder).")
            return

        def work() -> None:
            try:
                state = self.backend.model_state()
            except BackendError as exc:
                self._queue.put(("error", str(exc)))
                return
            self._queue.put(("models", state))

        threading.Thread(target=work, daemon=True).start()

    def _show_models_window(self, state: dict[str, Any]) -> None:
        window = tk.Toplevel(self)
        window.title("Models & providers")
        window.geometry("640x480")
        text = tk.Text(window, wrap=tk.WORD, padx=8, pady=8)
        text.pack(fill=tk.BOTH, expand=True)
        lines = ["MODELS"]
        for model in state.get("models", []):
            if isinstance(model, dict):
                lines.append(f"  {model.get('name')} (provider={model.get('provider')}"
                             f" free={model.get('free')} local={model.get('local')})")
            else:
                lines.append(f"  {model}")
        lines.append("")
        lines.append("PROVIDERS")
        for provider in state.get("providers", []):
            if isinstance(provider, dict):
                lines.append(f"  {provider.get('name')}: kind={provider.get('kind')}"
                             f" model={provider.get('model') or '-'}")
            else:
                lines.append(f"  {provider}")
        lines.append("")
        lines.append("HEALTH")
        for name, info in state.get("health", {}).items():
            lines.append(f"  {name}: {info}")
        text.insert(tk.END, "\n".join(lines))
        text.configure(state=tk.DISABLED)

    # -- agent manager ---------------------------------------------------------

    def _open_agent_manager(self) -> None:
        if not self.backend.running:
            messagebox.showinfo(APP_TITLE, "Start the backend first (open a project folder).")
            return
        project_id = self._current_project_id()
        if not project_id:
            messagebox.showinfo(APP_TITLE, "Select a project first.")
            return
        existing = getattr(self, "_agent_manager", None)
        if existing is not None:
            try:
                existing.lift()
                existing.focus_force()
                return
            except tk.TclError:
                self._agent_manager = None
        self._agent_manager = AgentManagerWindow(self, self.backend,
                                                 project_id)

    # -- dialogs ----------------------------------------------------------------------

    def _first_run(self) -> None:
        if self.backend.running:
            return
        if messagebox.askyesno(APP_TITLE,
                               "Welcome to Forge AI Desktop!\n\n"
                               "Open a project folder to get started?"):
            self._open_folder()
        else:
            self._show_setup()

    def _show_setup(self) -> None:
        window = tk.Toplevel(self)
        window.title("Setup guide - connect a model")
        window.geometry("620x480")
        text = tk.Text(window, wrap=tk.WORD, padx=10, pady=10)
        text.pack(fill=tk.BOTH, expand=True)
        guide = (
            "Forge needs a real code model before tasks can succeed.\n"
            "Without one, every task fails honestly instead of faking code.\n\n"
            "OPTION 1 - Ollama (free, local, recommended)\n"
            "  1. Install Ollama from https://ollama.com\n"
            "  2. Run:  ollama serve\n"
            "  3. Run:  ollama pull llama3.2\n"
            "  4. Back here: Models > Model readiness - it should say READY.\n\n"
            "OPTION 2 - OpenAI (paid, remote, optional)\n"
            "  1. Set the OPENAI_API_KEY environment variable.\n"
            "  2. Restart Forge Desktop.\n\n"
            "MODES\n"
            "  assisted   - every file write asks your approval (default).\n"
            "  autonomous - low-risk writes auto-approve; sensitive ones still ask.\n"
            "  safe       - read/analyze only; nothing is modified.\n\n"
            "Approvals appear in the orange panel above the log. A task that\n"
            "says WAITING_APPROVAL is paused until you Approve or Deny it."
        )
        text.insert(tk.END, guide)
        text.configure(state=tk.DISABLED)
        row = ttk.Frame(window)
        row.pack(pady=(0, 8))
        ttk.Button(row, text="Open ollama.com",
                   command=lambda: webbrowser.open(OLLAMA_URL)).pack(side=tk.LEFT, padx=4)
        ttk.Button(row, text="Check readiness",
                   command=self._show_doctor_async).pack(side=tk.LEFT, padx=4)

    def _show_about(self) -> None:
        from forge.desktop_app import __version__

        messagebox.showinfo(APP_TITLE,
                            f"Forge AI Desktop v{__version__}\n\n"
                            "Native Python desktop app for Forge AI.\n"
                            "No server, no browser, no extra dependencies.\n\n"
                            "Tasks run through the same guarded pipeline\n"
                            "as the browser cockpit: plan, code, test,\n"
                            "review, acceptance - with approvals for writes.")


class AgentManagerWindow(tk.Toplevel):
    """Agent Manager: create, benchmark, and lifecycle created agents.

    Thin view over ``DesktopBackend.agents_*``: every backend call runs in
    a worker thread and the result is applied on the main thread via
    ``after()``. Creation grants nothing — grants are recorded by the
    operator through the backend, never by an agent.
    """

    OPERATIONS = ("validate", "test", "enable", "pause", "resume",
                  "disable", "retire")

    def __init__(self, parent: tk.Tk, backend: DesktopBackend,
                 project_id: str) -> None:
        super().__init__(parent)
        self.backend = backend
        self.project_id = project_id
        self.title(f"Agent Manager - {project_id}")
        self.geometry("760x560")
        self.minsize(620, 460)
        self._agents: list[dict[str, Any]] = []
        self._templates: list[dict[str, Any]] = []

        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)
        ttk.Label(top, text="Template:").pack(side=tk.LEFT)
        self._template = tk.StringVar()
        self._template_box = ttk.Combobox(top,
                                          textvariable=self._template,
                                          state="readonly", width=18)
        self._template_box.pack(side=tk.LEFT, padx=(4, 8))
        ttk.Label(top, text="Name:").pack(side=tk.LEFT)
        self._name = tk.StringVar()
        ttk.Entry(top, textvariable=self._name, width=22).pack(
            side=tk.LEFT, padx=(4, 8))
        ttk.Button(top, text="Create",
                   command=self._create_async).pack(side=tk.LEFT)

        middle = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        middle.pack(fill=tk.BOTH, expand=True, padx=8)
        left = ttk.Frame(middle)
        self._list = tk.Listbox(left, activestyle="dotbox", width=34)
        self._list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._list.bind("<<ListboxSelect>>", self._on_selected)
        scroll = ttk.Scrollbar(left, command=self._list.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._list.configure(yscrollcommand=scroll.set)
        middle.add(left, weight=1)
        right = ttk.Frame(middle)
        self._details = tk.Text(right, wrap=tk.WORD, state=tk.DISABLED,
                                width=40)
        self._details.pack(fill=tk.BOTH, expand=True)
        middle.add(right, weight=2)

        buttons = ttk.Frame(self, padding=8)
        buttons.pack(fill=tk.X)
        for operation in self.OPERATIONS:
            ttk.Button(buttons, text=operation.capitalize(),
                       command=lambda op=operation: self._operate_async(
                           op)).pack(side=tk.LEFT, padx=2)
        ttk.Button(buttons, text="Refresh",
                   command=self._refresh_async).pack(side=tk.LEFT,
                                                     padx=(8, 2))

        self._status = tk.StringVar(value="Loading agents...")
        ttk.Label(self, textvariable=self._status,
                  padding=(8, 0)).pack(fill=tk.X)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._refresh_async()

    # -- worker plumbing -------------------------------------------------

    def _run_async(self, label: str, call) -> None:
        self._status.set(f"{label}...")

        def work() -> None:
            try:
                result = call()
            except BackendError as exc:
                self.after(0, lambda: self._failed(label, str(exc)))
                return
            except Exception as exc:  # never strand the UI thread
                self.after(0, lambda: self._failed(label, str(exc)))
                return
            self.after(0, lambda: self._done(label, result))

        threading.Thread(target=work, daemon=True).start()

    def _failed(self, label: str, error: str) -> None:
        self._status.set(f"{label} failed: {error}")
        messagebox.showerror(APP_TITLE, f"{label} failed:\n{error}", parent=self)

    def _done(self, label: str, result: Any) -> None:
        if isinstance(result, dict) and "agents" in result \
                and "templates" in result:
            self._agents = result["agents"]
            self._templates = result["templates"]
            self._template_box.configure(
                values=[t["id"] for t in self._templates])
            if self._templates and not self._template.get():
                self._template.set(self._templates[0]["id"])
            self._render_list()
            self._status.set(f"{len(self._agents)} agent(s) in "
                             f"{self.project_id}")
        else:
            self._show_result(label, result)
            self._refresh_async(silent=True)

    # -- actions ----------------------------------------------------------

    def _refresh_async(self, silent: bool = False) -> None:
        def call():
            return {"agents": self.backend.agents_list(self.project_id),
                    "templates": self.backend.agents_templates()}

        if silent:
            def work() -> None:
                try:
                    result = call()
                except Exception:
                    return
                self.after(0, lambda: self._done("Refresh", result))

            threading.Thread(target=work, daemon=True).start()
        else:
            self._run_async("Refresh", call)

    def _selected_name(self) -> str:
        selection = self._list.curselection()
        if not selection or selection[0] >= len(self._agents):
            return ""
        return str(self._agents[selection[0]].get("name", ""))

    def _create_async(self) -> None:
        template = self._template.get().strip()
        name = self._name.get().strip()
        if not template or not name:
            messagebox.showinfo(APP_TITLE, "Pick a template and a name first.",
                                parent=self)
            return
        self._run_async(
            "Create",
            lambda: self.backend.agents_create(self.project_id, template,
                                               name))

    def _operate_async(self, operation: str) -> None:
        name = self._selected_name()
        if not name:
            messagebox.showinfo(APP_TITLE, "Select an agent first.",
                                parent=self)
            return
        action = getattr(self.backend, "agents_%s" % operation)
        self._run_async(operation.capitalize(),
                         lambda: action(self.project_id, name))

    def _on_selected(self, _event=None) -> None:
        name = self._selected_name()
        if not name:
            return

        def work() -> None:
            try:
                package = self.backend.agents_show(self.project_id, name)
            except Exception as exc:
                self.after(0, lambda: self._status.set(str(exc)))
                return
            self.after(0, lambda: self._show_result("Agent", package))

        threading.Thread(target=work, daemon=True).start()

    # -- rendering --------------------------------------------------------

    def _render_list(self) -> None:
        self._list.delete(0, tk.END)
        for agent in self._agents:
            self._list.insert(tk.END,
                              f"{agent.get('name')} "
                              f"v{agent.get('version')} "
                              f"[{agent.get('state')}]")

    def _show_result(self, label: str, result: Any) -> None:
        self._details.configure(state=tk.NORMAL)
        self._details.delete("1.0", tk.END)
        self._details.insert(tk.END, f"{label}\n\n"
                             + json.dumps(result, indent=2, default=str))
        self._details.configure(state=tk.DISABLED)
        if isinstance(result, dict) and result.get("name"):
            self._status.set(f"{label}: {result['name']} "
                             f"[{result.get('state', '?')}]")
        else:
            self._status.set(f"{label} finished")


def launch(projects: dict[str, str] | None = None,
           db_path: str = "", actor: str = "desktop",
           fabric: Any = None, policy: Any = None) -> None:
    """Start the desktop app (blocks until the window closes)."""
    backend = DesktopBackend(actor=actor, db_path=db_path, fabric=fabric,
                             policy=policy)
    app = ForgeDesktopApp(backend, initial_projects=projects)
    app.mainloop()


def main(argv: list[str] | None = None) -> int:
    """CLI entry for ``python -m forge.desktop_app``."""
    import argparse

    parser = argparse.ArgumentParser(prog="forge-desktop",
                                     description="Forge AI Desktop app")
    parser.add_argument("--project", dest="projects", action="append",
                        default=[], metavar="ID=ROOT",
                        help="Register a project (repeatable)")
    parser.add_argument("--db", default="", help="Database path")
    parser.add_argument("--actor", default="desktop")
    args = parser.parse_args(argv)

    projects: dict[str, str] = {}
    for spec in args.projects:
        name, _, root = spec.partition("=")
        if not name or not root:
            parser.error("--project must look like ID=ROOT")
        projects[name] = root
    try:
        launch(projects or None, db_path=args.db, actor=args.actor)
    except tk.TclError as exc:
        print(f"Cannot open the desktop window: {exc}\n"
              f"Headless machine? Use `forge serve` (browser) or "
              f"`forge run` (terminal) instead.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
