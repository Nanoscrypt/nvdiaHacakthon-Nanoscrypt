from datetime import datetime, timezone

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from nanoscrypt.core.tool_lifecycle import decide_lifecycle_state
from nanoscrypt.models.database import (
    Base,
    DBTool,
    DBToolExecution,
    DBToolLifecycle,
    DBToolOutcome,
    DBToolVersion,
    DBToolVersionLifecycle,
)
from nanoscrypt.models.tool import GeneratedTool

logger = structlog.get_logger()


class ToolRegistry:
    """Persist tool metadata, execution metrics, and lifecycle evidence."""

    def __init__(self, database_url: str):
        self.engine = create_async_engine(database_url, echo=False)
        self.session_factory = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def initialize_db(self) -> None:
        """Initializes database tables if they do not exist."""
        # Ensure database directory exists for SQLite
        if "sqlite" in self.engine.url.drivername:
            database_path = self.engine.url.database
            if database_path and database_path != ":memory:":
                from pathlib import Path

                db_dir = Path(database_path).parent
                db_dir.mkdir(parents=True, exist_ok=True)

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await self._migrate_legacy_lifecycle_states()

    async def _migrate_legacy_lifecycle_states(self) -> None:
        """Move legacy tool-wide lifecycle state into version-scoped rows.

        Old databases have one lifecycle row per tool. New rows are derived from
        each version's own outcomes so evidence cannot silently cross versions.
        Unattributed legacy state is not enough to share a version safely.
        """
        async with self.session_factory() as session, session.begin():
            result = await session.execute(select(DBTool))
            tools = list(result.scalars().all())
            for tool in tools:
                legacy = await session.get(DBToolLifecycle, tool.id)
                version_result = await session.execute(
                    select(DBToolVersion.version)
                    .where(DBToolVersion.tool_id == tool.id)
                    .order_by(DBToolVersion.version)
                )
                versions = set(version_result.scalars().all())
                versions.add(tool.current_version)

                for version in sorted(versions):
                    existing = await session.get(
                        DBToolVersionLifecycle, (tool.id, version)
                    )
                    if existing is not None:
                        continue

                    outcomes = await self._get_version_outcomes(
                        session, tool.id, version
                    )
                    if (
                        legacy is not None
                        and version == tool.current_version
                        and legacy.state in {"quarantined", "retired"}
                    ):
                        state, reason = legacy.state, legacy.reason
                    else:
                        state, reason = decide_lifecycle_state(
                            "candidate", outcomes
                        )
                        reason = f"Migrated per version: {reason}"

                    session.add(
                        DBToolVersionLifecycle(
                            tool_id=tool.id,
                            version=version,
                            state=state,
                            reason=reason,
                        )
                    )

    @staticmethod
    async def _get_version_outcomes(
        session: AsyncSession, tool_id: int, version: int
    ) -> list[tuple[str, str]]:
        result = await session.execute(
            select(DBToolOutcome.task_key, DBToolOutcome.contribution)
            .join(
                DBToolExecution,
                DBToolExecution.id == DBToolOutcome.execution_id,
            )
            .where(
                DBToolExecution.tool_id == tool_id,
                DBToolExecution.version == version,
            )
            .order_by(DBToolOutcome.created_at.asc(), DBToolOutcome.id.asc())
        )
        return list(result.all())

    async def register(
        self,
        tool: GeneratedTool,
        code_hash: str,
        prompt_used: str,
        change_reason: str = "initial",
    ) -> DBTool:
        """Saves tool metadata and its version details inside the registry."""
        log = logger.bind(component="registry", tool_name=tool.name)
        log.debug("registry_registering_tool")

        async with self.session_factory() as session:
            async with session.begin():
                # Check if tool already exists
                stmt = select(DBTool).where(DBTool.name == tool.name)
                result = await session.execute(stmt)
                db_tool = result.scalar_one_or_none()

                if not db_tool:
                    # Create new tool record
                    db_tool = DBTool(
                        name=tool.name,
                        purpose=tool.manifest.input_schema.get("purpose")
                        or tool.readme[:200],
                        language=tool.manifest.language,
                        entry_point=tool.manifest.entry,
                        dependencies=tool.manifest.dependencies,
                        input_schema=tool.manifest.input_schema,
                        output_schema=tool.manifest.output_schema,
                        tags=[tool.name],
                        current_version=1,
                        success_rate=0.0,
                        usage_count=0,
                        status="active",
                    )
                    session.add(db_tool)
                    # Flush to get the tool ID
                    await session.flush()
                    session.add(
                        DBToolVersionLifecycle(
                            tool_id=db_tool.id,
                            version=1,
                            state="candidate",
                            reason="Awaiting task-outcome evidence",
                        )
                    )
                    version_num = 1
                else:
                    # Increment version
                    version_num = db_tool.current_version + 1
                    db_tool.current_version = version_num
                    db_tool.status = "active"
                    db_tool.updated_at = datetime.now(timezone.utc)
                    db_tool.dependencies = list(
                        set(db_tool.dependencies + tool.manifest.dependencies)
                    )
                    session.add(
                        DBToolVersionLifecycle(
                            tool_id=db_tool.id,
                            version=version_num,
                            state="candidate",
                            reason="New version must earn shared status",
                        )
                    )

                # Add version snapshot details
                db_version = DBToolVersion(
                    tool_id=db_tool.id,
                    version=version_num,
                    code_hash=code_hash,
                    prompt_used=prompt_used,
                    change_reason=change_reason,
                    test_results={"status": "passed"},
                    runtime_stats={"avg_ms": 0},
                )
                session.add(db_version)

            await session.commit()
            log.info("registry_tool_registered", version=version_num)
            return db_tool

    async def get(self, name: str) -> DBTool | None:
        """Retrieves active tool metadata by name."""
        async with self.session_factory() as session:
            stmt = select(DBTool).where(DBTool.name == name, DBTool.status == "active")
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def delete(self, name: str) -> bool:
        """Sets the status of a tool to 'inactive' so it won't be reused."""
        async with self.session_factory() as session:
            async with session.begin():
                stmt = select(DBTool).where(DBTool.name == name)
                result = await session.execute(stmt)
                db_tool = result.scalar_one_or_none()
                if not db_tool:
                    return False
                db_tool.status = "inactive"
                db_tool.updated_at = datetime.now(timezone.utc)
            await session.commit()
            return True

    async def search(
        self, query: str, limit: int = 5, shared_only: bool = False
    ) -> list[DBTool]:
        """Performs simple keyword search matching tool name, purpose, or tags."""
        async with self.session_factory() as session:
            like_pattern = f"%{query}%"
            stmt = (
                select(DBTool)
                .where(
                    DBTool.status == "active",
                    or_(
                        DBTool.name.like(like_pattern),
                        DBTool.purpose.like(like_pattern),
                    ),
                )
                .order_by(DBTool.success_rate.desc(), DBTool.usage_count.desc())
                .limit(limit)
            )
            if shared_only:
                stmt = (
                    select(DBTool)
                    .outerjoin(
                        DBToolVersionLifecycle,
                        (DBToolVersionLifecycle.tool_id == DBTool.id)
                        & (DBToolVersionLifecycle.version == DBTool.current_version),
                    )
                    .where(
                        DBTool.status == "active",
                        DBToolVersionLifecycle.state == "shared",
                        or_(
                            DBTool.name.like(like_pattern),
                            DBTool.purpose.like(like_pattern),
                        ),
                    )
                    .order_by(DBTool.success_rate.desc(), DBTool.usage_count.desc())
                    .limit(limit)
                )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def update_stats(
        self,
        tool_name: str,
        success: bool,
        runtime_ms: int,
        input_data: dict,
        output_data: dict | None = None,
        error: str | None = None,
    ) -> int | None:
        """Updates tool usage counts, metrics, and logs the individual execution run."""
        log = logger.bind(component="registry", tool_name=tool_name)

        async with self.session_factory() as session:
            async with session.begin():
                # Find the tool
                stmt = select(DBTool).where(DBTool.name == tool_name)
                result = await session.execute(stmt)
                db_tool = result.scalar_one_or_none()

                if not db_tool:
                    log.warning("registry_stats_update_skipped_tool_not_found")
                    return

                # Record execution details
                exec_record = DBToolExecution(
                    tool_id=db_tool.id,
                    version=db_tool.current_version,
                    success=success,
                    input_data=input_data,
                    output_data=output_data,
                    error=error,
                    runtime_ms=runtime_ms,
                    started_at=datetime.now(timezone.utc),
                    completed_at=datetime.now(timezone.utc),
                )
                session.add(exec_record)
                await session.flush()
                execution_id = exec_record.id

                # Fetch all execution stats to re-compute success rate
                stmt_all = select(DBToolExecution.success).where(
                    DBToolExecution.tool_id == db_tool.id
                )
                res_all = await session.execute(stmt_all)
                runs = res_all.scalars().all()

                total_runs = len(runs)
                successful_runs = sum(1 for r in runs if r)

                # Update DBTool attributes
                db_tool.usage_count = total_runs
                db_tool.success_rate = float(successful_runs / total_runs)
                db_tool.last_used = datetime.now(timezone.utc)
                db_tool.updated_at = datetime.now(timezone.utc)

            await session.commit()
            log.info(
                "registry_stats_updated",
                total_runs=total_runs,
                success_rate=db_tool.success_rate,
            )
            return execution_id

    async def record_outcome(
        self,
        execution_id: int,
        task_key: str,
        contribution: str,
        evidence: str,
    ) -> dict | None:
        """Record task-level feedback and update only the executed version's state."""
        if contribution not in {"helpful", "neutral", "harmful"}:
            raise ValueError("Contribution must be helpful, neutral, or harmful")
        if not task_key.strip():
            raise ValueError("Task key must not be empty")
        async with self.session_factory() as session:
            async with session.begin():
                execution = await session.get(DBToolExecution, execution_id)
                if execution is None:
                    return None
                prior_outcome = await session.execute(
                    select(DBToolOutcome.id).where(
                        DBToolOutcome.execution_id == execution.id
                    )
                )
                if prior_outcome.scalar_one_or_none() is not None:
                    raise ValueError(
                        "An outcome has already been recorded for this execution"
                    )

                lifecycle = await session.get(
                    DBToolVersionLifecycle,
                    (execution.tool_id, execution.version),
                )
                if lifecycle is None:
                    lifecycle = DBToolVersionLifecycle(
                        tool_id=execution.tool_id,
                        version=execution.version,
                        state="candidate",
                        reason="Version requires its own lifecycle evidence",
                    )
                    session.add(lifecycle)
                    await session.flush()

                session.add(
                    DBToolOutcome(
                        execution_id=execution.id,
                        task_key=task_key.strip().casefold(),
                        contribution=contribution,
                        evidence=evidence.strip(),
                    )
                )
                await session.flush()

                outcomes = await self._get_version_outcomes(
                    session, execution.tool_id, execution.version
                )
                lifecycle.state, lifecycle.reason = decide_lifecycle_state(
                    lifecycle.state, outcomes
                )
                lifecycle.updated_at = datetime.now(timezone.utc)
                tool = await session.get(DBTool, execution.tool_id)
                response = {
                    "tool_name": tool.name if tool else "unknown",
                    "version": execution.version,
                    "execution_id": execution.id,
                    "state": lifecycle.state,
                    "reason": lifecycle.reason,
                    "outcome_count": len(outcomes),
                }
            await session.commit()
            return response

    async def get_lifecycle(self, name: str, version: int | None = None) -> dict | None:
        async with self.session_factory() as session:
            tool_result = await session.execute(
                select(DBTool).where(DBTool.name == name)
            )
            tool = tool_result.scalar_one_or_none()
            if tool is None:
                return None
            selected_version = (
                version if version is not None else tool.current_version
            )
            version_result = await session.execute(
                select(DBToolVersion).where(
                    DBToolVersion.tool_id == tool.id,
                    DBToolVersion.version == selected_version,
                )
            )
            version_record = version_result.scalar_one_or_none()
            if version_record is None:
                return None
            lifecycle = await session.get(
                DBToolVersionLifecycle, (tool.id, selected_version)
            )
            return {
                "tool_name": name,
                "version": selected_version,
                "code_hash": version_record.code_hash if version_record else None,
                "state": lifecycle.state if lifecycle else "shared",
                "reason": (
                    lifecycle.reason
                    if lifecycle
                    else "Existing version has legacy shared status"
                ),
            }

    async def set_lifecycle_state(
        self,
        name: str,
        state: str,
        reason: str,
        version: int | None = None,
    ) -> bool:
        if state not in {"candidate", "quarantined", "retired"}:
            raise ValueError("Unsupported lifecycle state")
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    select(DBTool).where(DBTool.name == name)
                )
                tool = result.scalar_one_or_none()
                if tool is None:
                    return False
                selected_version = (
                    version if version is not None else tool.current_version
                )
                version_result = await session.execute(
                    select(DBToolVersion.version).where(
                        DBToolVersion.tool_id == tool.id,
                        DBToolVersion.version == selected_version,
                    )
                )
                if version_result.scalar_one_or_none() is None:
                    return False
                lifecycle = await session.get(
                    DBToolVersionLifecycle, (tool.id, selected_version)
                )
                if lifecycle is None:
                    lifecycle = DBToolVersionLifecycle(
                        tool_id=tool.id,
                        version=selected_version,
                        state=state,
                        reason=reason,
                    )
                    session.add(lifecycle)
                else:
                    lifecycle.state = state
                    lifecycle.reason = reason
                    lifecycle.updated_at = datetime.now(timezone.utc)
            await session.commit()
            return True
