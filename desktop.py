"""Forge Desktop — thin client with automatic server model discovery.

The desktop never loads model weights locally. It connects to the Forge Server,
authenticates when FORGE_API_KEY is present, asks the server's Model Fabric to
discover configured backends/models, and displays the first usable model.

Environment variables (all optional):
  FORGE_SERVER_URL  e.g. http://127.0.0.1:8010
  FORGE_API_KEY     API key for the Forge Server
  FORGE_MODEL       optional explicit model id/name override

Stdlib/Tkinter only; suitable for the G560 thin-client design.
"""
from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import messagebox
from typing import Any, Dict, List

from forge.server.client import ForgeServerClient, ForgeServerClientError


DEFAULT_SERVER = os.environ.get("FORGE_SERVER_URL", "http://127.0.0.1:8010")
DEFAULT_KEY = os.environ.get("FORGE_API_KEY", "")
DEFAULT_MODEL = os.environ.get("FORGE_MODEL", "")


def _model_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Accept the server's current list shape without inventing models."""
    rows = payload.get("models", payload.get("items", []))
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _usable(row: Dict[str, Any]) -> bool:
    if row.get("available") is False:
        return False
    health = row.get("health")
    if isinstance(health, dict):
        state = str(health.get("state", health.get("status", ""))).lower()
        if state in {"failed", "unavailable", "unhealthy", "offline"}:
            return False
    return True


def discover_model(client: ForgeServerClient) -> Dict[str, Any]:
    """Discover the configured model from the server, never locally."""
    payload = client.list_models(discover=True)
    rows = _model_rows(payload)
    requested = DEFAULT_MODEL.strip()
    if requested:
        for row in rows:
            identity = str(row.get("name", row.get("model_id", "")))
            if identity == requested and _usable(row):
                return row
        raise ForgeServerClientError(
            "MODEL_NOT_FOUND",
            "Configured model '%s' was not returned by Forge Server." % requested,
        )
    for row in rows:
        if _usable(row) and not bool(row.get("fallback", False)):
            return row
    for row in rows:
        if _usable(row):
            return row
    raise ForgeServerClientError(
        "NO_MODEL",
        "Forge Server is reachable, but no usable configured model was discovered.",
    )


class ForgeDesktop(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Forge AI — Desktop")
        self.geometry("640x360")
        self.minsize(520, 300)

        self.server_var = tk.StringVar(value=DEFAULT_SERVER)
        self.key_var = tk.StringVar(value=DEFAULT_KEY, show="*")
        self.status_var = tk.StringVar(value="Not connected")
        self.model_var = tk.StringVar(value="No model discovered")
        self.provider_var = tk.StringVar(value="—")

        frame = tk.Frame(self, padx=18, pady=18)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="Forge AI", font=("Segoe UI", 20, "bold")).pack(anchor="w")
        tk.Label(frame, text="Thin desktop client • model execution stays on Forge Server",
                 fg="#666").pack(anchor="w", pady=(0, 16))

        row = tk.Frame(frame); row.pack(fill="x", pady=4)
        tk.Label(row, text="Server", width=12, anchor="w").pack(side="left")
        tk.Entry(row, textvariable=self.server_var).pack(side="left", fill="x", expand=True)

        row = tk.Frame(frame); row.pack(fill="x", pady=4)
        tk.Label(row, text="API key", width=12, anchor="w").pack(side="left")
        tk.Entry(row, textvariable=self.key_var, show="*").pack(side="left", fill="x", expand=True)

        tk.Button(frame, text="Connect & Discover Model", command=self.connect,
                  height=2).pack(fill="x", pady=(14, 18))

        info = tk.Frame(frame); info.pack(fill="x")
        tk.Label(info, text="Connection:", width=14, anchor="w").grid(row=0, column=0, sticky="w")
        tk.Label(info, textvariable=self.status_var, anchor="w").grid(row=0, column=1, sticky="w")
        tk.Label(info, text="Model:", width=14, anchor="w").grid(row=1, column=0, sticky="w")
        tk.Label(info, textvariable=self.model_var, anchor="w").grid(row=1, column=1, sticky="w")
        tk.Label(info, text="Provider:", width=14, anchor="w").grid(row=2, column=0, sticky="w")
        tk.Label(info, textvariable=self.provider_var, anchor="w").grid(row=2, column=1, sticky="w")
        info.columnconfigure(1, weight=1)

        self.after(250, self.connect)

    def connect(self) -> None:
        self.status_var.set("Connecting to Forge Server…")
        self.model_var.set("Discovering…")
        self.provider_var.set("—")
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self) -> None:
        try:
            client = ForgeServerClient(self.server_var.get().strip(),
                                       token=self.key_var.get().strip())
            if self.key_var.get().strip():
                client.login(self.key_var.get().strip())
            else:
                # /ping is intentionally anonymous; model discovery will give
                # the precise authentication error if a credential is required.
                if not client.ping():
                    raise ForgeServerClientError("UNREACHABLE", "Forge Server did not answer /ping.")
            model = discover_model(client)
            name = str(model.get("name", model.get("model_id", "Unknown model")))
            provider = str(model.get("provider", model.get("backend", "Unknown provider")))
            self.after(0, lambda: self._success(name, provider))
        except ForgeServerClientError as exc:
            self.after(0, lambda: self._failure(exc))
        except Exception as exc:
            self.after(0, lambda: self._failure(
                ForgeServerClientError("CLIENT_ERROR", str(exc))))

    def _success(self, name: str, provider: str) -> None:
        self.status_var.set("Connected — server model discovered")
        self.model_var.set(name)
        self.provider_var.set(provider)

    def _failure(self, exc: ForgeServerClientError) -> None:
        self.status_var.set("Not ready: " + exc.code)
        self.model_var.set("No usable server model")
        self.provider_var.set("—")
        messagebox.showerror("Forge model discovery", exc.message)


if __name__ == "__main__":
    ForgeDesktop().mainloop()
