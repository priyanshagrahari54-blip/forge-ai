"""Persistent assistant sessions (A84 Stage B1/B2).

``Forge should understand that the user is interacting with one continuing
assistant``. This ledger gives every cockpit/server session its persistent
assistant record with the exact fields the intelligence addendum requires:

``session id · title · creation time · last activity · conversation history ·
active task · active project · relevant memory references · tool activity ·
agent activity · research references · outputs/artifacts``

Design constraints:

* **One store, many surfaces.** The ledger is a plain SQLite layer over the
  same ``forge.control.db.Database`` used by the plane, the A37 session
  memory and the server stores — so continuity survives restarts and no
  parallel "chat app database" is introduced.
* **Bounded everything.** History is capped per session; references are
  capped; nothing here can be flooded by a long conversation.
* **Memory references, not memory payloads.** The session keeps ids and
  notes for referenced memories/research/artifacts; content stays where it
  lives (memory engine, run store, research cache) and is retrieved on
  demand — retrieval, not wholesale injection (Stage B3/M).
* **Retention opt-out.** A session may set ``retention_mode=disabled`` which
  makes the assistant keep only in-RAM short-term context (see
  :class:`forge.assistant.memory.PersonalMemoryService`).
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = ["AssistantSession", "SessionLedger", "REF_KINDS"]

REF_KINDS = ("memory", "research", "artifact", "tool", "agent", "task")
MAX_TITLE = 120
MAX_NOTE = 280
MAX_HISTORY = 100           # turns kept per session (bounded)
MAX_SESSIONS = 10_000
_TITLE_STRIP = re.compile(r"^(?:please\s+|hey\s+forge[,!]?\s+|forge[:,]?\s+)",
                          flags=re.IGNORECASE)


def derive_title(first_message: str) -> str:
    """A deterministic, honest title from the first user message."""
    text = _TITLE_STRIP.sub("", (first_message or "").strip())
    text = " ".join(text.split())
    if not text:
        return "Session"
    return (text[:MAX_TITLE - 1] + "…") if len(text) > MAX_TITLE else text


@dataclass(frozen=True)
class AssistantSession:
    """One persistent assistant session (B2 field set)."""

    id: str
    title: str
    created_at: float
    last_activity: float
    project_id: str
    active_task_id: str
    active_project: str
    state: str                      # active | closed
    retention_mode: str             # normal | disabled
    summary: str
    history: Tuple[Dict[str, Any], ...] = ()
    memory_refs: Tuple[Dict[str, Any], ...] = ()
    research_refs: Tuple[Dict[str, Any], ...] = ()
    tool_activity: Tuple[Dict[str, Any], ...] = ()
    agent_activity: Tuple[Dict[str, Any], ...] = ()
    artifacts: Tuple[Dict[str, Any], ...] = ()

    def to_dict(self, *, include_history: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "session_id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "project": self.project_id,
            "active_task": self.active_task_id,
            "active_project": self.active_project,
            "state": self.state,
            "retention_mode": self.retention_mode,
            "summary": self.summary,
            "memory_refs": [dict(r) for r in self.memory_refs],
            "research_refs": [dict(r) for r in self.research_refs],
            "tool_activity": [dict(r) for r in self.tool_activity],
            "agent_activity": [dict(r) for r in self.agent_activity],
            "artifacts": [dict(r) for r in self.artifacts],
        }
        if include_history:
            payload["history"] = [dict(h) for h in self.history]
        return payload


class SessionLedger:
    """SQLite-backed assistant sessions layered over the plane database."""

    def __init__(self, db: Any, *, max_history: int = MAX_HISTORY,
                 max_refs: int = 200, max_sessions: int = MAX_SESSIONS) -> None:
        self._db = db
        self.max_history = max(10, int(max_history))
        self.max_refs = max(10, int(max_refs))
        self.max_sessions = max(100, int(max_sessions))
        db.execute("""
            CREATE TABLE IF NOT EXISTS assistant_sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                last_activity REAL NOT NULL,
                project_id TEXT NOT NULL DEFAULT '',
                active_task_id TEXT NOT NULL DEFAULT '',
                active_project TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'active',
                retention_mode TEXT NOT NULL DEFAULT 'normal',
                summary TEXT NOT NULL DEFAULT '',
                plane_session_id TEXT NOT NULL DEFAULT ''
            )""")
        db.execute("""
            CREATE TABLE IF NOT EXISTS assistant_session_turns (
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL DEFAULT '',
                task_id TEXT NOT NULL DEFAULT '',
                at REAL NOT NULL,
                PRIMARY KEY (session_id, seq)
            )""")
        db.execute("""
            CREATE TABLE IF NOT EXISTS assistant_session_refs (
                session_id TEXT NOT NULL,
                ref_kind TEXT NOT NULL,
                ref_id TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                at REAL NOT NULL,
                PRIMARY KEY (session_id, ref_kind, ref_id)
            )""")

    # -- lifecycle ---------------------------------------------------------------

    def open(self, *, project_id: str = "", plane_session_id: str = "",
             first_message: str = "", title: str = "",
             retention_mode: str = "normal", session_id: str = ""
             ) -> AssistantSession:
        """Create (or resume, when re-opening by plane session id).

        ``session_id`` lets the caller adopt its own key (the cockpit session
        id, or a test fixture id) so ledger reads and writes always hit the
        same row; otherwise a fresh ``as-`` id is minted and linked through
        ``plane_session_id``.
        """
        if session_id:
            existing = self.get(session_id)
            if existing is not None:
                return existing
        if plane_session_id:
            existing_row = self._db.query_one(
                "SELECT id FROM assistant_sessions WHERE plane_session_id = ? "
                "ORDER BY last_activity DESC LIMIT 1",
                (plane_session_id[:64],))
            if existing_row is not None:
                return self.get(existing_row["id"])  # type: ignore[arg-type]
        now = time.time()
        session_id = (session_id or "")[:64] or ("as-" + uuid.uuid4().hex[:16])
        self._db.execute(
            "INSERT INTO assistant_sessions (id, title, created_at, "
            "last_activity, project_id, active_task_id, active_project, "
            "state, retention_mode, summary, plane_session_id) "
            "VALUES (?, ?, ?, ?, ?, '', ?, 'active', ?, '', ?)",
            (session_id, (title or derive_title(first_message))[:MAX_TITLE],
             now, now, project_id[:64], project_id[:64],
             "disabled" if retention_mode == "disabled" else "normal",
             plane_session_id[:64]))
        self._enforce_session_cap()
        return self.get(session_id)  # type: ignore[return-value]

    def get(self, session_id: str) -> Optional[AssistantSession]:
        row = self._db.query_one(
            "SELECT * FROM assistant_sessions WHERE id = ?", (session_id,))
        if row is None:
            return None
        turns = self._db.query(
            "SELECT * FROM assistant_session_turns WHERE session_id = ? "
            "ORDER BY seq DESC LIMIT ?", (session_id, self.max_history))
        refs = self._db.query(
            "SELECT * FROM assistant_session_refs WHERE session_id = ? "
            "ORDER BY at DESC LIMIT ?", (session_id, self.max_refs))
        by_kind: Dict[str, List[Dict[str, Any]]] = {}
        for ref in refs:
            by_kind.setdefault(ref["ref_kind"], []).append({
                "id": ref["ref_id"], "note": ref["note"],
                "at": float(ref["at"])})
        history = [{
            "seq": int(t["seq"]), "role": t["role"], "kind": t["kind"],
            "text": t["text"][:2000], "task_id": t["task_id"],
            "at": float(t["at"])} for t in reversed(list(turns))]
        return AssistantSession(
            id=row["id"], title=row["title"],
            created_at=float(row["created_at"]),
            last_activity=float(row["last_activity"]),
            project_id=row["project_id"],
            active_task_id=row["active_task_id"],
            active_project=row["active_project"], state=row["state"],
            retention_mode=row["retention_mode"], summary=row["summary"],
            history=tuple(history),
            memory_refs=tuple(by_kind.get("memory", ())),
            research_refs=tuple(by_kind.get("research", ())),
            tool_activity=tuple(by_kind.get("tool", ())),
            agent_activity=tuple(by_kind.get("agent", ())),
            artifacts=tuple(by_kind.get("artifact", ())))

    def list(self, *, project_id: str = "", state: str = "active",
              limit: int = 50) -> List[AssistantSession]:
        where: List[str] = []
        params: List[Any] = []
        if project_id:
            where.append("project_id = ?")
            params.append(project_id[:64])
        if state and state != "any":
            where.append("state = ?")
            params.append(state)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(max(1, min(int(limit), 500)))
        rows = self._db.query(
            "SELECT id FROM assistant_sessions " + clause +
            " ORDER BY last_activity DESC LIMIT ?", tuple(params))
        return [out for out in (self.get(row["id"]) for row in rows)
                if out is not None]

    # -- activity ----------------------------------------------------------------

    def record_turn(self, session_id: str, role: str, text: str, *,
                    kind: str = "", task_id: str = "") -> int:
        role = (role or "user").strip().lower()
        if role not in ("user", "assistant", "system-note"):
            raise ValueError("turn role must be user|assistant|system-note")
        now = time.time()
        row = self._db.query_one(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM assistant_session_turns "
            "WHERE session_id = ?", (session_id,))
        seq = int(row["seq"] or 0) + 1 if row is not None else 1
        self._db.execute(
            "INSERT INTO assistant_session_turns (session_id, seq, role, "
            "kind, text, task_id, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, seq, role, kind[:32], (text or "")[:2000],
             task_id[:64], now))
        self._db.execute(
            "UPDATE assistant_sessions SET last_activity = ? WHERE id = ?",
            (now, session_id))
        self._enforce_history_cap(session_id)
        return seq

    def record_ref(self, session_id: str, ref_kind: str, ref_id: str, *,
                   note: str = "") -> bool:
        ref_kind = (ref_kind or "").strip().lower()
        if ref_kind not in REF_KINDS:
            raise ValueError(f"unknown ref kind {ref_kind!r}; expected one "
                             f"of {REF_KINDS}")
        if not (ref_id or "").strip():
            raise ValueError("ref id must be non-empty")
        now = time.time()
        cursor = self._db.execute(
            "INSERT INTO assistant_session_refs (session_id, ref_kind, "
            "ref_id, note, at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id, ref_kind, ref_id) DO UPDATE SET "
            "note = excluded.note, at = excluded.at",
            (session_id, ref_kind, ref_id[:120], note[:MAX_NOTE], now)) \
            if hasattr(self._db, "execute") else None
        self._db.execute(
            "UPDATE assistant_sessions SET last_activity = ? WHERE id = ?",
            (now, session_id))
        self._enforce_ref_cap(session_id)
        return cursor is not None

    def set_active_task(self, session_id: str, task_id: str) -> None:
        self._db.execute(
            "UPDATE assistant_sessions SET active_task_id = ?, "
            "last_activity = ? WHERE id = ?",
            ((task_id or "")[:64], time.time(), session_id))
        if task_id:
            self.record_ref(session_id, "task", task_id,
                            note="active task binding")

    def set_summary(self, session_id: str, summary: str) -> None:
        self._db.execute(
            "UPDATE assistant_sessions SET summary = ?, last_activity = ? "
            "WHERE id = ?", ((summary or "")[:2000], time.time(), session_id))

    def set_retention_mode(self, session_id: str, mode: str) -> bool:
        mode = (mode or "").strip().lower()
        if mode not in ("normal", "disabled"):
            raise ValueError("retention_mode must be normal|disabled")
        self._db.execute(
            "UPDATE assistant_sessions SET retention_mode = ? WHERE id = ?",
            (mode, session_id))
        return True

    def close(self, session_id: str) -> None:
        self._db.execute(
            "UPDATE assistant_sessions SET state = 'closed', last_activity "
            "= ? WHERE id = ?", (time.time(), session_id))

    def reactivate(self, session_id: str) -> None:
        self._db.execute(
            "UPDATE assistant_sessions SET state = 'active', last_activity "
            "= ? WHERE id = ?", (time.time(), session_id))

    def clear_session(self, session_id: str) -> Dict[str, int]:
        """B2/C3: wipe this session's turns + refs (long-term memory is
        untouched — forgetting long-term facts is a separate explicit act)."""
        turns = self._db.execute(
            "DELETE FROM assistant_session_turns WHERE session_id = ?",
            (session_id,)).rowcount
        refs = self._db.execute(
            "DELETE FROM assistant_session_refs WHERE session_id = ?",
            (session_id,)).rowcount
        self._db.execute(
            "UPDATE assistant_sessions SET summary = '', active_task_id = ''"
            " WHERE id = ?", (session_id,))
        return {"turns_deleted": int(turns or 0),
                "refs_deleted": int(refs or 0)}

    # -- bounded maintenance --------------------------------------------------------

    def _enforce_history_cap(self, session_id: str) -> None:
        row = self._db.query_one(
            "SELECT COUNT(*) AS c FROM assistant_session_turns WHERE "
            "session_id = ?", (session_id,))
        total = int(row["c"]) if row is not None else 0
        overflow = total - self.max_history
        if overflow > 0:
            self._db.execute(
                "DELETE FROM assistant_session_turns WHERE session_id = ? "
                "AND seq IN (SELECT seq FROM assistant_session_turns WHERE "
                "session_id = ? ORDER BY seq ASC LIMIT ?)",
                (session_id, session_id, overflow))

    def _enforce_ref_cap(self, session_id: str) -> None:
        row = self._db.query_one(
            "SELECT COUNT(*) AS c FROM assistant_session_refs WHERE "
            "session_id = ?", (session_id,))
        total = int(row["c"]) if row is not None else 0
        overflow = total - self.max_refs
        if overflow > 0:
            self._db.execute(
                "DELETE FROM assistant_session_refs WHERE session_id = ? "
                "AND rowid IN (SELECT rowid FROM assistant_session_refs "
                "WHERE session_id = ? ORDER BY at ASC LIMIT ?)",
                (session_id, session_id, overflow))

    def _enforce_session_cap(self) -> None:
        row = self._db.query_one(
            "SELECT COUNT(*) AS c FROM assistant_sessions")
        total = int(row["c"]) if row is not None else 0
        overflow = total - self.max_sessions
        if overflow > 0:
            doomed = self._db.query(
                "SELECT id FROM assistant_sessions ORDER BY last_activity "
                "ASC LIMIT ?", (overflow,))
            for item in doomed:
                session_id = item["id"]
                self._db.execute("DELETE FROM assistant_session_turns "
                                 "WHERE session_id = ?", (session_id,))
                self._db.execute("DELETE FROM assistant_session_refs "
                                 "WHERE session_id = ?", (session_id,))
                self._db.execute("DELETE FROM assistant_sessions WHERE id "
                                 "= ?", (session_id,))
