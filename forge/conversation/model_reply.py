"""Model-backed conversational intelligence (conversation channel).

Deterministic intent matching remains the **fast path**: it is instant,
offline, auditable, and it never invents an answer. It is deliberately not
the *only* conversational intelligence — when a runtime-verified live model
is attached to the fabric, open-ended speech ("explain what you are
building", "why did the build fail", "how are you?") is answered by that
model, and every reply carries the provenance of the engine that produced
it.

Honesty rules enforced here:

* only models that are ``available`` *and* marked ``runtime_verified`` are
  used — a configured-but-unverified provider is never silently promoted to
  "live";
* a fallback (offline placeholder) reply is never presented as a model
  answer;
* the model is told explicitly that this channel cannot act, so a
  conversational reply can never claim work it did not do;
* no model, no prompt of the user's content to a provider — the channel
  reports ``deterministic`` provenance instead of pretending.
"""
from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any, Iterable

from forge.models.readiness import is_fallback_response
from forge.models.request import ModelRequest

#: Bounded so a chat channel can never become an unbounded prompt sink.
MAX_MESSAGE_CHARS = 4000
MAX_STATE_CHARS = 2000
MAX_HISTORY_TURNS = 8
MAX_REPLY_CHARS = 4000

SYSTEM_PREAMBLE = (
    "You are Forge, an autonomous software-engineering control plane. "
    "Reply conversationally and concisely, in the same language the user "
    "wrote in (English, Hindi or Hinglish are all supported). "
    "You are speaking through a conversational channel: you cannot run "
    "commands, edit files, deploy, or approve anything from here. Never "
    "claim you performed an action, and never invent repository facts, "
    "file contents, numbers, or results that were not given to you. When "
    "the user asks for real work, tell them the concrete next step (which "
    "task or command to submit) instead of pretending it already happened."
)


@dataclass(frozen=True)
class ModelReply:
    """One answer produced by a runtime-verified live model."""

    text: str
    model: str
    provider: str
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model": self.model,
            "provider": self.provider,
            "latency_ms": round(self.latency_ms, 2),
        }


def live_model_candidates(fabric: Any) -> list[Any]:
    """Return models that are live, routable and runtime-verified.

    ``runtime_verified`` is Forge's own marker for "a real probe observed
    this exact model on its provider". Anything else — configured,
    discovered, unverified, fallback — is not eligible to speak as a model.
    """
    registry = getattr(fabric, "registry", None)
    if registry is None:
        return []
    try:
        models = list(registry.list())
    except Exception:
        return []
    result = []
    for model in models:
        try:
            if bool(getattr(model, "fallback", False)):
                continue
            if not bool(getattr(model, "available", False)):
                continue
            metadata = getattr(model, "metadata", None) or {}
            if metadata.get("runtime_verified") is not True:
                continue
        except Exception:
            continue
        result.append(model)
    return sorted(result, key=lambda model: str(getattr(model, "name", "")))


def conversation_capability(fabric: Any) -> tuple[bool, str]:
    """Return ``(available, reason)`` for the model conversation path."""
    if fabric is None:
        return False, "no model fabric is attached"
    candidates = live_model_candidates(fabric)
    if not candidates:
        return False, ("no runtime-verified live model is attached "
                       "(a configured provider is not a live model)")
    names = ", ".join(str(getattr(model, "name", "?")) for model in candidates[:5])
    return True, f"live model(s) available: {names}"


def _capabilities(model: Any) -> tuple[str, ...]:
    values = getattr(model, "capabilities", ()) or ()
    return tuple(str(value) for value in values if str(value))


def choose_capability(model: Any, preferred: str = "reasoning") -> str:
    """Pick a capability this model actually declares."""
    declared = _capabilities(model)
    if preferred in declared:
        return preferred
    for capability in declared:
        if capability in ("text", "chat", "conversation"):
            return capability
    return declared[0] if declared else preferred


class ModelConversationalist:
    """Answer open-ended conversation with a runtime-verified live model."""

    def __init__(self, fabric: Any, *, caller: str = "conversation:model",
                 max_output_tokens: int = 700, temperature: float = 0.4,
                 timeout: float | None = None) -> None:
        self.fabric = fabric
        self.caller = caller
        self.max_output_tokens = max(64, int(max_output_tokens))
        self.temperature = float(temperature)
        self.timeout = timeout
        self.last_error = ""
        self.last_route: dict[str, Any] = {}

    # -- availability -------------------------------------------------------

    def available(self) -> bool:
        return conversation_capability(self.fabric)[0]

    def unavailable_reason(self) -> str:
        return conversation_capability(self.fabric)[1]

    def status(self) -> dict[str, Any]:
        available, reason = conversation_capability(self.fabric)
        candidates = live_model_candidates(self.fabric)
        return {
            "path": "model" if available else "deterministic",
            "available": available,
            "reason": reason,
            "models": [str(getattr(model, "name", "?")) for model in candidates],
            "last_error": self.last_error,
        }

    # -- prompting ----------------------------------------------------------

    @staticmethod
    def _render_history(history: Iterable[Any]) -> str:
        lines: list[str] = []
        for item in list(history)[-MAX_HISTORY_TURNS:]:
            role = ""
            text = ""
            if isinstance(item, dict):
                role = str(item.get("role", ""))
                text = str(item.get("text", ""))
            else:
                role = str(getattr(item, "role", ""))
                text = str(getattr(item, "text", ""))
            if not text.strip():
                continue
            lines.append(f"{role or 'user'}: {text.strip()[:600]}")
        return "\n".join(lines)[-MAX_STATE_CHARS:]

    def build_prompt(self, message: str, *, history: Iterable[Any] = (),
                     state: str = "") -> str:
        sections = [SYSTEM_PREAMBLE]
        if state.strip():
            sections.append("Known Forge state (real values only):\n"
                            + state.strip()[:MAX_STATE_CHARS])
        rendered = self._render_history(history)
        if rendered:
            sections.append("Conversation so far:\n" + rendered)
        sections.append("User: " + str(message).strip()[:MAX_MESSAGE_CHARS])
        sections.append("Forge:")
        return "\n\n".join(sections)

    # -- answering ----------------------------------------------------------

    def reply(self, message: str, *, history: Iterable[Any] = (),
              state: str = "") -> ModelReply | None:
        """Return a live-model reply, or ``None`` when none can be produced.

        ``None`` is the honest answer: the caller falls back to the
        deterministic channel and reports that provenance instead.
        """
        self.last_error = ""
        if not isinstance(message, str) or not message.strip():
            self.last_error = "empty message"
            return None
        candidates = live_model_candidates(self.fabric)
        if not candidates:
            self.last_error = self.unavailable_reason()
            return None
        model = candidates[0]
        name = str(getattr(model, "name", "") or "")
        provider = str(getattr(model, "provider", "") or "")
        request = ModelRequest(
            prompt=self.build_prompt(message, history=history, state=state),
            task=("Answer the user's message conversationally. You cannot "
                  "execute actions in this channel."),
            caller=self.caller,
            capability=choose_capability(model),
            required_capabilities=(),
            model=name,
            require_verified=True,
            max_output_tokens=self.max_output_tokens,
            temperature=self.temperature,
            complexity=1.0,
            timeout=self.timeout,
            metadata={"channel": "conversation", "agent": "forge-conversation"},
        )
        started = perf_counter()
        try:
            response = self.fabric.generate(request)
        except Exception as exc:
            self.last_error = f"model call failed: {type(exc).__name__}"
            return None
        latency = (perf_counter() - started) * 1000.0
        self.last_route = {
            "model": getattr(response, "model", ""),
            "provider": getattr(response, "provider", ""),
            "success": bool(getattr(response, "success", False)),
        }
        if not bool(getattr(response, "success", False)):
            self.last_error = (str(getattr(response, "error", "") or "")
                               or "model returned no answer")
            return None
        text = str(getattr(response, "text", "") or "").strip()
        if not text:
            self.last_error = "model returned an empty answer"
            return None
        served_model = str(getattr(response, "model", "") or name)
        served_provider = str(getattr(response, "provider", "") or provider)
        try:
            if is_fallback_response(self.fabric, served_model, served_provider):
                self.last_error = "only the offline placeholder answered"
                return None
        except Exception:
            pass
        return ModelReply(text=text[:MAX_REPLY_CHARS], model=served_model,
                          provider=served_provider, latency_ms=latency)
