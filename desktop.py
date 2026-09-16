"""Forge AI Desktop — zero-setup thin client for the Forge Server.

Double-click this file (or run ``python desktop.py``).  The desktop is only a
thin client: model weights and inference stay on Forge Server.  When the
configured local server is not already running, this launcher starts one in
the background, creates/loads its local bootstrap token, and then discovers a
real usable model through the server Model Fabric.

Environment variables (optional):
  FORGE_SERVER_URL  Server URL, default http://127.0.0.1:8300
  FORGE_API_KEY     API key/session credential for an existing server
  FORGE_SERVER_TOKEN Bootstrap token for an existing server
  FORGE_SERVER_DB   Server database/token location
  FORGE_MODEL       Optional exact model id/name to select

The only GUI dependency is Tkinter. Forge's normal Python dependencies are
used for the embedded local server when it needs to be started.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from forge.server.client import ForgeServerClient, ForgeServerClientError


DEFAULT_SERVER = os.environ.get("FORGE_SERVER_URL", "http://127.0.0.1:8300")
DEFAULT_CREDENTIAL = (
    os.environ.get("FORGE_API_KEY", "").strip()
    or os.environ.get("FORGE_SERVER_TOKEN", "").strip()
)
DEFAULT_MODEL = os.environ.get("FORGE_MODEL", "").strip()
DEFAULT_DB = os.environ.get("FORGE_SERVER_DB", ".forge/server/server.db")


class DesktopStartupError(Exception):
    """A clean, user-facing desktop startup failure."""


def _base_host_port(server_url: str) -> Tuple[str, int]:
    parsed = urlparse(server_url if "://" in server_url else "http://" + server_url)
    return parsed.hostname or "127.0.0.1", int(parsed.port or 8300)


def _is_local_server(server_url: str) -> bool:
    host, _ = _base_host_port(server_url)
    return host in {"127.0.0.1", "localhost", "::1"}


def _server_db_path() -> Path:
    return Path(DEFAULT_DB).expanduser()


def _token_path() -> Path:
    return _server_db_path().parent / "token"


def _read_token() -> str:
    if DEFAULT_CREDENTIAL:
        return DEFAULT_CREDENTIAL
    try:
        path = _token_path()
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def _write_token(token: str) -> None:
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        os.chmod(str(path), 0o600)
    except (OSError, AttributeError):
        pass


def _model_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = payload.get("models", payload.get("items", []))
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _model_identity(row: Dict[str, Any]) -> str:
    return str(
        row.get("model_id")
        or row.get("name")
        or row.get("id")
        or ""
    )


def discover_model(client: ForgeServerClient) -> Dict[str, Any]:
    """Discover only models the server itself says are usable."""
    requested = DEFAULT_MODEL
    payload = client.list_models(discover=True, usable_only=True)
    rows = _model_rows(payload)

    if requested:
        for row in rows:
            candidates = {
                _model_identity(row),
                str(row.get("name", "")),
            }
            if requested in candidates:
                return row
        raise ForgeServerClientError(
            "MODEL_NOT_FOUND",
            "Configured model '%s' is not currently usable on Forge Server."
            % requested,
        )

    if rows:
        return rows[0]

    raise ForgeServerClientError(
        "NO_MODEL",
        "Forge Server is reachable, but no configured and verified model is usable. "
        "Configure a provider/model on the server, then reconnect.",
    )


class ForgeDesktop(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Forge AI — Desktop")
        self.geometry("700x410")
        self.minsize(560, 340)
        self._connecting = False
        self._owned_server: Optional[Any] = None

        self.server_var = tk.StringVar(value=DEFAULT_SERVER)
        self.credential_var = tk.StringVar(value=DEFAULT_CREDENTIAL, show="*")
        self.status_var = tk.StringVar(value="Starting…")
        self.model_var = tk.StringVar(value="—")
        self.provider_var = tk.StringVar(value="—")
        self.backend_var = tk.StringVar(value="—")
        self.auto_var = tk.BooleanVar(value=True)

        frame = tk.Frame(self, padx=20, pady=18)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="Forge AI", font=("Segoe UI", 22, "bold")).pack(anchor="w")
        tk.Label(
            frame,
            text="Thin desktop client • execution stays on Forge Server",
            fg="#666",
        ).pack(anchor="w", pady=(0, 16))

        row = tk.Frame(frame); row.pack(fill="x", pady=4)
        tk.Label(row, text="Server", width=14, anchor="w").pack(side="left")
        tk.Entry(row, textvariable=self.server_var).pack(side="left", fill="x", expand=True)

        row = tk.Frame(frame); row.pack(fill="x", pady=4)
        tk.Label(row, text="Credential", width=14, anchor="w").pack(side="left")
        tk.Entry(row, textvariable=self.credential_var, show="*").pack(
            side="left", fill="x", expand=True
        )

        tk.Checkbutton(
            frame,
            text="Automatically start the local Forge Server when it is not running",
            variable=self.auto_var,
        ).pack(anchor="w", pady=(6, 4))

        self.connect_button = tk.Button(
            frame, text="Connect & Discover Model", command=self.connect, height=2
        )
        self.connect_button.pack(fill="x", pady=(10, 18))

        info = tk.Frame(frame); info.pack(fill="x")
        labels = [
            ("Connection:", self.status_var),
            ("Model:", self.model_var),
            ("Provider:", self.provider_var),
            ("Backend:", self.backend_var),
        ]
        for index, (label, variable) in enumerate(labels):
            tk.Label(info, text=label, width=14, anchor="w").grid(
                row=index, column=0, sticky="w", pady=2
            )
            tk.Label(info, textvariable=variable, anchor="w").grid(
                row=index, column=1, sticky="w", pady=2
            )
        info.columnconfigure(1, weight=1)

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(250, self.connect)

    def connect(self) -> None:
        if self._connecting:
            return
        server_url = self.server_var.get().strip()
        credential = self.credential_var.get().strip()
        auto_start = bool(self.auto_var.get())
        if not server_url:
            self._failure(ForgeServerClientError("BAD_URL", "Enter a Forge Server URL."))
            return

        self._connecting = True
        self.connect_button.configure(state="disabled")
        self.status_var.set("Checking Forge Server…")
        self.model_var.set("—")
        self.provider_var.set("—")
        self.backend_var.set("—")
        threading.Thread(
            target=self._connect_worker,
            args=(server_url, credential, auto_start),
            daemon=True,
        ).start()

    def _connect_worker(self, server_url: str, credential: str, auto_start: bool) -> None:
        try:
            token = credential or _read_token()
            client = ForgeServerClient(server_url, token=token)

            if not client.ping():
                if not (auto_start and _is_local_server(server_url)):
                    raise ForgeServerClientError(
                        "UNREACHABLE",
                        "Forge Server is not running at %s." % server_url,
                    )
                token = self._start_local_server(server_url, token)
                client = ForgeServerClient(server_url, token=token)
                if not self._wait_for_server(client):
                    raise ForgeServerClientError(
                        "SERVER_START_TIMEOUT",
                        "Forge Server started but did not become ready in time.",
                    )

            # A bootstrap token is a valid bearer credential, but an API key
            # should be exchanged for a short-lived session before discovery.
            # If the supplied credential is already a session/bootstrap token,
            # direct bearer discovery is the correct path.
            if token:
                try:
                    client.whoami()
                except ForgeServerClientError as exc:
                    if exc.http_status == 401:
                        # It may be an API key. login() performs the official
                        # challenge/response exchange and replaces client.token.
                        client.login(token)
                    else:
                        raise
            else:
                raise ForgeServerClientError(
                    "AUTH_REQUIRED",
                    "Forge Server requires authentication. No local token was found.",
                )

            model = discover_model(client)
            model_name = _model_identity(model)
            provider = str(model.get("provider_id") or model.get("provider") or "—")
            backend = str(model.get("backend_id") or model.get("backend") or "—")
            self.after(0, lambda: self._success(model_name, provider, backend))
        except ForgeServerClientError as exc:
            self.after(0, lambda: self._failure(exc))
        except Exception as exc:
            self.after(
                0,
                lambda: self._failure(
                    ForgeServerClientError("CLIENT_ERROR", str(exc))
                ),
            )

    def _start_local_server(self, server_url: str, existing_token: str) -> str:
        """Start the real Forge Server in-process; no terminal is required."""
        from forge.server import ForgeServer, ServerConfig

        host, port = _base_host_port(server_url)
        token = existing_token or secrets.token_urlsafe(32)
        _write_token(token)

        root = str(Path(__file__).resolve().parent)
        db_path = str(_server_db_path())
        config = ServerConfig(
            db_path=db_path,
            host=host,
            port=port,
            projects={Path(root).name or "forge": root},
            bootstrap_token=token,
            profile="assisted",
        )
        server = ForgeServer(config)
        self._owned_server = server
        self.status_var.set("Starting local Forge Server…")
        threading.Thread(
            target=lambda: server.run_uvicorn(host=host, port=port, log_level="warning"),
            daemon=True,
        ).start()
        return token

    @staticmethod
    def _wait_for_server(client: ForgeServerClient, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if client.ping():
                return True
            time.sleep(0.25)
        return False

    def _success(self, name: str, provider: str, backend: str) -> None:
        self._connecting = False
        self.connect_button.configure(state="normal")
        self.status_var.set("Connected — usable model discovered")
        self.model_var.set(name)
        self.provider_var.set(provider)
        self.backend_var.set(backend)

    def _failure(self, exc: ForgeServerClientError) -> None:
        self._connecting = False
        self.connect_button.configure(state="normal")
        self.status_var.set("Not ready: " + exc.code)
        self.model_var.set("—")
        self.provider_var.set("—")
        self.backend_var.set("—")
        messagebox.showerror("Forge AI", exc.message)

    def _close(self) -> None:
        server = self._owned_server
        if server is not None:
            try:
                server.close()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    ForgeDesktop().mainloop()
