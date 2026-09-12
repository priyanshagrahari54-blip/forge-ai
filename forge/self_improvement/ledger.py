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


class ImprovementLedger:
    def __init__(self, root: str | Path = ".", *, path: Path | None = None) -> None:
        self.root = Path(root).resolve()
        self.path = path or (self.root / ".forge" / "self_improvement" / "ledger.jsonl")

    # -- writing ----------------------------------------------------------------

    def _last(self) -> dict[str, Any] | None:
        entries = self.entries()
        return entries[-1] if entries else None

    def record(self, kind: str, payload: dict[str, Any], **refs: Any) -> dict[str, Any]:
        if kind not in KINDS:
            raise ValueError(f"unknown ledger kind {kind!r}; expected one of {KINDS}")
        last = self._last()
        seq = (int(last["seq"]) + 1) if last else 1
        previous = str(last["hash"]) if last else ""
        body = {
            "seq": seq, "kind": kind, "at": time.time(),
            "refs": {k: v for k, v in refs.items() if v},
            "payload": _redact_keys(redact(payload)),
        }
        digest = hashlib.sha256(
            (previous + json.dumps(body, sort_keys=True, default=str)).encode("utf-8")
        ).hexdigest()
        body["prev"] = previous
        body["hash"] = digest
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(body, default=str) + "\n")
        self._prune()
        return body

    def _prune(self) -> None:
        entries = self.entries()
        if len(entries) <= MAX_ENTRIES:
            return
        keep = entries[-MAX_ENTRIES:]
        with self.path.open("w", encoding="utf-8") as handle:
            for entry in keep:
                handle.write(json.dumps(entry, default=str) + "\n")

    # -- reading ----------------------------------------------------------------

    def entries(self, kind: str | None = None, *, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if kind is None or data.get("kind") == kind:
                    rows.append(data)
        if limit is not None:
            rows = rows[-max(0, int(limit)):]
        return rows

    def verify_chain(self) -> tuple[bool, str]:
        previous = ""
        for entry in self.entries():
            body = {k: entry[k] for k in ("seq", "kind", "at", "refs", "payload") if k in entry}
            expected = hashlib.sha256(
                (previous + json.dumps(body, sort_keys=True, default=str)).encode("utf-8")
            ).hexdigest()
            if entry.get("prev", "") != previous or entry.get("hash") != expected:
                return False, f"chain broken at seq {entry.get('seq')}"
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
