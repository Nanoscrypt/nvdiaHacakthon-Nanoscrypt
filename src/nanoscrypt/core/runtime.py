import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import venv
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import structlog

from nanoscrypt.config.settings import settings
from nanoscrypt.models.tool import GeneratedTool
from nanoscrypt.utils.filesystem import ensure_directory, remove_directory

logger = structlog.get_logger()


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    return_code: int
    runtime_ms: int
    timed_out: bool
    workspace_path: Path
    cancelled: bool = False


_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def _decode_sandbox_output(data: dict) -> str:
    value = data.get("value", "")
    if data.get("encoding", "ascii") == "base64":
        value = base64.b64decode(value)
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    text = _ANSI_ESCAPE.sub("", text)
    return "".join(ch for ch in text if ch in "\n\r\t" or ord(ch) >= 32)


def _stream_sandbox_operation(
    *,
    operation_id: str,
    base_url: str,
    api_key: str,
    project_id: str,
    output_callback: Callable[[str, str], None],
    stop_event: threading.Event,
) -> None:
    """Follow Nebius SSE output, reconnecting from the last fully handled event."""
    cursor: str | None = None
    completed = False
    endpoint = f"{base_url.rstrip('/')}/v1/operations/{operation_id}/events?follow=1"
    while not stop_event.is_set() and not completed:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Project": project_id,
            "Accept": "text/event-stream",
        }
        if cursor is not None:
            headers["Last-Event-Id"] = cursor
        request = urllib.request.Request(endpoint, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                frame: list[str] = []
                while not stop_event.is_set():
                    raw = response.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line:
                        frame.append(line)
                        continue
                    if not frame:
                        continue
                    fields: dict[str, list[str]] = {}
                    for item in frame:
                        if item.startswith(":"):
                            continue
                        key, separator, value = item.partition(":")
                        if separator:
                            field_value = value[1:] if value.startswith(" ") else value
                            fields.setdefault(key, []).append(field_value)
                    frame = []
                    if "id" not in fields or "data" not in fields:
                        continue
                    try:
                        payload = json.loads("\n".join(fields["data"]))
                        event_type = payload.get("type")
                        if event_type in {"stdout", "stderr"}:
                            text = _decode_sandbox_output(payload.get("data") or {})
                            if text:
                                output_callback(event_type, text)
                        completed = event_type == "completion"
                        cursor = fields["id"][0]
                    except Exception:
                        # A bad frame must not terminate tool execution or advance the cursor.
                        logger.warning(
                            "nebius_sandbox_event_decode_failed",
                            operation_id=operation_id,
                        )
            if completed:
                return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.debug(
                "nebius_sandbox_event_stream_reconnecting",
                operation_id=operation_id,
                error=str(exc),
            )
            stop_event.wait(0.25)
        except Exception as exc:
            logger.warning("nebius_sandbox_event_stream_failed", operation_id=operation_id, error=str(exc))
            return


class RuntimeManager:
    """Manages isolated workspaces and shared virtual environments to optimize tool execution latency."""

    def __init__(self, workspace_root: Path | str, timeout_seconds: int = 30):
        self.workspace_root = Path(workspace_root)
        self.timeout_seconds = timeout_seconds
        self.venv_cache_dir = Path(settings.runtime.venv_cache_dir)
        ensure_directory(self.workspace_root)
        ensure_directory(self.venv_cache_dir)

    def get_session_workspace(self, session_id: str) -> Path:
        """Returns the isolated path for a specific session's workspace."""
        root = self.workspace_root.resolve()
        workspace = (root / session_id).resolve()
        try:
            workspace.relative_to(root)
        except ValueError as exc:
            raise ValueError("Session workspace escapes the configured workspace root.") from exc
        if workspace.parent != root:
            raise ValueError("Session workspace must be a direct child of the workspace root.")
        return workspace

    def get_dependencies_hash(self, requirements: list[str]) -> str:
        """Generates a stable hash for a given list of dependencies."""
        cleaned = sorted([r.strip().lower() for r in requirements if r.strip()])
        if not cleaned:
            return "base_env"
        h = hashlib.md5()
        h.update("\n".join(cleaned).encode("utf-8"))
        return h.hexdigest()

    def get_venv_directory(self, requirements: list[str]) -> Path:
        """Returns the cached venv directory path for the given requirements."""
        reqs_hash = self.get_dependencies_hash(requirements)
        return self.venv_cache_dir / reqs_hash

    def setup_workspace(self, session_id: str, tool: GeneratedTool) -> Path:
        """Creates a workspace and writes the tool files (tool.py, requirements.txt)."""
        workspace = self.get_session_workspace(session_id)
        ensure_directory(workspace)

        # Write tool.py
        tool_file = workspace / "tool.py"
        tool_file.write_text(tool.code, encoding="utf-8")

        # Write requirements.txt
        req_file = workspace / "requirements.txt"
        req_content = "\n".join(tool.requirements)
        req_file.write_text(req_content, encoding="utf-8")

        return workspace

    def setup_application_workspace(self, session_id: str, app_manifest) -> Path:
        """Scaffolds a multi-file project workspace preserving directory structure."""
        workspace = self.get_session_workspace(session_id)
        ensure_directory(workspace)

        # Write all files in the manifest dictionary
        for rel_path, content in app_manifest.files.items():
            dest = (workspace / rel_path).resolve()
            try:
                dest.relative_to(workspace.resolve())
            except ValueError as exc:
                raise ValueError(
                    f"Application file path escapes its workspace: {rel_path}"
                ) from exc
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

        # Ensure requirements.txt or package.json exists if dependencies were declared
        if getattr(app_manifest, "language", "python") == "python":
            req_file = workspace / "requirements.txt"
            if not req_file.exists() and app_manifest.dependencies:
                req_file.write_text("\n".join(app_manifest.dependencies), encoding="utf-8")
        elif getattr(app_manifest, "language", "") in ("javascript", "typescript", "node"):
            pkg_file = workspace / "package.json"
            if not pkg_file.exists():
                import json
                deps_dict = {dep: "latest" for dep in app_manifest.dependencies}
                pkg_data = {
                    "name": app_manifest.name or "nanoscrypt-app",
                    "version": "1.0.0",
                    "main": app_manifest.entry_point or "server.js",
                    "dependencies": deps_dict
                }
                pkg_file.write_text(json.dumps(pkg_data, indent=2), encoding="utf-8")

        return workspace

    def create_virtual_env(self, venv_dir: Path) -> Path:
        """Creates a virtual environment in the specified directory if it doesn't exist."""
        # If the path looks like a workspace directory (i.e. contains tool.py or requirements.txt),
        # create venv in .venv subdirectory to maintain backward compatibility
        target_dir = venv_dir
        if (venv_dir / "tool.py").exists() or (venv_dir / "requirements.txt").exists():
            target_dir = venv_dir / ".venv"

        if not target_dir.exists():
            logger.info("runtime_creating_venv_cache", path=str(target_dir))
            venv.create(target_dir, with_pip=True)
        return target_dir

    def install_dependencies(self, venv_dir: Path, requirements: list[str]) -> None:
        """Installs the list of dependencies into the cached virtual environment if needed."""
        cleaned = [r.strip() for r in requirements if r.strip()]
        if not cleaned:
            return

        # Determine python/pip executable based on OS
        if sys.platform == "win32":
            pip_executable = venv_dir / "Scripts" / "pip.exe"
            python_executable = venv_dir / "Scripts" / "python.exe"
        else:
            pip_executable = venv_dir / "bin" / "pip"
            python_executable = venv_dir / "bin" / "python"

        if not pip_executable.exists():
            raise FileNotFoundError(
                f"Virtual environment pip not found at {pip_executable}"
            )

        # Check if we have already installed these requirements by checking a sentinel file
        reqs_hash = self.get_dependencies_hash(requirements)
        sentinel_file = venv_dir / ".dependencies_installed"
        if sentinel_file.exists():
            try:
                if sentinel_file.read_text(encoding="utf-8").strip() == reqs_hash:
                    return
            except Exception:
                pass

        logger.info("runtime_installing_cached_dependencies", count=len(cleaned))

        # Write requirements temporarily inside the venv dir to run pip install
        temp_reqs = venv_dir / "temp_requirements.txt"
        temp_reqs.write_text("\n".join(cleaned), encoding="utf-8")

        try:
            # Upgrade pip first to avoid package installation bugs
            subprocess.run(
                [str(python_executable), "-m", "pip", "install", "--upgrade", "pip"],
                capture_output=True,
                check=True,
            )
            # Run pip install
            res = subprocess.run(
                [str(pip_executable), "install", "-r", str(temp_reqs)],
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                logger.warning(
                    "pip_install_failed_continuing_offline",
                    stderr=res.stderr,
                    stdout=res.stdout,
                )
                # If network fails or pip install fails, check if standard modules exist or continue gracefully
                print(f"\n[Warning] Pip install offline/failed for {cleaned}. Attempting tool execution with available environment packages...")
            else:
                # Write sentinel file with the requirements hash
                sentinel_file.write_text(reqs_hash, encoding="utf-8")
        finally:
            if temp_reqs.exists():
                temp_reqs.unlink()

    def execute_tool(
        self,
        session_id: str,
        input_data: str,
        requirements: list[str] | None = None,
        timeout: int | None = None,
        cancellation_event: threading.Event | None = None,
        sandbox_output_callback: Callable[[str, str], None] | None = None,
        sandbox_operation_callback: Callable[[str], None] | None = None,
    ) -> ExecutionResult:
        """Runs the tool wrapper script using the shared/cached virtual environment python interpreter."""
        workspace = self.get_session_workspace(session_id)

        if settings.runtime.execution_backend == "nebius_sandbox":
            return asyncio.run(
                self._execute_tool_nebius_sandbox(
                    workspace=workspace,
                    input_data=input_data,
                    timeout=timeout or self.timeout_seconds,
                    output_callback=sandbox_output_callback,
                    operation_callback=sandbox_operation_callback,
                )
            )

        capsem_path = None
        if settings.runtime.capsem_enabled:
            import shutil

            capsem_path = shutil.which("capsem")
            if capsem_path is None:
                return ExecutionResult(
                    stdout="",
                    stderr="CapSem is enabled but unavailable; tool execution was blocked.",
                    return_code=126,
                    runtime_ms=0,
                    timed_out=False,
                    workspace_path=workspace,
                )

        # Check if venv is disabled in settings
        if not getattr(settings.runtime, "use_venv", True):
            python_executable = Path(sys.executable)
        else:
            # Determine venv dir based on requirements
            reqs = requirements or []
            venv_dir = self.get_venv_directory(reqs)
            self.create_virtual_env(venv_dir)
            self.install_dependencies(venv_dir, reqs)

            # Determine python executable based on OS
            if sys.platform == "win32":
                python_executable = venv_dir / "Scripts" / "python.exe"
            else:
                python_executable = venv_dir / "bin" / "python"

        if not python_executable.exists():
            raise FileNotFoundError(
                f"Python executable not found at {python_executable}"
            )

        wrapper_code = f"""
import json
import sys
import traceback
import inspect
import tool

try:
    input_str = {input_data!r}
    # Try parsing input as JSON, then try ast.literal_eval, then fallback to raw string
    args = None
    try:
        args = json.loads(input_str)
    except Exception:
        try:
            import ast
            args = ast.literal_eval(input_str)
        except Exception:
            args = input_str

    if isinstance(args, dict):
        # Introspect run() signature and fill missing params with defaults
        try:
            sig = inspect.signature(tool.run)
            has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            if not has_var_keyword:
                filtered_args = dict()
                for key_param, val_param in args.items():
                    if key_param in sig.parameters:
                        filtered_args[key_param] = val_param
                args = filtered_args
            for param_name, param in sig.parameters.items():
                if param_name not in args and param.default is not inspect.Parameter.empty:
                    args[param_name] = param.default
        except Exception:
            pass
        result = tool.run(**args)
    elif args is None or args == '' or args == '{{}}':
        # No args provided - call run() with no arguments (relies on defaults)
        result = tool.run()
    else:
        result = tool.run(args)
        
    print(json.dumps({{"status": "success", "output": result}}))
except Exception as e:
    tb_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    sys.stderr.write(tb_str + "\\n")
    print(json.dumps({{"status": "error", "message": str(e), "traceback": tb_str}}), file=sys.stderr)
    sys.exit(1)
"""
        wrapper_file = workspace / "wrapper.py"
        wrapper_file.write_text(wrapper_code, encoding="utf-8")

        limit = timeout or self.timeout_seconds
        start_time = time.perf_counter()

        timed_out = False
        stdout = ""
        stderr = ""
        return_code = -1

        # Determine execution command list with absolute wrapper path
        wrapper_path = (workspace / "wrapper.py").resolve()
        cmd = [str(python_executable.resolve()), str(wrapper_path)]
        if capsem_path:
            cmd = [capsem_path] + cmd
            logger.info("runtime_executing_via_capsem_sandbox", command=cmd)

        project_root = Path(".").resolve()
        env = dict(os.environ)
        env["PROJECT_ROOT"] = str(project_root)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(workspace.resolve()), env.get("PYTHONPATH", "")]
        )

        process: subprocess.Popen | None = None
        try:
            process_options: dict[str, int | bool] = {}
            if os.name == "nt":
                process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                process_options["start_new_session"] = True
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(workspace),
                env=env,
                **process_options,
            )
            deadline = time.monotonic() + limit
            while True:
                if cancellation_event is not None and cancellation_event.is_set():
                    self._stop_process(process)
                    stdout, stderr = process.communicate()
                    return_code = (
                        process.returncode if process.returncode is not None else -15
                    )
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    self._stop_process(process)
                    stdout, stderr = process.communicate()
                    return_code = (
                        process.returncode if process.returncode is not None else -9
                    )
                    stderr = stderr or "Execution timed out"
                    break
                try:
                    stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                    return_code = process.returncode
                    break
                except subprocess.TimeoutExpired:
                    continue
        except Exception as e:
            stderr = str(e)
            stdout = ""
            return_code = -1
        finally:
            if process is not None and process.poll() is None:
                self._stop_process(process)

        runtime_ms = int((time.perf_counter() - start_time) * 1000)

        # Cleanup wrapper file
        if wrapper_file.exists():
            try:
                wrapper_file.unlink()
            except Exception:
                pass

        return ExecutionResult(
            stdout=stdout,
            stderr=stderr,
            return_code=return_code,
            runtime_ms=runtime_ms,
            timed_out=timed_out,
            workspace_path=workspace,
            cancelled=bool(cancellation_event and cancellation_event.is_set()),
        )

    async def _execute_tool_nebius_sandbox(
        self,
        workspace: Path,
        input_data: str,
        timeout: int,
        output_callback: Callable[[str, str], None] | None = None,
        operation_callback: Callable[[str], None] | None = None,
    ) -> ExecutionResult:
        """Run a generated tool in Nebius Token Factory Sandboxes via ConTree."""
        started = time.perf_counter()
        try:
            from contree_sdk import Contree
        except ImportError:
            return ExecutionResult(
                stdout="",
                stderr=(
                    "Nebius sandbox backend requires the optional dependency. "
                    "Install Nanoscrypt with the [nebius] extra."
                ),
                return_code=126,
                runtime_ms=0,
                timed_out=False,
                workspace_path=workspace,
            )

        api_key = os.getenv("NEBIUS_API_KEY") or settings.nebius_api_key
        project_id = os.getenv("NEBIUS_PROJECT_ID") or settings.nebius_project_id
        if not api_key or not project_id:
            return ExecutionResult(
                stdout="",
                stderr="Set NEBIUS_API_KEY and NEBIUS_PROJECT_ID for sandbox execution.",
                return_code=126,
                runtime_ms=0,
                timed_out=False,
                workspace_path=workspace,
            )
        # ConTree reads these variables from the process environment. The
        # pydantic settings loader also supports values from the local .env.
        os.environ.setdefault("NEBIUS_API_KEY", api_key)
        os.environ.setdefault("NEBIUS_PROJECT_ID", project_id)

        wrapper_lines = [
            "import json",
            "import sys",
            "import traceback",
            "import inspect",
            "import tool",
            "",
            "try:",
            "    input_str = " + repr(input_data),
            "    try:",
            "        args = json.loads(input_str)",
            "    except Exception:",
            "        try:",
            "            import ast",
            "            args = ast.literal_eval(input_str)",
            "        except Exception:",
            "            args = input_str",
            "    if isinstance(args, dict):",
            "        try:",
            "            sig = inspect.signature(tool.run)",
            "            has_var_keyword = any(",
            "                p.kind == inspect.Parameter.VAR_KEYWORD",
            "                for p in sig.parameters.values()",
            "            )",
            "            if not has_var_keyword:",
            "                args = {k: v for k, v in args.items() if k in sig.parameters}",
            "            for name, param in sig.parameters.items():",
            "                if name not in args and param.default is not inspect.Parameter.empty:",
            "                    args[name] = param.default",
            "        except Exception:",
            "            pass",
            "        result = tool.run(**args)",
            "    elif args is None or args == '' or args == '{}':",
            "        result = tool.run()",
            "    else:",
            "        result = tool.run(args)",
            "    print(json.dumps({'status': 'success', 'output': result}))",
            "except Exception as exc:",
            "    trace = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))",
            "    sys.stderr.write(trace + chr(10))",
            "    print(json.dumps({'status': 'error', 'message': str(exc), 'traceback': trace}), file=sys.stderr)",
            "    sys.exit(1)",
        ]
        wrapper_code = "\n".join(wrapper_lines) + "\n"
        requirements_file = workspace / "requirements.txt"
        requirements = (
            requirements_file.read_text(encoding="utf-8")
            if requirements_file.exists()
            else ""
        )
        files = {
            "/workspace/tool.py": (workspace / "tool.py").read_bytes(),
            "/workspace/wrapper.py": wrapper_code.encode("utf-8"),
            "/workspace/requirements.txt": requirements.encode("utf-8"),
        }
        shell = (
            "cd /workspace && "
            "if [ -s requirements.txt ]; then "
            "python -m pip install --disable-pip-version-check -r requirements.txt "
            "|| exit $?; fi && python wrapper.py"
        )

        try:
            client = Contree()
            image = await client.images.use(
                settings.runtime.nebius_sandbox_image, strict=True
            )
            stop_stream = threading.Event()
            stream_threads: list[threading.Thread] = []
            if output_callback is not None or operation_callback is not None:
                original_start_operation = client._start_operation

                async def start_operation_and_stream(request):
                    operation_id = await original_start_operation(request)
                    if operation_callback is not None:
                        operation_callback(str(operation_id))
                    if output_callback is not None:
                        stream = threading.Thread(
                            target=_stream_sandbox_operation,
                            kwargs={
                                "operation_id": str(operation_id),
                                "base_url": client.config.auth.base_url,
                                "api_key": api_key,
                                "project_id": project_id,
                                "output_callback": output_callback,
                                "stop_event": stop_stream,
                            },
                            daemon=True,
                            name="nebius-sandbox-events",
                        )
                        stream_threads.append(stream)
                        stream.start()
                    return operation_id

                # The SDK does not expose a streaming callback; intercept its
                # operation UUID after submission and observe that same run.
                client._start_operation = start_operation_and_stream
            try:
                result = await image.run(
                    shell=shell,
                    files=files,
                    disposable=True,
                    timeout=timeout,
                    truncate_output_at=1_048_576,
                )
            finally:
                stop_stream.set()
                for stream in stream_threads:
                    stream.join(timeout=0.25)
            stderr = result.stderr or ""
            if getattr(getattr(result, "result", None), "truncated", False):
                stderr = (stderr + "\nSandbox output was truncated.").strip()
            return ExecutionResult(
                stdout=result.stdout or "",
                stderr=stderr,
                return_code=int(result.exit_code),
                runtime_ms=int((time.perf_counter() - started) * 1000),
                timed_out=False,
                workspace_path=workspace,
            )
        except Exception as exc:
            return ExecutionResult(
                stdout="",
                stderr=f"Nebius sandbox execution failed: {exc}",
                return_code=1,
                runtime_ms=int((time.perf_counter() - started) * 1000),
                timed_out=False,
                workspace_path=workspace,
            )

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        """Stop the process tree where supported, falling back to the child process."""
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=3,
                    check=False,
                )
            else:
                import signal as process_signal

                os.killpg(process.pid, process_signal.SIGTERM)
        except Exception:
            process.kill()

    def execute_application(
        self,
        session_id: str,
        app_manifest,
        timeout: int | None = None,
    ) -> ExecutionResult:
        """Executes a multi-file polyglot application or starts it as a supervised daemon."""
        workspace = self.setup_application_workspace(session_id, app_manifest)
        language = getattr(app_manifest, "language", "python").lower()

        # Handle Python
        if language == "python":
            venv_dir = self.get_venv_directory(app_manifest.dependencies)
            self.create_virtual_env(venv_dir)
            self.install_dependencies(venv_dir, app_manifest.dependencies)

            if sys.platform == "win32":
                python_executable = venv_dir / "Scripts" / "python.exe"
            else:
                python_executable = venv_dir / "bin" / "python"

            cmd = [str(python_executable.resolve()), app_manifest.entry_point]

        # Handle Node.js / JavaScript / TypeScript
        elif language in ("javascript", "typescript", "node"):
            # If npm dependencies exist, run npm install
            if app_manifest.dependencies:
                subprocess.run(["npm", "install"], cwd=str(workspace), capture_output=True)
            cmd = ["node", app_manifest.entry_point]

        # Handle Go
        elif language == "go":
            cmd = ["go", "run", app_manifest.entry_point]

        # Handle Rust
        elif language == "rust":
            cmd = ["cargo", "run"]

        else:
            raise ValueError(f"Unsupported language driver: {language}")

        # If daemon / long-running web application
        if getattr(app_manifest, "is_daemon", False):
            import socket
            import time

            logger.info("runtime_starting_daemon_application", name=app_manifest.name, port=app_manifest.port)
            proc = subprocess.Popen(
                cmd,
                cwd=str(workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            # Wait briefly to confirm port is listening
            port = app_manifest.port or 8080
            time.sleep(1.5)

            # Check if process died immediately
            if proc.poll() is not None:
                stdout, stderr = proc.communicate()
                return ExecutionResult(
                    stdout=stdout,
                    stderr=stderr,
                    return_code=proc.returncode,
                    runtime_ms=1500,
                    timed_out=False,
                    workspace_path=workspace,
                )

            return ExecutionResult(
                stdout=f"Daemon application '{app_manifest.name}' successfully launched on port {port}. PID: {proc.pid}",
                stderr="",
                return_code=0,
                runtime_ms=1500,
                timed_out=False,
                workspace_path=workspace,
            )

        # Standard execution for non-daemon applications
        limit = timeout or self.timeout_seconds
        start_time = time.perf_counter()
        res = subprocess.run(cmd, cwd=str(workspace), capture_output=True, text=True, timeout=limit)
        runtime_ms = int((time.perf_counter() - start_time) * 1000)

        return ExecutionResult(
            stdout=res.stdout,
            stderr=res.stderr,
            return_code=res.returncode,
            runtime_ms=runtime_ms,
            timed_out=False,
            workspace_path=workspace,
        )

    def cleanup_workspace(self, session_id: str) -> None:
        """Removes the entire session workspace directory."""
        workspace = self.get_session_workspace(session_id)
        if workspace == Path(self.workspace_root).resolve():
            return
        if workspace.exists():
            remove_directory(workspace)

