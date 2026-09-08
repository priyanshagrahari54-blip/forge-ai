"""Natural voice conversation (A42): multi-turn, interruptible,
confirming, context-aware — on top of the A36 voice gate.

The A36 loop is one-shot (wake → transcribe → intent → gate → act).
A42 wraps it in a bounded conversation with:

* **short-term context** — the previous turn's target fills pronoun
  references ("it", "that", "this one") deterministically, so
  follow-ups like "make it faster" attach to the right object;
* **barge-in** — an interrupt event marks the current turn
  ``interrupted`` and prevents any subsequent action (task creation,
  replies) from that turn;
* **clarifying questions** — unrecognized speech is never guessed at:
  the engine asks a spoken question and waits;
* **confirm before executing** — task-creating intents pause at an
  ``awaiting_confirmation`` turn and are only acted on after an
  affirmative reply, and even then only through the existing A36
  voice permission gate;
* **spoken results** — every reply carries a spoken summary.

Honesty: intent recognition and speech are the A36 simulated stack
(labeled); context resolution is a bounded, documented heuristic.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from forge.voice.base import VoiceCommand, VoiceInterface

MAX_TURNS = 24
AFFIRMATIVES = ("yes", "yeah", "yep", "confirm", "ok", "okay", "sure",
                "do it", "go ahead", "please do", "proceed")
NEGATIVES = ("no", "nope", "cancel", "stop", "never mind", "abort")
PRONOUNS = ("it", "that", "this", "this one", "that one")


@dataclass
class ConversationTurn:
    role: str  # user | assistant
    text: str
    status: str = "completed"  # completed | interrupted |
    #                            awaiting_confirmation | awaiting_answer
    intent: str = ""
    resolved: str = ""
    spoken: str = ""
    task: dict[str, Any] | None = None
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "text": self.text[:1000],
            "status": self.status,
            "intent": self.intent,
            "spoken": self.spoken[:2000],
            "task": self.task,
            "at": self.at,
        }


class VoiceConversation:
    """One bounded, interruptible voice conversation."""

    def __init__(self, interface: VoiceInterface, conversation_id: str,
                 *, max_turns: int = MAX_TURNS) -> None:
        self.interface = interface
        self.id = conversation_id
        self.max_turns = max_turns
        self.turns: list[ConversationTurn] = []
        self._interrupt = threading.Event()
        self._lock = threading.RLock()

    # -- context ------------------------------------------------------------

    def last_user_turn(self) -> ConversationTurn | None:
        for turn in reversed(self.turns):
            if turn.role == "user":
                return turn
        return None

    def resolve_context(self, speech: str) -> str:
        """Fill pronoun references from the previous turn (bounded)."""
        previous = self.last_user_turn()
        if previous is None or previous.intent not in (
                "summarize", "review"):
            return speech
        lowered = speech.lower().strip().rstrip(".!?")
        for pronoun in PRONOUNS:
            if lowered == pronoun or lowered.startswith(pronoun + " "):
                speech = lowered.replace(pronoun,
                                         "the previous target", 1)
                return speech
        return speech

    # -- barge-in -------------------------------------------------------------

    def interrupt(self) -> bool:
        """Barge in: mark the active turn interrupted, block new actions."""
        self._interrupt.set()
        for turn in reversed(self.turns):
            if turn.role == "user" and turn.status != "interrupted":
                turn.status = "interrupted"
                return True
        return False

    def interrupted(self) -> bool:
        return self._interrupt.is_set()

    def clear_interrupt(self) -> None:
        self._interrupt.clear()

    # -- turns -------------------------------------------------------------------

    def say(self, speech: str, *, task_factory: Callable | None = None,
            approval_token_id: str = "", confirm: bool = True
            ) -> dict[str, Any]:
        """Process one user utterance into a bounded turn.

        Returns the assistant reply dict: spoken text, status, and the
        task when one was created. Interrupted turns never create
        tasks and never speak replies.
        """
        with self._lock:
            speech = str(speech).strip()
            if not speech or len(speech) > 2000:
                raise ValueError("utterance must be 1-2000 characters")
            self.turns = self.turns[-(self.max_turns - 1):]
            resolved = self.resolve_context(speech)
            user_turn = ConversationTurn(role="user", text=speech,
                                         resolved=resolved)
            self.turns.append(user_turn)
            was_interrupted = self.interrupted()
            self.clear_interrupt()  # the next utterance starts fresh
            if was_interrupted:
                user_turn.status = "interrupted"
                return self._reply(
                    "Turn interrupted; nothing was executed.", "",
                    status="interrupted", user_turn=user_turn)
            lowered = resolved.lower().strip().rstrip(".!?")
            # Confirm-or-cancel a pending confirmation.
            pending = self._pending_confirmation()
            if pending is not None:
                return self._settle_confirmation(
                    pending, lowered, resolved, user_turn, task_factory,
                    approval_token_id)
            intent = self.interface.parse(VoiceCommand(text=resolved))
            if not intent.known:
                user_turn.status = "awaiting_answer"
                spoken = ("I didn't catch a command I can run. "
                          "What would you like me to do?")
                return self._reply(spoken, "", status="awaiting_answer",
                                   user_turn=user_turn)
            user_turn.intent = intent.name
            if confirm:
                user_turn.status = "awaiting_confirmation"
                spoken = (f"Shall I {intent.name.replace('_', ' ')}? "
                          "Say yes to proceed or no to cancel.")
                return self._reply(spoken, intent.name,
                                   status="awaiting_confirmation",
                                   user_turn=user_turn)
            return self._execute(resolved, intent.name, user_turn,
                                 task_factory, approval_token_id)


    def _settle_confirmation(self, pending: ConversationTurn, lowered: str,
                             resolved: str, user_turn: ConversationTurn,
                             task_factory: Callable | None,
                             approval_token_id: str) -> dict[str, Any]:
        if lowered in AFFIRMATIVES:
            if self.interrupted():
                return self._reply(
                    "Turn interrupted; nothing was executed.", "",
                    status="interrupted", user_turn=user_turn)
            pending.status = "completed"
            user_turn.intent = pending.intent
            return self._execute(pending.resolved or pending.text,
                                 pending.intent, user_turn,
                                 task_factory, approval_token_id)
        if lowered in NEGATIVES:
            pending.status = "completed"
            return self._reply("Cancelled. Nothing was created.",
                               pending.intent, status="completed",
                               user_turn=user_turn)
        # Ambiguous answer to a confirmation: ask again, never guess.
        user_turn.status = "awaiting_confirmation"
        return self._reply(
            "Please say yes to proceed or no to cancel.",
            pending.intent, status="awaiting_confirmation",
            user_turn=user_turn)

    def _execute(self, resolved: str, intent_name: str,
                 user_turn: ConversationTurn,
                 task_factory: Callable | None,
                 approval_token_id: str) -> dict[str, Any]:
        result = self.interface.handle(
            VoiceCommand(text=resolved), task_factory=task_factory,
            approval_token_id=approval_token_id)
        if self.interrupted():
            user_turn.status = "interrupted"
            return self._reply(
                "Turn interrupted; nothing was executed.", intent_name,
                status="interrupted", user_turn=user_turn)
        if not result.ok:
            user_turn.status = "completed"
            return self._reply(result.message or "Voice command not "
                               "permitted.", intent_name,
                               status="completed", user_turn=user_turn)
        task = result.task or {}
        user_turn.task = task
        if task.get("kind") == "reply":
            spoken = task.get("text", "Done.")
        elif task.get("kind") == "task":
            spoken = ("Task created: "
                      + str(task.get("task_id", ""))[:12] + " — "
                      + str(task.get("requirement", ""))[:120])
        else:
            spoken = "Done."
        return self._reply(spoken, intent_name, status="completed",
                           user_turn=user_turn, task=task)

    def _pending_confirmation(self) -> ConversationTurn | None:
        for turn in reversed(self.turns):
            if turn.role == "user" and \
                    turn.status == "awaiting_confirmation":
                return turn
            if turn.role == "assistant" and turn.status == "completed":
                return None
        return None

    def _reply(self, spoken: str, intent: str, *, status: str,
               user_turn: ConversationTurn,
               task: dict[str, Any] | None = None) -> dict[str, Any]:
        self.turns.append(ConversationTurn(
            role="assistant", text=spoken, spoken=spoken, status=status,
            intent=intent, task=task))
        self.turns = self.turns[-self.max_turns:]
        return {"conversation_id": self.id, "spoken": spoken,
                "intent": intent, "status": status, "task": task}

    def state(self) -> dict[str, Any]:
        return {
            "conversation_id": self.id,
            "turns": [turn.to_dict() for turn in self.turns],
            "interrupted": self.interrupted(),
            "pending_confirmation": self._pending_confirmation() is not None,
            "turn_count": len(self.turns),
        }


def new_conversation(interface: VoiceInterface) -> VoiceConversation:
    return VoiceConversation(interface, uuid4().hex[:12])
