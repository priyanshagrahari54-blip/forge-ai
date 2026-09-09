"""Managed local computation (A48): real code execution with honest
resource accounting.

Honesty rules:

* Execution is real: code runs in a fresh ``sys.executable -I -c``
  subprocess against the project working directory. Status
  (succeeded/failed/timeout) comes from real exit codes, real
  timeouts, and captured output — never invented.
* There is no remote/GPU/cloud backend in this build. The backend is
  labeled ``local-python`` everywhere, and the status payload says
  so.
* Quotas are enforced before execution (cells and total seconds);
  refusal is honest and leaves no side effects.
"""
from __future__ import annotations

import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_CODE = 6000
MAX_OUTPUT = 4000
MAX_CELLS = 20
BACKEND = "local-python"


@dataclass
class ComputeQuota:
    max_cells: int = MAX_CELLS
    max_seconds: float = 300.0
    cell_timeout: float = 30.0
    cells_used: int = 0
    seconds_used: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_cells": self.max_cells,
            "max_seconds": self.max_seconds,
            "cell_timeout": self.cell_timeout,
            "cells_used": self.cells_used,
            "seconds_used": round(self.seconds_used, 2),
            "cells_remaining": max(0, self.max_cells - self.cells_used),
            "seconds_remaining": round(
                max(0.0, self.max_seconds - self.seconds_used), 2),
        }


class ComputeEngine:
    """Bounded local execution backend for one session."""

    def __init__(self, root: str | Path, *, max_cells: int = MAX_CELLS,
                 max_seconds: float = 300.0,
                 cell_timeout: float = 30.0) -> None:
        self.root = Path(root).resolve()
        self.quota = ComputeQuota(max_cells=max_cells,
                                  max_seconds=max_seconds,
                                  cell_timeout=cell_timeout)
        self.cells: list[dict[str, Any]] = []

    def backend_info(self) -> dict[str, Any]:
        remote_info: dict[str, Any] = {"available": False}
        try:
            from forge.compute.remote import RemoteComputeBackend
            remote = RemoteComputeBackend()
            remote_info = {
                "available": remote.available(),
                "backend": remote.backend_name,
            }
        except Exception:
            pass
        return {"backend": BACKEND,
                "note": "Real local Python execution via a fresh "
                        "subprocess.",
                "remote": remote_info,
                "quota": self.quota.to_dict()}

    def execute(self, code: str, *, timeout: float | None = None
                ) -> dict[str, Any]:
        if not isinstance(code, str) or not code.strip() \
                or "\x00" in code:
            raise ValueError("code must be 1-6000 characters, no nulls")
        code = code.strip()[:MAX_CODE]
        if self.quota.cells_used >= self.quota.max_cells:
            return self._refused("cell quota exhausted")
        if self.quota.seconds_used >= self.quota.max_seconds:
            return self._refused("time quota exhausted")
        timeout = min(timeout or self.quota.cell_timeout,
                      self.quota.cell_timeout,
                      self.quota.max_seconds - self.quota.seconds_used)
        cell_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [sys.executable, "-I", "-c", code],
                cwd=str(self.root), capture_output=True, text=True,
                timeout=timeout,
                check=False)
            status = "succeeded" if completed.returncode == 0 else "failed"
            output = (completed.stdout or "") + (completed.stderr or "")
            return_code = completed.returncode
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            status = "timeout"
            output = ((exc.stdout or "") if isinstance(exc.stdout, str)
                      else "") + ((exc.stderr or "")
                                  if isinstance(exc.stderr, str) else "")
            return_code = -1
            timed_out = True
        elapsed = time.monotonic() - started
        self.quota.cells_used += 1
        self.quota.seconds_used += elapsed
        cell = {
            "cell_id": cell_id,
            "status": status,
            "return_code": return_code,
            "output": output[:MAX_OUTPUT],
            "output_truncated": len(output) > MAX_OUTPUT,
            "elapsed_ms": round(elapsed * 1000, 1),
            "timed_out": timed_out,
            "backend": BACKEND,
            "code_chars": len(code),
        }
        self.cells.append(cell)
        self.cells = self.cells[-MAX_CELLS:]
        return {**cell, "quota": self.quota.to_dict()}

    def _refused(self, reason: str) -> dict[str, Any]:
        return {"cell_id": "", "status": "refused", "return_code": 0,
                "output": f"Refused: {reason}. Nothing was executed.",
                "output_truncated": False, "elapsed_ms": 0.0,
                "timed_out": False, "backend": BACKEND,
                "code_chars": 0, "reason": reason,
                "quota": self.quota.to_dict()}

    def history(self) -> list[dict[str, Any]]:
        return list(self.cells)
