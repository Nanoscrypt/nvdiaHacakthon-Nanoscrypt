import pytest
import json
from pathlib import Path
from nanoscrypt.core.runtime import RuntimeManager
from nanoscrypt.models.tool import GeneratedTool, ToolManifest

@pytest.fixture
def runtime_manager(temp_workspace):
    return RuntimeManager(workspace_root=temp_workspace / "workspaces", timeout_seconds=5)

def test_runtime_setup_and_execution(runtime_manager):
    # A simple tool adding two parameters
    code = """def run(x: int, y: int) -> int:
    return x + y
"""
    manifest = ToolManifest(name="add_tool")
    tool = GeneratedTool(
        name="add_tool",
        code=code,
        requirements=[],  # standard lib, no external dependency
        manifest=manifest,
        tests="",
        readme=""
    )

    session_id = "test-session-1"
    
    # 1. Setup workspace
    workspace = runtime_manager.setup_workspace(session_id, tool)
    assert workspace.exists()
    assert (workspace / "tool.py").exists()

    # 2. Initialize Virtual Environment
    venv_dir = runtime_manager.create_virtual_env(workspace)
    assert venv_dir.exists()

    # 3. Execute Tool
    input_data = json.dumps({"x": 10, "y": 20})
    result = runtime_manager.execute_tool(session_id, input_data)

    assert result.return_code == 0
    assert result.timed_out is False
    
    # Parse stdout response
    output_obj = json.loads(result.stdout.strip())
    assert output_obj["status"] == "success"
    assert output_obj["output"] == 30

    # 4. Cleanup
    runtime_manager.cleanup_workspace(session_id)
    assert not workspace.exists()

def test_runtime_execution_timeout(runtime_manager):
    # Tool with an infinite loop
    code = """import time
def run(seconds: int) -> str:
    time.sleep(seconds)
    return "done"
"""
    tool = GeneratedTool(
        name="infinite_tool",
        code=code,
        requirements=[],
        manifest=ToolManifest(name="infinite_tool"),
        tests="",
        readme=""
    )

    session_id = "test-session-2"
    workspace = runtime_manager.setup_workspace(session_id, tool)
    runtime_manager.create_virtual_env(workspace)

    # Trigger with 10 seconds sleep, timeout limit is 2 seconds
    result = runtime_manager.execute_tool(session_id, json.dumps({"seconds": 10}), timeout=2)

    assert result.timed_out is True
    assert result.return_code == -9  # our custom timeout status code

    runtime_manager.cleanup_workspace(session_id)


@pytest.mark.asyncio
async def test_nebius_sandbox_backend_sends_tool_and_input_to_contree(
    runtime_manager, temp_workspace, monkeypatch
):
    from types import SimpleNamespace
    import sys

    submitted = {}

    class FakeImage:
        async def run(self, **kwargs):
            submitted.update(kwargs)
            return SimpleNamespace(
                stdout='{"status":"success","output":3}\n',
                stderr="",
                exit_code=0,
                result=SimpleNamespace(truncated=False),
            )

    class FakeImages:
        async def use(self, image, strict):
            submitted["image"] = image
            submitted["strict"] = strict
            return FakeImage()

    class FakeContree:
        def __init__(self):
            self.images = FakeImages()

    monkeypatch.setitem(
        sys.modules, "contree_sdk", SimpleNamespace(Contree=FakeContree)
    )
    monkeypatch.setenv("NEBIUS_API_KEY", "test-key")
    monkeypatch.setenv("NEBIUS_PROJECT_ID", "test-project")
    workspace = temp_workspace / "remote-workspace"
    workspace.mkdir()
    (workspace / "tool.py").write_text(
        "def run():\n    return 3\n", encoding="utf-8"
    )
    (workspace / "requirements.txt").write_text("", encoding="utf-8")

    result = await runtime_manager._execute_tool_nebius_sandbox(
        workspace=workspace, input_data="{}", timeout=5
    )

    assert result.return_code == 0
    assert '"output":3' in result.stdout
    assert submitted["image"] == "python:3.12-slim"
    assert submitted["disposable"] is True
    assert "/workspace/tool.py" in submitted["files"]
    assert "/workspace/wrapper.py" in submitted["files"]
    assert "python wrapper.py" in submitted["shell"]


@pytest.mark.asyncio
async def test_nebius_sandbox_live_output_callback(runtime_manager, temp_workspace, monkeypatch):
    from types import SimpleNamespace
    import sys
    import threading

    submitted = {}
    output_seen = threading.Event()

    class FakeImage:
        def __init__(self, client):
            self.client = client

        async def run(self, **kwargs):
            submitted.update(kwargs)
            await self.client._start_operation(None)
            return SimpleNamespace(
                stdout='{"status":"success","output":3}\n',
                stderr="",
                exit_code=0,
                result=SimpleNamespace(truncated=False),
            )

    class FakeImages:
        def __init__(self, client):
            self.client = client

        async def use(self, image, strict):
            return FakeImage(self.client)

    class FakeContree:
        def __init__(self):
            self.config = SimpleNamespace(auth=SimpleNamespace(base_url="https://sandbox.invalid"))
            self.images = FakeImages(self)

        async def _start_operation(self, request):
            return "operation-id"

    def fake_stream(**kwargs):
        kwargs["output_callback"]("stdout", "live chunk\n")
        output_seen.set()

    monkeypatch.setattr("nanoscrypt.core.runtime._stream_sandbox_operation", fake_stream)
    monkeypatch.setitem(sys.modules, "contree_sdk", SimpleNamespace(Contree=FakeContree))
    monkeypatch.setenv("NEBIUS_API_KEY", "test-key")
    monkeypatch.setenv("NEBIUS_PROJECT_ID", "test-project")
    workspace = temp_workspace / "remote-stream-workspace"
    workspace.mkdir()
    (workspace / "tool.py").write_text("def run():\n    return 3\n", encoding="utf-8")
    (workspace / "requirements.txt").write_text("", encoding="utf-8")
    chunks = []
    operation_ids = []

    result = await runtime_manager._execute_tool_nebius_sandbox(
        workspace=workspace, input_data="{}", timeout=5,
        output_callback=lambda channel, text: chunks.append((channel, text)),
        operation_callback=operation_ids.append,
    )

    assert result.return_code == 0
    assert output_seen.wait(1)
    assert chunks == [("stdout", "live chunk\n")]
    assert operation_ids == ["operation-id"]


def test_nebius_sse_stream_decodes_channels_and_completion(monkeypatch):
    import io
    import threading
    from nanoscrypt.core.runtime import _stream_sandbox_operation

    events = (
        b"id: 1\nevent: message\ndata: {\"type\":\"stdout\",\"data\":{\"encoding\":\"ascii\",\"value\":\"working\\n\"}}\n\n"
        b"id: 2\nevent: message\ndata: {\"type\":\"stderr\",\"data\":{\"encoding\":\"base64\",\"value\":\"ZXJyb3I=\"}}\n\n"
        b"id: 3\nevent: message\ndata: {\"type\":\"completion\"}\n\n"
    )
    requests = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    def fake_urlopen(request, timeout):
        requests.append(request)
        return Response(events)

    monkeypatch.setattr("nanoscrypt.core.runtime.urllib.request.urlopen", fake_urlopen)
    output = []
    _stream_sandbox_operation(
        operation_id="op-1",
        base_url="https://example.invalid/sandboxes",
        api_key="test-key",
        project_id="test-project",
        output_callback=lambda channel, text: output.append((channel, text)),
        stop_event=threading.Event(),
    )

    assert output == [("stdout", "working\n"), ("stderr", "error")]
    assert requests[0].full_url.endswith("/v1/operations/op-1/events?follow=1")
    assert requests[0].get_header("Project") == "test-project"
