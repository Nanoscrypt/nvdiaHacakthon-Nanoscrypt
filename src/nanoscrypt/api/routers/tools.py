from fastapi import APIRouter, Depends, HTTPException, Query

from nanoscrypt.api.dependencies import get_registry
from nanoscrypt.api.schemas import ToolLifecycleAction, ToolOutcomeSubmit, ToolResponse
from nanoscrypt.core.registry import ToolRegistry

router = APIRouter(prefix="/tools", tags=["tools"])


@router.post("/outcomes")
async def submit_tool_outcome(
    payload: ToolOutcomeSubmit, registry: ToolRegistry = Depends(get_registry)
):
    """Attach caller-reviewed task contribution evidence to one tool execution."""
    try:
        result = await registry.record_outcome(
            execution_id=payload.execution_id,
            task_key=payload.task_key,
            contribution=payload.contribution,
            evidence=payload.evidence,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Tool execution not found")
    return result


@router.get("/{name}/lifecycle")
async def get_tool_lifecycle(
    name: str,
    version: int | None = Query(default=None, ge=1),
    registry: ToolRegistry = Depends(get_registry),
):
    result = await registry.get_lifecycle(name, version=version)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Tool '{name}' not found")
    return result


@router.put("/{name}/lifecycle")
async def update_tool_lifecycle(
    name: str,
    payload: ToolLifecycleAction,
    registry: ToolRegistry = Depends(get_registry),
):
    updated = await registry.set_lifecycle_state(
        name, payload.state, payload.reason, version=payload.version
    )
    if not updated:
        raise HTTPException(status_code=404, detail=f"Tool '{name}' not found")
    return await registry.get_lifecycle(name, version=payload.version)


@router.get("", response_model=list[ToolResponse])
async def list_tools(query: str = "", registry: ToolRegistry = Depends(get_registry)):
    """
    List or search available tools in the registry.
    Optional query parameter filters tools by name or description.
    """
    tools = await registry.search(query)
    response = []
    for t in tools:
        lifecycle = await registry.get_lifecycle(t.name)
        response.append(
            ToolResponse(
                name=t.name,
                purpose=t.purpose,
                language=t.language,
                current_version=t.current_version,
                success_rate=t.success_rate,
                usage_count=t.usage_count,
                status=t.status,
                created_at=t.created_at,
                lifecycle_state=lifecycle["state"] if lifecycle else "shared",
            )
        )
    return response


@router.get("/{name}", response_model=ToolResponse)
async def get_tool(name: str, registry: ToolRegistry = Depends(get_registry)):
    """
    Get a specific tool's details by its name.
    Returns a 404 error if the tool is not found.
    """
    t = await registry.get(name)
    if not t:
        raise HTTPException(
            status_code=404, detail=f"Tool '{name}' not found or inactive in registry."
        )

    return ToolResponse(
        name=t.name,
        purpose=t.purpose,
        language=t.language,
        current_version=t.current_version,
        success_rate=t.success_rate,
        usage_count=t.usage_count,
        status=t.status,
        created_at=t.created_at,
        lifecycle_state=(await registry.get_lifecycle(t.name) or {}).get(
            "state", "shared"
        ),
    )
