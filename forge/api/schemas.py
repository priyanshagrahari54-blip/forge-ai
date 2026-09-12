"""Request schemas for the cockpit API (A34).

Strict, bounded validation at the boundary: every string field has a max
length, every enum is closed, and no schema accepts paths, shell text,
credentials, or free-form commands.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

PROFILE_VALUES = ("safe", "assisted", "autonomous", "locked")
COMMAND_VALUES = ("START_TASK", "PAUSE_TASK", "RESUME_TASK", "CANCEL_TASK",
                  "RETRY_TASK", "APPROVE", "DENY", "ROLLBACK")


class CreateSessionRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    profile: str = Field(default="assisted", max_length=32)


class CreateTaskRequest(BaseModel):
    requirement: str = Field(min_length=1, max_length=8000)
    mode: str = Field(default="", max_length=32)


class TaskActionRequest(BaseModel):
    expected_version: Optional[int] = Field(default=None, ge=1)


class RollbackRequest(BaseModel):
    checkpoint_id: str = Field(default="", max_length=128)
    expected_version: Optional[int] = Field(default=None, ge=1)


class CommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=32)
    task_id: str = Field(default="", max_length=128)
    approval_id: str = Field(default="", max_length=128)
    requirement: str = Field(default="", max_length=8000)
    expected_version: Optional[int] = Field(default=None, ge=1)


class InterpretRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class VoiceRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class VoiceProcessRequest(BaseModel):
    text: str = Field(default="", max_length=2000)
    # base64 of a bounded WAV; 760k chars ≈ 570 KB decoded.
    audio_b64: str = Field(default="", max_length=760_000)
    approval_id: str = Field(default="", max_length=128)
    task_id: str = Field(default="", max_length=128)
    require_wake: bool = True
    # A42 conversation: require an explicit confirmation before acting.
    confirm: bool = True


class VoiceSynthesizeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class VoiceTranscribeRequest(BaseModel):
    audio_b64: str = Field(min_length=1, max_length=760_000)


class MemorySaveRequest(BaseModel):
    kind: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=20_000)
    approval_id: str = Field(default="", max_length=128)


class MemoryProjectSaveRequest(BaseModel):
    key: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=20_000)
    approval_id: str = Field(default="", max_length=128)


class MemoryDeleteRequest(BaseModel):
    entry_id: str = Field(min_length=1, max_length=128)
    approval_id: str = Field(default="", max_length=128)


class DesktopCheckRequest(BaseModel):
    action: str = Field(min_length=1, max_length=64)
    target: str = Field(default="", max_length=500)
    params: dict = Field(default_factory=dict)
    task_id: str = Field(default="", max_length=128)


class DesktopActRequest(BaseModel):
    action: str = Field(min_length=1, max_length=64)
    target: str = Field(default="", max_length=500)
    params: dict = Field(default_factory=dict)
    reason: str = Field(default="", max_length=500)
    task_id: str = Field(default="", max_length=128)
    approval_id: str = Field(default="", max_length=128)


class DesktopGrantRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=128)
    scopes: List[str] = Field(min_length=1, max_length=32)


class DesktopDecisionRequest(BaseModel):
    approval_id: str = Field(min_length=1, max_length=128)


class Pagination(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=100000)


# -- A38 orchestration ---------------------------------------------------------

class OrchestrationSubmitRequest(BaseModel):
    requirement: str = Field(min_length=1, max_length=8000)
    chain: bool = False


# -- A39 vision ---------------------------------------------------------------

class VisionAnalyzeRequest(BaseModel):
    image_b64: str = Field(min_length=1, max_length=7_000_000)
    approval_id: str = ""


# -- A40 computer use -----------------------------------------------------------

class ComputerActRequest(BaseModel):
    action: str = Field(min_length=1, max_length=64)
    target: str = Field(default="", max_length=500)
    params: dict = Field(default_factory=dict)
    reason: str = Field(default="", max_length=1000)
    approval_id: str = Field(default="", max_length=200)


class ComputerScreenRequest(BaseModel):
    image_b64: str = Field(min_length=1, max_length=7_000_000)
    goal: str = Field(default="", max_length=1000)


# -- A43 general conversation ----------------------------------------------------

class ConversationRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


# -- A44 AI-to-AI collaboration ----------------------------------------------------

class CollaborationConsultRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    provider: str = Field(default="simulated-external", max_length=128)
    approval_id: str = Field(default="", max_length=200)


# -- A45 AI council -------------------------------------------------------------------

class CouncilQuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


# -- A46 model fabric bridge ----------------------------------------------------------

class ModelGenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    capability: str = Field(default="coding", max_length=64)
    approval_id: str = Field(default="", max_length=200)


# -- A47 research / intelligence --------------------------------------------------------

class ResearchQuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class ResearchQueryRequest(BaseModel):
    """Secure multi-source research query (provenance-tracked)."""

    question: str = Field(min_length=1, max_length=2000)
    allow_web: bool = True
    sources: List[str] = Field(default_factory=list, max_length=8)
    user_notes: List[str] = Field(default_factory=list, max_length=10)
    allow_model_knowledge: bool = False


# -- A48 compute -------------------------------------------------------------------------

class ComputeExecuteRequest(BaseModel):
    code: str = Field(min_length=1, max_length=6000)
    timeout: Optional[float] = Field(default=None, gt=0, le=120)
    approval_id: str = Field(default="", max_length=200)
    backend: str = Field(default="local", max_length=32)


# -- A49 agent creation -------------------------------------------------------------------

class AgentCreateRequest(BaseModel):
    name: str = Field(min_length=3, max_length=48)
    role: str = Field(min_length=2, max_length=32)
    capabilities: List[str] = Field(min_length=1, max_length=12)
    description: str = Field(default="", max_length=500)
    bind: bool = Field(default=False)


class AgentUpdateRequest(BaseModel):
    role: str = Field(default="", max_length=32)
    capabilities: Optional[List[str]] = Field(default=None, max_length=12)
    description: Optional[str] = Field(default=None, max_length=500)


# -- A50 agent evolution -------------------------------------------------------------------

class AgentOutcomeRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=128)


# -- A51 agent execution -------------------------------------------------------------------

class AgentRunRequest(BaseModel):
    requirement: str = Field(min_length=1, max_length=4000)
    approval_id: str = Field(default="", max_length=200)


# -- A52 agent teams -------------------------------------------------------------------------

class TeamCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    members: List[str] = Field(min_length=1, max_length=6)


class TeamExecuteRequest(BaseModel):
    requirement: str = Field(min_length=1, max_length=4000)
    approval_id: str = Field(default="", max_length=200)


# -- A53 agent memory -------------------------------------------------------------------------

class AgentMemorySetRequest(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=2000)


# -- A54 agent skills -------------------------------------------------------------------------

class SkillCreateRequest(BaseModel):
    name: str = Field(min_length=3, max_length=48)
    capability: str = Field(min_length=2, max_length=64)
    description: str = Field(default="", max_length=500)
    version: str = Field(default="1.0.0", max_length=32)


class SkillAttachRequest(BaseModel):
    skill: str = Field(min_length=3, max_length=48)


# -- A55 agent lifecycle -----------------------------------------------------------------------

class AgentStatusRequest(BaseModel):
    status: str = Field(min_length=2, max_length=16)


# -- A56 agent packaging -----------------------------------------------------------------------

class AgentImportRequest(BaseModel):
    payload: dict


# -- A57 agent governance -----------------------------------------------------------------------

class AgentLimitsRequest(BaseModel):
    max_runs_per_hour: int = Field(default=60, ge=1, le=1000)
    max_concurrent: int = Field(default=2, ge=1, le=20)


# -- A58 agent self-development -----------------------------------------------------------------

class SelfDevApplyRequest(BaseModel):
    proposal_id: str = Field(min_length=6, max_length=32)


# -- A59 failure learning ----------------------------------------------------------------------

class FailureRecordRequest(BaseModel):
    category: str = Field(min_length=2, max_length=16)
    error: str = Field(min_length=1, max_length=500)


# -- A60 model benchmarking ---------------------------------------------------------------------

class BenchmarkRequest(BaseModel):
    models: Optional[List[str]] = None


# -- A64 deployment ----------------------------------------------------------------------------

class DeploymentCreateRequest(BaseModel):
    name: str = Field(min_length=3, max_length=48)
    version: str = Field(min_length=1, max_length=32)


class DeploymentDeployRequest(BaseModel):
    target: str = Field(min_length=1, max_length=512)


# -- A65 backup & recovery ----------------------------------------------------------------------

class BackupCreateRequest(BaseModel):
    label: str = Field(min_length=1, max_length=64)


# -- A66 plugin SDK -----------------------------------------------------------------------------

class PluginInstallRequest(BaseModel):
    manifest: dict


# -- A69 autonomy levels -------------------------------------------------------------------------

class AutonomyLevelRequest(BaseModel):
    level: str = Field(min_length=2, max_length=16)


# -- A72 final verification -----------------------------------------------------------------------

class FinalVerifyRunRequest(BaseModel):
    run_id: str = Field(min_length=6, max_length=64)


# -- A74-A80 final gates --------------------------------------------------------------------------

class FinalBenchmarkRequest(BaseModel):
    min_passed: int = Field(default=1, ge=0, le=8)


class FinalGateVerifyRequest(BaseModel):
    """Explicit A80 provider-capability verification request.

    ``provider`` empty means: verify every currently configured
    provider. Must name one of the known providers otherwise.
    """

    provider: str = Field(default="", max_length=64)


class FinalLoopRequest(BaseModel):
    max_iterations: int = Field(default=3, ge=1, le=5)


# -- A82 staged builds ----------------------------------------------------------------------------

class StagedBuildCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    roadmap: str = Field(default="", max_length=20000)
    blueprint: str = Field(default="", max_length=20000)


class StagedBuildUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)
    roadmap: Optional[str] = Field(default=None, max_length=20000)
    blueprint: Optional[str] = Field(default=None, max_length=20000)


class StagedStageItem(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=4000)


class StagedStagesAddRequest(BaseModel):
    stages: List[StagedStageItem] = Field(min_length=1, max_length=50)


class StagedStageUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=160)
    prompt: Optional[str] = Field(default=None, max_length=4000)


class StagedRunRequest(BaseModel):
    mode: str = Field(default="", max_length=32)


class StagedPreviewUpdateRequest(BaseModel):
    entry: str = Field(default="", max_length=512)
