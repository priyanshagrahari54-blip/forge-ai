"""General conversation engine (A43): every message is classified,
routed, answered with real information or turned into a real task —
never fabricated.

Classification is deterministic and documented:

* ``task_request`` — actionable engineering requests (build/add/fix/
  implement/run/…) route into the existing supervisor task pipeline
  with a clear summary of what was created;
* ``question`` — answered with real information: repository structure
  and insights from RepositoryIntelligence, live task counts from the
  control plane, remembered facts/preferences from the A37 memory
  store — or an honest "I don't have an answer" with what Forge can
  do instead;
* ``preference`` — "remember that …" / "I prefer …" stores a bounded
  fact through the normal (policy-gated) memory path;
* ``greeting`` / ``chat`` — deterministic, bounded replies that never
  pretend to be more than they are.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_MESSAGE = 4000
MAX_HISTORY = 16


@dataclass
class Message:
    role: str  # user | assistant
    text: str
    kind: str = ""
    task_id: str = ""
    at: float = field(default_factory=lambda: __import__("time").time())

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "text": self.text[:2000],
                "kind": self.kind, "task_id": self.task_id, "at": self.at}


_TASK_MARKERS = (
    "build", "create", "add ", "adding", "implement", "fix ", "fixing",
    "refactor", "write ", "generate", "deploy", "install", "remove ",
    "delete the", "rename", "run the tests", "run tests", "commit ",
    "update the", "optimize", "migrate", "analyze ",
)
_QUESTION_MARKERS = (
    "what", "who", "where", "when", "why", "how ", "which", "does ",
    "do you", "can you tell", "explain", "describe", "list the",
    "show me", "summarize", "is the", "is there", "are there",
)
_PREFERENCE_MARKERS = ("remember that", "i prefer", "i like", "always ",
                       "note that", "my preference")
_GREETINGS = ("hi", "hello", "hey", "good morning", "good afternoon",
              "good evening", "yo")


class GeneralConversationEngine:
    """Classify → route → answer with real information."""

    def __init__(self, *, submit_task: Callable[[str], dict[str, Any]],
                 answer_question: Callable[[str], str],
                 remember: Callable[[str], bool],
                 remember_history: Callable[[str], None],
                 history: list[Message] | None = None) -> None:
        self.submit_task = submit_task
        self.answer_question = answer_question
        self.remember = remember
        self.remember_history = remember_history
        self.history: list[Message] = history or []

    # -- classification -----------------------------------------------------

    def classify(self, message: str) -> str:
        lowered = message.lower().strip()
        if any(lowered.startswith(greeting + " ") or lowered == greeting
               for greeting in _GREETINGS):
            return "greeting"
        if any(marker in lowered for marker in _PREFERENCE_MARKERS):
            return "preference"
        if any(marker in lowered for marker in _TASK_MARKERS):
            if lowered.endswith("?") and any(
                    marker in lowered for marker in _QUESTION_MARKERS):
                return "question"  # "how do you build X?" is a question
            return "task_request"
        if lowered.endswith("?") or any(
                marker in lowered for marker in _QUESTION_MARKERS):
            return "question"
        return "chat"

    # -- routing -------------------------------------------------------------

    def converse(self, message: str) -> dict[str, Any]:
        if not isinstance(message, str) or not message.strip() \
                or len(message) > MAX_MESSAGE:
            raise ValueError("message must be 1-4000 characters")
        message = message.strip()
        kind = self.classify(message)
        self.history.append(Message(role="user", text=message, kind=kind))
        self.history = self.history[-MAX_HISTORY:]
        if kind == "task_request":
            task = self.submit_task(message)
            reply = (f"I've started a task for that: "
                     f"{str(task.get('task_id', ''))[:12]} — "
                     f"{str(task.get('requirement', message))[:140]}")
            self._append(reply, kind, str(task.get("task_id", "")))
            return {"kind": "task_request", "reply": reply,
                    "task": task, "history": self.snapshot()}
        if kind == "preference":
            remembered = self.remember(message)
            reply = ("I've remembered that." if remembered
                     else "I couldn't remember that (memory gate "
                          "denied).")
            self._append(reply, kind)
            return {"kind": "preference", "reply": reply,
                    "remembered": remembered, "history": self.snapshot()}
        if kind == "question":
            answer = self.answer_question(message)
            self._append(answer, kind)
            return {"kind": "question", "reply": answer,
                    "history": self.snapshot()}
        if kind == "greeting":
            reply = ("Hello! I'm Forge. I can start engineering tasks "
                     "(\"add CSV export to this project\"), answer "
                     "questions about the repository or your tasks, and "
                     "remember your preferences.")
            self._append(reply, kind)
            return {"kind": "greeting", "reply": reply,
                    "history": self.snapshot()}
        reply = ("I can help with engineering tasks and questions about "
                 "the project. Try \"what does this project do?\" or "
                 "\"add a README section\".")
        self._append(reply, kind)
        return {"kind": "chat", "reply": reply,
                "history": self.snapshot()}

    def _append(self, text: str, kind: str, task_id: str = "") -> None:
        self.history.append(Message(role="assistant", text=text,
                                    kind=kind, task_id=task_id))
        self.history = self.history[-MAX_HISTORY:]
        try:
            self.remember_history(text[:400])
        except Exception:
            pass  # history memory is best-effort; replies never fail

    def snapshot(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.history]
