from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class DBTool(Base):
    __tablename__ = "tools"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, unique=True, index=True, nullable=False)
    purpose = Column(Text, nullable=False)
    language = Column(String, default="python", nullable=False)
    entry_point = Column(String, default="tool.py", nullable=False)
    dependencies = Column(JSON, default=list, nullable=False)  # List of strings
    input_schema = Column(JSON, default=dict, nullable=False)  # Expected inputs
    output_schema = Column(JSON, default=dict, nullable=False)  # Expected outputs
    tags = Column(JSON, default=list, nullable=False)  # Categorization tags
    current_version = Column(Integer, default=1, nullable=False)
    success_rate = Column(Float, default=0.0, nullable=False)
    usage_count = Column(Integer, default=0, nullable=False)
    status = Column(
        String, default="active", nullable=False
    )  # active, deprecated, failed
    created_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    last_used = Column(DateTime, nullable=True)

    versions = relationship(
        "DBToolVersion", back_populates="tool", cascade="all, delete-orphan"
    )
    executions = relationship(
        "DBToolExecution", back_populates="tool", cascade="all, delete-orphan"
    )
    lifecycle = relationship(
        "DBToolLifecycle", back_populates="tool", uselist=False,
        cascade="all, delete-orphan"
    )
    version_lifecycles = relationship(
        "DBToolVersionLifecycle", back_populates="tool", cascade="all, delete-orphan"
    )


class DBToolVersion(Base):
    __tablename__ = "tool_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tool_id = Column(Integer, ForeignKey("tools.id"), nullable=False)
    version = Column(Integer, nullable=False)
    code_hash = Column(String, nullable=False)
    prompt_used = Column(Text, nullable=False)
    change_reason = Column(Text, default="initial", nullable=False)
    test_results = Column(JSON, default=dict, nullable=False)  # test outcome metrics
    runtime_stats = Column(
        JSON, default=dict, nullable=False
    )  # avg latency, max memory, etc.
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    tool = relationship("DBTool", back_populates="versions")


class DBToolExecution(Base):
    __tablename__ = "tool_executions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tool_id = Column(Integer, ForeignKey("tools.id"), nullable=False)
    version = Column(Integer, nullable=False)
    success = Column(Boolean, nullable=False)
    input_data = Column(JSON, default=dict, nullable=False)
    output_data = Column(JSON, default=dict, nullable=True)
    error = Column(Text, nullable=True)
    runtime_ms = Column(Integer, nullable=False)
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    completed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    tool = relationship("DBTool", back_populates="executions")
    outcome = relationship(
        "DBToolOutcome", back_populates="execution", uselist=False,
        cascade="all, delete-orphan"
    )


class DBToolLifecycle(Base):
    """Legacy tool-level lifecycle state retained for database migration."""

    __tablename__ = "tool_lifecycle"

    tool_id = Column(Integer, ForeignKey("tools.id"), primary_key=True)
    state = Column(String, default="candidate", nullable=False)
    reason = Column(Text, default="Awaiting task-outcome evidence", nullable=False)
    updated_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc), nullable=False
    )
    tool = relationship("DBTool", back_populates="lifecycle")


class DBToolVersionLifecycle(Base):
    """Availability state bound to one immutable tool version."""

    __tablename__ = "tool_version_lifecycles"

    tool_id = Column(Integer, ForeignKey("tools.id"), primary_key=True)
    version = Column(Integer, primary_key=True)
    state = Column(String, default="candidate", nullable=False)
    reason = Column(Text, default="Awaiting task-outcome evidence", nullable=False)
    updated_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc), nullable=False
    )
    tool = relationship("DBTool", back_populates="version_lifecycles")


class DBToolOutcome(Base):
    """Caller-supplied evidence about whether an execution helped its task."""

    __tablename__ = "tool_outcomes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    execution_id = Column(
        Integer, ForeignKey("tool_executions.id"), unique=True, nullable=False
    )
    task_key = Column(String, nullable=False, index=True)
    contribution = Column(String, nullable=False)  # helpful, neutral, harmful
    evidence = Column(Text, nullable=False)
    created_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    execution = relationship("DBToolExecution", back_populates="outcome")


# --- ENTERPRISE TABLES V0.2.0 ---


class DBMemoryEntry(Base):
    __tablename__ = "memory_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String, unique=True, index=True, nullable=False)
    value = Column(Text, nullable=False)
    category = Column(String, default="general", nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class DBAuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String, index=True, nullable=False)
    session_id = Column(String, index=True, nullable=False)
    agent_name = Column(String, nullable=False)
    details = Column(JSON, default=dict, nullable=False)
    cost = Column(Float, default=0.0, nullable=False)
    token_usage = Column(Integer, default=0, nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class DBAgentDefinition(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, unique=True, index=True, nullable=False)
    role = Column(String, nullable=False)
    goal = Column(Text, nullable=False)
    backstory = Column(Text, default="", nullable=False)
    tools = Column(JSON, default=list, nullable=False)
    permissions = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class DBApprovalRecord(Base):
    __tablename__ = "approvals"

    id = Column(String, primary_key=True)
    session_id = Column(String, index=True, nullable=False)
    approval_type = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    risk_level = Column(String, nullable=False)
    resource_details = Column(JSON, default=dict, nullable=False)
    agent_name = Column(String, default="orchestrator", nullable=False)
    status = Column(String, default="pending", nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    resolved_at = Column(DateTime, nullable=True)
    reason = Column(Text, nullable=True)


class DBSemanticMemory(Base):
    __tablename__ = "semantic_memories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, index=True, nullable=False, default="default_user")
    agent_id = Column(String, default="orchestrator", nullable=False)
    text = Column(Text, nullable=False)
    metadata_json = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class DBRepairAttempt(Base):
    __tablename__ = "repair_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String, index=True, nullable=False)
    tool_name = Column(String, index=True, nullable=False)
    attempt_number = Column(Integer, nullable=False)
    strategy = Column(String, nullable=False)
    error_classification = Column(String, nullable=False)
    generated_code = Column(Text, nullable=False)  # Full detail, not compressed
    diff = Column(Text, nullable=False)
    stdout = Column(Text, nullable=True)
    stderr = Column(Text, nullable=True)
    return_code = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    # Link: "this repair came from that repair"
    parent_repair_id = Column(Integer, ForeignKey("repair_attempts.id"), nullable=True)

    parent_attempt = relationship(
        "DBRepairAttempt", remote_side=[id], backref="child_attempts"
    )


