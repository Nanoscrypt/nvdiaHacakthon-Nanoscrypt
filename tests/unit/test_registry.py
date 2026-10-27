import pytest

from nanoscrypt.core.registry import ToolRegistry
from nanoscrypt.models.database import DBToolLifecycle, DBToolVersionLifecycle
from nanoscrypt.models.tool import GeneratedTool, ToolManifest


@pytest.fixture
def database_url():
    # Use SQLite in-memory database for isolated unit testing
    return "sqlite+aiosqlite:///:memory:"

@pytest.mark.asyncio
async def test_tool_registry_lifecycle(database_url):
    registry = ToolRegistry(database_url)

    # 1. Initialize tables
    await registry.initialize_db()

    # 2. Register Tool v1
    manifest = ToolManifest(
        name="xml_converter",
        input_schema={"xml_str": "str", "purpose": "Convert XML payload to JSON dict"},
        output_schema={"json_dict": "dict"}
    )
    tool = GeneratedTool(
        name="xml_converter",
        code="def run(xml_str: str) -> dict: return {}",
        requirements=[],
        manifest=manifest,
        tests="",
        readme=""
    )

    db_tool = await registry.register(
        tool=tool,
        code_hash="sha-v1",
        prompt_used="create xml to json parser",
        change_reason="initial release"
    )

    assert db_tool.name == "xml_converter"
    assert db_tool.current_version == 1
    assert db_tool.usage_count == 0

    # 3. Retrieve tool
    retrieved = await registry.get("xml_converter")
    assert retrieved is not None
    assert retrieved.name == "xml_converter"

    # 4. Keyword Search Test
    results = await registry.search("converter")
    assert len(results) == 1
    assert results[0].name == "xml_converter"

    empty_results = await registry.search("unrelated_keyword")
    assert len(empty_results) == 0

    # 5. Update Stats (Successful run)
    await registry.update_stats(
        tool_name="xml_converter",
        success=True,
        runtime_ms=150,
        input_data={"xml_str": "<node></node>"},
        output_data={"res": "ok"}
    )

    updated = await registry.get("xml_converter")
    assert updated.usage_count == 1
    assert updated.success_rate == 1.0

    # 6. Update Stats (Failed run)
    await registry.update_stats(
        tool_name="xml_converter",
        success=False,
        runtime_ms=50,
        input_data={"xml_str": "invalid"},
        error="Parsing error"
    )

    updated = await registry.get("xml_converter")
    assert updated.usage_count == 2
    # 1 success out of 2 runs = 50% success rate
    assert updated.success_rate == 0.5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "old_version_contribution,expected_old_state",
    [("helpful", "shared"), ("harmful", "quarantined")],
)
async def test_late_old_version_evidence_does_not_change_new_version(
    database_url, old_version_contribution, expected_old_state
):
    registry = ToolRegistry(database_url)
    await registry.initialize_db()

    manifest = ToolManifest(
        name="version_scoped_tool",
        input_schema={"value": "int"},
        output_schema={"result": "int"},
    )
    version_one = GeneratedTool(
        name="version_scoped_tool",
        code="def run(value: int) -> int: return value + 1",
        requirements=[],
        manifest=manifest,
        tests="",
        readme="Version-scoping regression fixture",
    )
    await registry.register(
        version_one, "hash-v1", "create v1", change_reason="initial version"
    )

    if old_version_contribution == "helpful":
        task_keys = [
            "family-a",
            "family-a",
            "family-b",
            "family-b",
            "family-c",
            "family-c",
        ]
    else:
        task_keys = [
            "family-a",
            "family-a",
            "family-b",
            "family-b",
        ]

    execution_ids = []
    for _ in task_keys:
        execution_id = await registry.update_stats(
            tool_name="version_scoped_tool",
            success=True,
            runtime_ms=1,
            input_data={"value": 1},
            output_data={"result": 2},
        )
        execution_ids.append(execution_id)

    evidence_count = 5 if old_version_contribution == "helpful" else 3
    for index in range(evidence_count):
        result = await registry.record_outcome(
            execution_ids[index],
            task_keys[index],
            old_version_contribution,
            "Controlled version-isolation test evidence",
        )
    assert result["state"] == expected_old_state
    version_one_state = await registry.get_lifecycle(
        "version_scoped_tool", version=1
    )
    assert version_one_state["state"] == expected_old_state

    version_two = version_one.model_copy(
        update={"code": "def run(value: int) -> int: return value + 2"}
    )
    await registry.register(
        version_two, "hash-v2", "create v2", change_reason="changed implementation"
    )
    assert (await registry.get_lifecycle("version_scoped_tool"))["state"] == "candidate"
    assert await registry.search("version_scoped", shared_only=True) == []

    # This feedback arrives after v2 is current but belongs to a v1 execution.
    late_result = await registry.record_outcome(
        execution_ids[-1],
        task_keys[-1],
        old_version_contribution,
        "Late evidence for the previous version",
    )

    assert late_result["version"] == 1
    assert late_result["state"] == expected_old_state
    assert (await registry.get_lifecycle("version_scoped_tool"))["state"] == "candidate"
    version_one_state = await registry.get_lifecycle(
        "version_scoped_tool", version=1
    )
    assert version_one_state["state"] == expected_old_state


@pytest.mark.asyncio
async def test_legacy_shared_state_without_version_evidence_migrates_conservatively(
    database_url,
):
    registry = ToolRegistry(database_url)
    await registry.initialize_db()
    manifest = ToolManifest(
        name="legacy_tool",
        input_schema={"value": "int"},
        output_schema={"result": "int"},
    )
    tool = GeneratedTool(
        name="legacy_tool",
        code="def run(value: int) -> int: return value",
        requirements=[],
        manifest=manifest,
        tests="",
        readme="Migration fixture",
    )
    db_tool = await registry.register(tool, "legacy-hash", "legacy prompt")

    async with registry.session_factory() as session, session.begin():
        await session.delete(
            await session.get(DBToolVersionLifecycle, (db_tool.id, 1))
        )
        session.add(
            DBToolLifecycle(
                tool_id=db_tool.id,
                state="shared",
                reason="Legacy global approval",
            )
        )

    await registry._migrate_legacy_lifecycle_states()

    lifecycle = await registry.get_lifecycle("legacy_tool")
    assert lifecycle["state"] == "candidate"
    assert await registry.search("legacy", shared_only=True) == []
