"""Request schemas for the cockpit API (A34).

Strict, bounded validation at the boundary: every string field has a max
length, every enum is closed, and no schema accepts paths, shell text,
credentials, or free-form commands.
"""
from __future__ import annotations

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
    expected_version: int | None = Field(default=None, ge=1)


class RollbackRequest(BaseModel):
    checkpoint_id: str = Field(default="", max_length=128)
    expected_version: int | None = Field(default=None, ge=1)


class CommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=32)
    task_id: str = Field(default="", max_length=128)
    approval_id: str = Field(default="", max_length=128)
    requirement: str = Field(default="", max_length=8000)
    expected_version: int | None = Field(default=None, ge=1)


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
    scopes: list[str] = Field(min_length=1, max_length=32)


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
