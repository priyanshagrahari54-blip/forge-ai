"""Voice command foundation (A33) with safe deterministic query handling.

Voice remains a policy-gated request source. Informational arithmetic is
handled locally with an AST allow-list; executable actions still require the
normal permission system.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from forge.security.approvals import ApprovalRequest, ApprovalStore, enforce_with_token
from forge.security.audit import AuditLog
from forge.security.policy import PermissionPolicy, PermissionRequest, Resource
from forge.security.policy_gate import PolicyDecision
from forge.voice.math import MathExpressionError, calculate, format_result


@dataclass(frozen=True)
class VoiceCommand:
    """A transcribed voice command (transcription itself is out of scope)."""

    text: str
    agent: str = "VoiceInterface"
    task_id: str = ""


@dataclass(frozen=True)
class VoiceIntent:
    """Parsed intent: WHAT the speaker asked for, with extracted slots."""

    name: str
    slots: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    raw: str = ""

    @property
    def known(self) -> bool:
        return self.name != "unknown" and self.confidence > 0.0


@dataclass(frozen=True)
class VoicePermission:
    """Evaluated authorization for one voice intent."""

    allowed: bool
    decision: PolicyDecision
    reason: str = ""
    approval_required: bool = False
    approval_request_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "decision": self.decision.value,
            "reason": self.reason,
            "approval_required": self.approval_required,
            "approval_request_id": self.approval_request_id,
        }


@dataclass
class VoiceCommandResult:
    ok: bool
    intent: VoiceIntent
    message: str = ""
    task: dict[str, Any] | None = None
    permission: VoicePermission | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "intent": self.intent.name,
            "slots": dict(self.intent.slots),
            "message": self.message,
            "task": self.task,
            "permission": self.permission.to_dict() if self.permission else None,
        }


#: Natural-language command templates.  Voice is intentionally
#: tolerant about conversational phrasing while execution remains
#: permission-gated.  Each entry is (pattern, intent, slot names).
_TEMPLATES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (r"(?:hey )?forge(?:,)? (?:update|change|modify) (?:the )?website", "update_website", ()),
    (r"(?:please )?(?:summarize|summary of|give me a summary of) (.+)", "summarize", ("target",)),
    (r"(?:please )?(?:run|execute) (?:the )?(?:full )?tests?(?: suite)?", "run_tests", ()),
    (r"(?:please )?(?:commit|save) (?:the )?(?:current )?changes", "commit", ()),
    (r"(?:please )?(?:review|check) (.+)", "review", ("target",)),
    (r"(?:what(?:'s| is) )?(?:the )?status|how(?:'s| is) (?:forge|the project) doing", "status", ()),
    (r"(?:help|what can you do|what can forge do|show me what you can do)", "help", ()),
    (r"(?:hello|hi|hey|good morning|good afternoon|good evening)(?: forge)?", "greeting", ()),
    (r"(?:cancel|never mind|forget it)", "cancel", ()),
    (r"(?:calculate|compute|what is|what's) (.+)", "calculate", ("expression",)),
    (r"(?:send|write|draft) (?:an )?email(?: to)? (.+)", "send_email", ("target",)),
    (r"(?:send|message) (?:a )?(?:whatsapp|whats app)(?: message)?(?: to)? (.+)", "send_whatsapp", ("target",)),
    (r"(?:call|phone) (.+)", "make_call", ("target",)),
    # Common Hindi / Hinglish speech-recognition variants.
    (r"(?:namaste|namaskar|pranam)(?: forge)?", "greeting", ()),
    # Devanagari output from hi-IN browser speech recognition.
    (r"(?:हेलो|हैलो|नमस्ते|नमस्कार)(?: फोर्ज| फोर्स)?", "greeting", ()),
    (r"(?:हेलो|हैलो|नमस्ते|नमस्कार)(?: फोर्ज| फोर्स)? (?:क्या हाल-चाल|क्या हाल चाल|कैसे हो|कैसा चल रहा है)", "how_are_you", ()),
    (r"(?:क्या हाल-चाल|क्या हाल चाल|कैसे हो|कैसा चल रहा है)(?: फोर्ज| फोर्स)?", "how_are_you", ()),
    (r"(?:तुम कौन हो|आप कौन हो|तुम क्या कर रहे हो|आप क्या कर रहे हो)", "identity", ()),
    (r"(?:तुम क्या कर सकते हो|आप क्या कर सकते हो|मैं क्या बोलूँ|मुझे बताओ क्या कर सकते हो)", "help", ()),
    (r"(?:स्टेटस बताओ|प्रोजेक्ट का स्टेटस बताओ|फोर्ज का स्टेटस बताओ)", "status", ()),
    (r"(?:टेस्ट चलाओ|टेस्ट रन करो)", "run_tests", ()),
    (r"(?:कोड रिव्यू करो|कोड को रिव्यू करो)", "review", ("target",)),
    (r"(?:वेबसाइट अपडेट करो|वेबसाइट को अपडेट करो|वेबसाइट बदलो|वेबसाइट चेंज करो)", "update_website", ()),
    (r"(?:kaise ho|kaisa ho|kya haal hai|kaise chal raha hai)(?: forge)?", "how_are_you", ()),
    (r"(?:tum kaun ho|aap kaun ho|who are you)(?: forge)?", "identity", ()),
    (r"(?:tum kya kar rahe ho|aap kya kar rahe ho|what are you doing)(?: forge)?", "activity", ()),
    (r"(?:kya kar sakte ho|tum kya kar sakte ho|aap kya kar sakte ho|main kya bolu|mujhe batao kya kar sakte ho)", "help", ()),
    (r"(?:status batao|project ka status batao|forge ka status batao)", "status", ()),
    (r"(?:tests? chalao|test chalao|tests? run karo)", "run_tests", ()),
    (r"(?:code review karo|code ko review karo|review karo)", "review", ("target",)),
    (r"(?:website update karo|website ko update karo|website badlo|website change karo)", "update_website", ()),
    (r"(?:changes commit karo|changes save karo|commit kar do)", "commit", ()),
)


class VoiceInterface:
    """Parse voice commands and route them through permission evaluation."""

    def __init__(self, policy: PermissionPolicy | None = None,
                 store: ApprovalStore | None = None,
                 audit: AuditLog | None = None) -> None:
        # No policy means no authorization: fail closed for actions.
        self.policy = policy if policy is not None else PermissionPolicy()
        self.store = store
        self.audit = audit

    def parse(self, command: VoiceCommand) -> VoiceIntent:
        """Match a command against deterministic templates."""
        text = command.text.strip().lower()
        # Speech recognition can emit commas/dashes/dandas and inconsistent
        # whitespace. Normalize those before deterministic intent matching.
        text = re.sub(r"[，,]+", " ", text)
        text = re.sub(r"[–—−]", "-", text)
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"^(?:forge|फोर्ज|फोर्स)[,\s]+", "", text).strip()
        text = re.sub(r"[.?!।]+$", "", text).strip()
        # Common Hindi conversational speech should not depend on one exact
        # transcription variant. Keep this deterministic and side-effect free.
        # Hindi conversational matching is keyword-based after normalization,
        # because browser STT may omit punctuation or split compound words.
        if (
            re.search(r"(?:हेलो|हैलो|नमस्ते|नमस्कार)", text)
            and re.search(r"(?:हाल|कैसे हो|कैसा चल रहा)", text)
        ) or re.search(r"(?:^| )(?:क्या हाल|कैसे हो|कैसा चल रहा)(?: |$)", text):
            return VoiceIntent("how_are_you", {}, 1.0, command.text)
        if re.search(r"(?:तुम कौन हो|आप कौन हो)", text):
            return VoiceIntent("identity", {}, 1.0, command.text)
        # Bare arithmetic is a safe, side-effect-free query.
        if re.fullmatch(r"[0-9+\-*/%.() ×÷ ]+", text):
            return VoiceIntent("calculate", {"expression": text}, 1.0, command.text)
        for pattern, name, slots in _TEMPLATES:
            match = re.fullmatch(pattern, text)
            if match:
                return VoiceIntent(
                    name=name,
                    slots=dict(zip(slots, match.groups())),
                    confidence=1.0, raw=command.text)
        return VoiceIntent(name="unknown", confidence=0.0, raw=command.text)

    def check(self, command: VoiceCommand) -> tuple[VoiceIntent, VoicePermission]:
        """Parse and evaluate a command without acting on it."""
        intent = self.parse(command)
        if not intent.known:
            return intent, VoicePermission(
                False, PolicyDecision.DENY, "Unknown voice intent")
        permission = PermissionRequest(
            agent=command.agent, resource=Resource.VOICE, operation="command",
            scope=intent.name, task_id=command.task_id,
            reason=f"voice intent {intent.name}")
        evaluation = self.policy.evaluate(permission)
        if self.audit is not None:
            self.audit.record_evaluation(permission, evaluation)
        if evaluation.decision == PolicyDecision.ALLOW:
            return intent, VoicePermission(True, evaluation.decision,
                                           evaluation.reason)
        if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL:
            return intent, VoicePermission(
                False, evaluation.decision, evaluation.reason,
                approval_required=True,
                approval_request_id=self._file_approval(command, intent))
        return intent, VoicePermission(False, evaluation.decision,
                                       evaluation.reason)

    def handle(self, command: VoiceCommand, *,
               task_factory: Callable[[VoiceIntent], dict[str, Any]] | None = None,
               approval_token_id: str = "") -> VoiceCommandResult:
        """Parse, authorize, and execute a safe query or create a task."""
        intent, permission = self.check(command)
        if not intent.known:
            return VoiceCommandResult(False, intent,
                                      "I heard you, but I need a little more detail to know what you want me to do.",
                                      permission=permission)
        if permission.decision == PolicyDecision.REQUIRE_APPROVAL and approval_token_id:
            probe = PermissionRequest(
                agent=command.agent, resource=Resource.VOICE,
                operation="command", scope=intent.name,
                task_id=command.task_id)
            allowed, reason = enforce_with_token(
                self.store, approval_token_id, probe)
            if allowed:
                permission = VoicePermission(True, PolicyDecision.ALLOW, reason)
        if not permission.allowed:
            return VoiceCommandResult(
                False, intent,
                permission.reason or "Voice command not permitted.",
                permission=permission)
        if intent.name in ("greeting", "help", "cancel", "how_are_you", "identity", "activity"):
            replies = {
                "greeting": "Hi! I’m Forge. I’m listening. Tell me naturally what you want to do.",
                "help": "You can talk naturally. Ask me to run tests, review or update the project, check status, calculate something, or ask me a question.",
                "cancel": "Okay, cancelled. Nothing was executed.",
                "how_are_you": "I’m doing well and I’m ready to work. Tell me what you want me to do.",
                "identity": "I’m Forge, your server-side AI system. I can understand requests, route work to specialist agents, and execute approved tasks.",
                "activity": "I’m ready and listening. Give me a task or ask me a question.",
            }
            return VoiceCommandResult(
                True, intent, replies[intent.name],
                task={"kind": "reply", "text": replies[intent.name]},
                permission=permission)

        if intent.name == "calculate":
            try:
                value = calculate(intent.slots.get("expression", ""))
            except MathExpressionError as exc:
                return VoiceCommandResult(False, intent, str(exc),
                                          permission=permission)
            expression = intent.slots.get("expression", "")
            result = format_result(value)
            return VoiceCommandResult(
                True, intent, f"The answer is {result}.",
                task={"kind": "reply", "text": result,
                      "expression": expression}, permission=permission)
        factory = task_factory or (lambda item: {
            "id": f"voice-{abs(hash(item.name)) % 10_000:04d}",
            "description": item.raw or item.name,
            "intent": item.name, "slots": dict(item.slots)})
        return VoiceCommandResult(True, intent, "Task created.",
                                  task=factory(intent), permission=permission)

    def _file_approval(self, command: VoiceCommand,
                       intent: VoiceIntent) -> str:
        if self.store is None:
            return ""
        request = ApprovalRequest(
            agent=command.agent, resource=Resource.VOICE, operation="command",
            scopes=(intent.name,), task_id=command.task_id,
            reason=f"Voice command: {command.text!r}",
            consequences="The voice command will create a task.")
        return self.store.submit(request).id
