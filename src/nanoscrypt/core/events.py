from typing import Any, Literal
from pydantic import BaseModel, Field


class AgentEvent(BaseModel):
    type: str = Field(..., description="The type identifier for the event")


class AgentStartEvent(AgentEvent):
    type: Literal["agent_start"] = "agent_start"


class AgentEndEvent(AgentEvent):
    type: Literal["agent_end"] = "agent_end"


class TurnStartEvent(AgentEvent):
    type: Literal["turn_start"] = "turn_start"
    turn: int


class TurnEndEvent(AgentEvent):
    type: Literal["turn_end"] = "turn_end"
    turn: int


class ProgressEvent(AgentEvent):
    """A concise, user-safe description of the current execution stage."""

    type: Literal["progress"] = "progress"
    message: str


class SandboxOperationEvent(AgentEvent):
    """The Nebius operation identifier for a submitted sandbox run."""

    type: Literal["sandbox_operation"] = "sandbox_operation"
    operation_id: str


class SandboxOutputEvent(AgentEvent):
    """A stdout/stderr chunk received live from a remote sandbox."""

    type: Literal["sandbox_output"] = "sandbox_output"
    channel: Literal["stdout", "stderr"]
    text: str


class MessageStartEvent(AgentEvent):
    type: Literal["message_start"] = "message_start"
    message_role: str


class MessageDeltaEvent(AgentEvent):
    type: Literal["message_delta"] = "message_delta"
    delta: str


class MessageEndEvent(AgentEvent):
    type: Literal["message_end"] = "message_end"
    message: Any  # Can hold UserMessage, AssistantMessage, etc.


class ThinkingDeltaEvent(AgentEvent):
    type: Literal["thinking_delta"] = "thinking_delta"
    delta: str


class ToolExecutionStartEvent(AgentEvent):
    type: Literal["tool_execution_start"] = "tool_execution_start"
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionEndEvent(AgentEvent):
    type: Literal["tool_execution_end"] = "tool_execution_end"
    tool_name: str
    success: bool
    output: str | None = None
    error: str | None = None


class ErrorEvent(AgentEvent):
    type: Literal["error"] = "error"
    message: str
    recoverable: bool = True
    data: dict[str, Any] | None = None


class RetryEvent(AgentEvent):
    type: Literal["retry"] = "retry"
    attempt: int
    max_attempts: int
    delay_seconds: float
    message: str
    data: dict[str, Any] | None = None


class QueueUpdateEvent(AgentEvent):
    type: Literal["queue_update"] = "queue_update"
    steering: tuple[str, ...] = ()
    follow_up: tuple[str, ...] = ()
