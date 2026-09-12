"""Improvement ledger (A81): append-only, bounded, JSONL.

Every proposal, candidate, decision, applied improvement, and rollback is
recorded with a monotonically increasing sequence number and a hash chain
over entries, so tampering with earlier records is detectable. The ledger
lives under ``.forge/self_improvement/ledger.jsonl`` (runtime state, not
committed).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from forge.core.report import REDACTED, redact

MAX_ENTRIES = 5000
_SECRET_KEYS = ("token", "secret", "password", "api_key", "apikey",
                "credential", "private_key", "authorization")


def _redact_keys(value: Any) -> Any:
    """Mask values stored under secret-like keys (on top of pattern redaction)."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in _SECRET_KEYS) and isinstance(item, str) and item:
                out[key] = REDACTED
            else:
                out[key] = _redact_keys(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact_keys(item) for item in value]
    return value
KINDS = ("analysis", "proposal", "candidate", "decision", "pending", "applied",
         "rejected", "rollback", "iteration", "note")


def _entry_hash(previous: str, body: dict[str, Any]) -> str:
    core = {k: body[k] for k in ("seq", "kind", "at", "refs", "payload") if k in body}
    return hashlib.sha256(
        (previous + json.dumps(core, sort_keys=True, default=str)).encode("utf-8")
    ).hexdigest()


class ImprovementLedger:
    """Append-only, hash-chained JSONL ledger.

    Appends are O(1): the last sequence/hash is kept in memory (and
    re-derived from the file tail on first use). Writes are serialized by
    a process lock; when the active file exceeds ``MAX_ENTRIES`` it is
    *rotated* to ``ledger.<n>.jsonl`` and the chain continues (the first
    entry of the new file references the last hash of the rotated one), so
    history is never truncated and the chain never breaks.
    """

    def __init__(self, root: str | Path = ".", *, path: Path | None = None) -> None:
        self.root = Path(root).resolve()
        self.path = path or (self.root / ".forge" / "self_improvement" / "ledger.jsonl")
        self._lock = threading.RLock()
        self._tail: tuple[int, str] | None = None  # (seq, hash)
        self._count: int | None = None

    # -- writing ----------------------------------------------------------------

    def _load_tail(self) -> tuple[int, str]:
        if self._tail is not None:
            return self._tail
        entries = self.entries()
        self._count = len(entries)
        if not entries:
            # Active file empty/rotated: continue from the newest rotated file.
            for path in reversed(self._rotated_paths()):
                rows = self._read(path)
                if rows:
                    entries = rows
                    break
        if entries:
            last = entries[-1]
            self._tail = (int(last["seq"]), str(last["hash"]))
        else:
            self._tail = (0, "")
        return self._tail

    def _rotated_paths(self) -> list[Path]:
        paths = []
        index = 1
        while True:
            candidate = self.path.with_name(f"{self.path.stem}.{index}{self.path.suffix}")
            if not candidate.exists():
                break
            paths.append(candidate)
            index += 1
        return paths

    def record(self, kind: str, payload: dict[str, Any], **refs: Any) -> dict[str, Any]:
        if kind not in KINDS:
            raise ValueError(f"unknown ledger kind {kind!r}; expected one of {KINDS}")
        with self._lock:
            last_seq, previous = self._load_tail()
            body = {
                "seq": last_seq + 1, "kind": kind, "at": time.time(),
                "refs": {k: v for k, v in refs.items() if v},
                "payload": _redact_keys(redact(payload)),
            }
            body["prev"] = previous
            body["hash"] = _entry_hash(previous, body)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(body, default=str) + "\n"
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            self._tail = (body["seq"], body["hash"])
            self._count = (self._count or 0) + 1
            if self._count > MAX_ENTRIES:
                self._rotate()
            return body

    def _rotate(self) -> None:
        index = 1
        while (self.path.with_name(f"{self.path.stem}.{index}{self.path.suffix}")).exists():
            index += 1
        self.path.rename(self.path.with_name(f"{self.path.stem}.{index}{self.path.suffix}"))
        self._count = 0

    # -- reading ----------------------------------------------------------------

    def entries(self, kind: str | None = None, *, limit: int | None = None) -> list[dict[str, Any]]:
        rows = self._read(self.path)
        if kind is not None:
            rows = [r for r in rows if r.get("kind") == kind]
        if limit is not None:
            rows = rows[-max(0, int(limit)):]
        return rows

    @staticmethod
    def _read(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                rows.append(data)
        return rows

    def verify_chain(self) -> tuple[bool, str]:
        """Verify the whole chain: every rotated file in order, then the
        active file. Any edit, deletion or reordering breaks it."""
        previous = ""
        expected_seq: int | None = None
        rows: list[dict[str, Any]] = []
        for path in self._rotated_paths():
            rows.extend(self._read(path))
        rows.extend(self._read(self.path))
        for entry in rows:
            expected = _entry_hash(previous, entry)
            if entry.get("prev", "") != previous or entry.get("hash") != expected:
                return False, f"chain broken at seq {entry.get('seq')}"
            seq = int(entry.get("seq", 0))
            if expected_seq is not None and seq != expected_seq:
                return False, f"sequence gap at seq {seq}"
            expected_seq = seq + 1
            previous = expected
        return True, "ok"

    def by_candidate(self, candidate_id: str) -> list[dict[str, Any]]:
        return [e for e in self.entries() if e.get("refs", {}).get("candidate_id") == candidate_id]

    def outcomes(self) -> list[dict[str, Any]]:
        """Compact history used by the proposal generator to avoid loops."""
        rows: list[dict[str, Any]] = []
        for entry in self.entries():
            if entry["kind"] in ("applied", "rejected"):
                payload = entry.get("payload", {})
                rows.append({
                    "outcome": "accepted" if entry["kind"] == "applied" else "rejected",
                    "weakness_id": payload.get("weakness_id", ""),
                    "proposal_id": entry.get("refs", {}).get("proposal_id", ""),
                    "candidate_id": entry.get("refs", {}).get("candidate_id", ""),
                    "at": entry.get("at"),
                })
        return rows

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for entry in self.entries():
            counts[entry["kind"]] = counts.get(entry["kind"], 0) + 1
        ok, detail = self.verify_chain()
        return {"entries": sum(counts.values()), "by_kind": counts,
                "chain_ok": ok, "chain_detail": detail, "path": str(self.path)}

    @staticmethod
    def latest_by_ref(entries: Iterable[dict[str, Any]], ref: str) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for entry in entries:
            key = entry.get("refs", {}).get(ref)
            if key:
                latest[key] = entry
        return latest
