"""Forge Personal Assistant Core (A84 Stages B, C, L, M, N, Q, S).

One continuing assistant in front of the existing platform — not a second
system. The plane of responsibility is:

```
User
  ↓  Personal Assistant Interface (AssistantCore)
  ↓  Conversation + intent understanding (deterministic classifier + Prompt Intelligence)
  ↓  Context / Memory (SessionLedger, PersonalMemoryService, ContextEngine)
  ↓  Prompt enhancement (forge.prompt_intelligence)
  ↓  Planning (behavior triage: answer / research / code / orchestrate / ask)
  ↓  Specialist selection (ModelTeam + AgentSelector over the existing fleet)
  ↓  Model Fabric (the single model authority — routing, policy, failover)
  ↓  Tools (forge.tools.intelligence registry/planner/verifier behind A33)
  ↓  Cross-verification (forge.verification critic/verifier loop)
  ↓  Synthesis → personalized response → controlled memory update
```

Everything below "Model Fabric" and every permission check *is* the existing
infrastructure: the supervisor task pipeline, the A33 policy gates, the A37
memory plane, the A38 orchestrations, the A81 research engine, voice and the
cockpit remain the only execution paths. ``AssistantCore`` never writes to a
repository, never runs a command and never grants itself anything: it plans,
delegates, and integrates results — and where the underlying capability is
not live, the response says so (Stage R).
"""
from forge.assistant.behavior import Action, AssistantBehavior
from forge.assistant.context import ContextBundle, ContextEngine, ContextQuality
from forge.assistant.continuity import ContinuityBundle, ContinuityResolver
from forge.assistant.memory import PersonalMemoryService, RetentionDecision
from forge.assistant.personalization import PreferenceProfile
from forge.assistant.sessions import AssistantSession, SessionLedger
from forge.assistant.core import AssistantCore, AssistantResponse

__all__ = [
    "Action",
    "AssistantBehavior",
    "AssistantCore",
    "AssistantResponse",
    "AssistantSession",
    "ContextBundle",
    "ContextEngine",
    "ContextQuality",
    "ContinuityBundle",
    "ContinuityResolver",
    "PersonalMemoryService",
    "PreferenceProfile",
    "RetentionDecision",
    "SessionLedger",
]
