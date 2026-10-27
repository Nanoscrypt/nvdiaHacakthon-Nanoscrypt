from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SessionCreate(BaseModel):
    session_id: str | None = None


class SessionResponse(BaseModel):
    id: str
    workspace_path: str
    created_at: datetime


class TaskSubmit(BaseModel):
    prompt: str


class TaskResponse(BaseModel):
    status: str
    action_taken: str
    tool_name: str | None = None
    version: int | None = None
    output: str | None = None
    error: str | None = None
    runtime_ms: int | None = None
    execution_id: int | None = None


class ToolResponse(BaseModel):
    name: str
    purpose: str
    language: str
    current_version: int
    success_rate: float
    usage_count: int
    status: str
    created_at: datetime
    lifecycle_state: str = "shared"


class ToolOutcomeSubmit(BaseModel):
    execution_id: int
    task_key: str = Field(..., min_length=1, max_length=200)
    contribution: Literal["helpful", "neutral", "harmful"]
    evidence: str = Field(..., min_length=3, max_length=2000)


class ToolLifecycleAction(BaseModel):
    state: Literal["candidate", "quarantined", "retired"]
    reason: str = Field(..., min_length=3, max_length=1000)
    version: int | None = Field(default=None, ge=1)


# --- ENTERPRISE SCHEMAS V0.2.0 ---


class AgentPermissionsSchema(BaseModel):
    file_system: str = "deny"
    network: str = "deny"
    tool_generation: str = "execute"
    tool_execution: str = "execute"
    delegation: bool = False


class AgentCreate(BaseModel):
    name: str
    role: str
    goal: str
    backstory: str = ""
    tools: list[str] = Field(default_factory=list)
    permissions: AgentPermissionsSchema = Field(default_factory=AgentPermissionsSchema)


class AgentResponse(BaseModel):
    name: str
    role: str
    goal: str
    backstory: str
    tools: list[str]
    permissions: AgentPermissionsSchema
    created_at: datetime


class ApprovalRecordResponse(BaseModel):
    id: str
    session_id: str
    approval_type: str
    description: str
    risk_level: str
    resource_details: dict[str, Any]
    agent_name: str
    status: str
    timestamp: datetime
    resolved_at: datetime | None = None
    reason: str | None = None


class ApprovalResolution(BaseModel):
    approved: bool
    reason: str | None = None


class AuditLogResponse(BaseModel):
    id: int
    event_type: str
    session_id: str
    agent_name: str
    details: dict[str, Any]
    cost: float
    token_usage: int
    timestamp: datetime
