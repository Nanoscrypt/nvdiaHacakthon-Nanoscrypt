import sys
import uuid
import typer
from rich.align import Align
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from nanoscrypt.api.dependencies import get_orchestrator
from nanoscrypt.core.approval import ApprovalRequest, ApprovalType
from nanoscrypt.models.agent import Agent, AgentRole
from nanoscrypt.models.permissions import AgentPermissions, PermissionLevel
from nanoscrypt.models.session import Session
from nanoscrypt.config.settings import settings

import os
from pathlib import Path
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.styles import Style

console = Console()


class FileCompleter(Completer):
    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root
        self.slash_commands = [
            "/help",
            "/setup",
            "/config",
            "/cost",
            "/model",
            "/profile",
            "/memory",
            "/clear",
            "/history",
            "/exit"
        ]
        self.skip_dirs = {
            ".git",
            ".venv",
            "venv",
            "__pycache__",
            "node_modules",
            "workspaces",
            "generated_tools",
            "venv_cache",
            "dist",
            "build",
            ".pytest_cache",
            ".mypy_cache",
            "scratch",
        }

    def _get_files(self) -> list[str]:
        files = []
        for root, dirs, filenames in os.walk(self.workspace_root):
            dirs[:] = [d for d in dirs if d not in self.skip_dirs and not d.startswith(".")]
            for f in filenames:
                if not f.startswith("."):
                    rel = os.path.relpath(os.path.join(root, f), self.workspace_root)
                    files.append(rel.replace("\\", "/"))
        return files

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text:
            return

        # 1. Autocomplete slash commands
        if text.startswith("/"):
            word_to_match = text.lower()
            if word_to_match.startswith("//"):
                double_slash_cmds = ["//TODO", "//inject", "//confluence"]
                for cmd in double_slash_cmds:
                    if cmd.lower().startswith(word_to_match):
                        yield Completion(
                            cmd,
                            start_position=-len(text),
                            display=cmd,
                            display_meta="Special Command",
                        )
            else:
                for cmd in self.slash_commands:
                    if cmd.startswith(word_to_match):
                        yield Completion(
                            cmd,
                            start_position=-len(text),
                            display=cmd,
                            display_meta="Command",
                        )
            return

        # 2. Autocomplete files using '@'
        last_at_idx = text.rfind("@")
        if last_at_idx == -1:
            return

        after_at = text[last_at_idx + 1 :]
        if " " in after_at:
            return

        word_to_match = after_at.lower()
        all_files = self._get_files()

        scored_matches = []
        for f in all_files:
            f_lower = f.lower()
            base_name = os.path.basename(f)
            base_lower = base_name.lower()

            if not word_to_match:
                # If user typed just '@', show all workspace files
                scored_matches.append((0, f, base_name))
            elif f_lower == word_to_match or base_lower == word_to_match:
                scored_matches.append((1, f, base_name))
            elif f_lower.startswith(word_to_match) or base_lower.startswith(word_to_match):
                scored_matches.append((2, f, base_name))
            elif word_to_match in base_lower:
                scored_matches.append((3, f, base_name))
            elif word_to_match in f_lower:
                scored_matches.append((4, f, base_name))

        # Sort by relevance rank then filename
        scored_matches.sort(key=lambda item: (item[0], item[1]))

        for score, rel_path, base_name in scored_matches:
            parent = Path(rel_path).parent.as_posix()
            display_meta = "project root" if parent == "." else parent
            yield Completion(
                rel_path,
                start_position=-len(word_to_match),
                display=rel_path,
                display_meta=display_meta,
            )



class Meter:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    def get_stats(self) -> dict:
        input_tokens = 0
        output_tokens = 0
        cost = 0.0

        llms = []
        if hasattr(self.orchestrator, "planner") and hasattr(self.orchestrator.planner, "llm"):
            llms.append(self.orchestrator.planner.llm)
        if hasattr(self.orchestrator, "generator") and hasattr(self.orchestrator.generator, "llm"):
            llms.append(self.orchestrator.generator.llm)
        if hasattr(self.orchestrator, "repair_loop") and self.orchestrator.repair_loop and hasattr(self.orchestrator.repair_loop, "llm"):
            llms.append(self.orchestrator.repair_loop.llm)

        for llm in llms:
            input_tokens += getattr(llm, "total_input_tokens", 0) or 0
            output_tokens += getattr(llm, "total_output_tokens", 0) or 0
            cost += getattr(llm, "total_cost", 0.0) or 0.0

        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost": cost,
        }

    def status(self) -> str:
        stats = self.get_stats()
        saved_info = ""
        try:
            from nanoscrypt.core.compressor import ContextCompressor
            comp = ContextCompressor()
            if comp.total_saved_tokens > 0:
                saved_info = f"  ·  [green]Headroom saved ~{comp.total_saved_tokens} tokens[/green]"
        except Exception:
            pass

        return (
            f"${stats['cost']:.5f}  ·  "
            f"in {stats['input_tokens']}  out {stats['output_tokens']}{saved_info}"
        )


def run_cmd(
    prompt: str = typer.Argument(None, help="The task prompt for the orchestrator. Omit to run in interactive REPL mode."),
    session_id: str = typer.Option(None, help="Optional session ID context"),
    agent_name: str = typer.Option(
        None, help="Define a custom Agent name to execute this task"
    ),
    agent_role: str = typer.Option(
        None,
        help="Define Agent Role (planner, coder, researcher, executor, reviewer, custom)",
    ),
    agent_goal: str = typer.Option(None, help="Goal description for the active Agent"),
    allow_web: bool = typer.Option(
        False, help="Explicitly enable web access for this run"
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show internal diagnostic logs"
    ),
):
    """Executes a task prompt or enters an interactive REPL developer session using the Nanoscrypt orchestrator."""
    sess_id = session_id or f"cli_{uuid.uuid4().hex[:8]}"

    async def async_run():
        from nanoscrypt.cli.setup import ensure_user_configured
        from nanoscrypt.logging import setup_logging

        # Keep internal planner/context logs out of the user-facing terminal by
        # default. They can include full prompts and are available with --verbose.
        setup_logging("INFO" if verbose else "WARNING")

        # Prompt user to configure API key & provider on first start if not configured
        if ensure_user_configured(interactive=True):
            from nanoscrypt.config.settings import reload_settings
            reload_settings()

        # Initialize dependencies
        orchestrator = await get_orchestrator()
        from nanoscrypt.core.session_store import SessionStore

        session_store = SessionStore(settings.runtime.workspace_root)
        session = session_store.load(sess_id) or Session(
            id=sess_id,
            workspace_path=str(Path(settings.runtime.workspace_root) / sess_id),
        )
        session_store.save(session)
        meter = Meter(orchestrator)

        # Build custom agent if args specified
        active_agent = None
        if agent_name or agent_role or agent_goal:
            role_enum = AgentRole.PLANNER
            if agent_role:
                try:
                    role_enum = AgentRole(agent_role.lower())
                except ValueError:
                    role_enum = AgentRole.CUSTOM

            perms = AgentPermissions(
                file_system=PermissionLevel.WRITE
                if role_enum == AgentRole.CODER
                else PermissionLevel.READ,
                network=PermissionLevel.EXECUTE if allow_web else PermissionLevel.DENY,
                tool_generation=PermissionLevel.EXECUTE,
                tool_execution=PermissionLevel.EXECUTE,
            )

            active_agent = Agent(
                name=agent_name or "CustomAgent",
                role=role_enum,
                goal=agent_goal or "Complete the requested user task",
                allow_web_access=allow_web,
                permissions=perms,
            )

        import shutil

        capsem_enabled = getattr(settings.runtime, "capsem_enabled", False)
        capsem_active = capsem_enabled and bool(shutil.which("capsem"))
        execution_backend = getattr(settings.runtime, "execution_backend", "local")
        if execution_backend == "nebius_sandbox":
            sandbox_lbl = "Nebius Token Factory"
        elif capsem_enabled:
            sandbox_lbl = "CapSem" if capsem_active else "CapSem unavailable (execution blocked)"
        else:
            sandbox_lbl = "Local execution"

        console.print()
        console.print(
            Align.center(
                Panel.fit(
                    Text.assemble(
                        ("Agent  ", "dim"),
                        (f"{active_agent.name if active_agent else 'Default agent'}", "white bold"),
                        ("\nSandbox  ", "dim"),
                        (sandbox_lbl, "yellow"),
                    ),
                    title="[bold cyan]NANOSCRYPT[/bold cyan]",
                    subtitle=f"session {sess_id}",
                    border_style="cyan",
                    padding=(1, 2),
                )
            )
        )
        console.print()

        # Personalized login / boot messages
        try:
            prof = await orchestrator.user_personal_memory.get_profile()
            user_name = prof.get("name", os.getlogin() or "GuestDeveloper")
        except Exception:
            user_name = os.getlogin() or "GuestDeveloper"
        console.print(
            f"[green]●[/green] Ready for [bold]{user_name}[/bold]  "
            f"[dim]· {settings.llm.model}[/dim]"
        )

        if hasattr(orchestrator, "mcp_manager"):
            for s_name in orchestrator.mcp_manager.servers.keys():
                console.print(f"[dim]↳ {s_name} connected[/dim]")
            if orchestrator.mcp_manager.servers:
                console.print(f"[dim]{len(orchestrator.mcp_manager.servers)} tool server(s) ready[/dim]")
        console.print()

        async def execute_prompt(user_prompt: str):
            from nanoscrypt.core.harness import AgentHarness

            stats_before = meter.get_stats()
            
            # Setup harness
            harness = AgentHarness(
                orchestrator,
                session,
                active_agent,
                max_turns=settings.runtime.max_agent_turns,
                session_store=session_store,
            )

            # Setup the progress display before approval handling so it can be
            # paused while the terminal waits for a visible y/n response.
            has_printed_response_header = False
            status = console.status("Thinking…", spinner="dots")
            status.start()
            status_active = True

            def cli_tavily_api_key_callback():
                nonlocal status_active
                if status_active:
                    status.stop()
                    status_active = False
                console.print("\n[cyan]Tavily web search needs an API key.[/cyan]")
                if not sys.stdin.isatty():
                    return None
                try:
                    key = typer.prompt("Tavily API key", hide_input=True).strip()
                except (EOFError, KeyboardInterrupt):
                    return None
                if not key:
                    return None
                if status_active is False:
                    status.update("Continuing with Tavily search…")
                    status.start()
                    status_active = True
                return key

            orchestrator.tavily_api_key_callback = cli_tavily_api_key_callback

            # Setup interactive console approval handler
            def cli_approval_callback(req: ApprovalRequest) -> bool:
                nonlocal status_active
                if status_active:
                    status.stop()
                    status_active = False
                console.print()

                # Risk level color determination
                risk_colors = {
                    "low": "green",
                    "medium": "yellow",
                    "high": "red",
                    "critical": "bold red reverse",
                }
                color = risk_colors.get(req.risk_level.lower(), "red")

                title = f"[bold {color}]SECURITY APPROVAL REQUIRED ({req.risk_level.upper()} RISK)[/bold {color}]"

                details = (
                    f"Operation: [bold cyan]{req.approval_type.value}[/bold cyan]\n"
                )
                details += f"Description: {req.description}\n\n"

                if req.approval_type == ApprovalType.WEB_ACCESS:
                    details += "[bold red]WEB SAFETY WARNING:[/bold red] This tool executes code that requests external web connectivity.\n"
                    if req.resource_details:
                        details += f"Resource Details: {req.resource_details}\n"
                elif req.approval_type == ApprovalType.FILE_ACCESS:
                    details += "[bold yellow]File System Warning:[/bold yellow] This tool attempts to read or write local files.\n"

                console.print(
                    Panel(
                        details.strip(), title=title, border_style=color, padding=(1, 2)
                    )
                )
                console.print()
                approved = typer.confirm("Do you authorize this action?", default=False)
                console.print()
                if approved:
                    status.update("Continuing after approval…")
                    status.start()
                    status_active = True
                return approved

            # Register the approval callback
            orchestrator.approval_gate.approval_callback = cli_approval_callback

            # Event listener for real-time progress events
            def event_listener(event):
                nonlocal has_printed_response_header, status_active
                if event.type == "thinking_delta":
                    # Never render planner reasoning in the terminal. It is
                    # internal control data, not user-facing progress.
                    status.update("Thinking…")
                elif event.type == "progress":
                    status.update(event.message)
                elif event.type == "sandbox_operation":
                    console.print(
                        f"[dim]Nebius operation UUID: {event.operation_id}[/dim]",
                        markup=True,
                        highlight=False,
                    )
                    console.file.flush()
                elif event.type == "sandbox_output":
                    status.update("Receiving sandbox output…")
                    style = "red" if event.channel == "stderr" else None
                    console.print(
                        event.text,
                        end="",
                        markup=False,
                        highlight=False,
                        style=style,
                    )
                    console.file.flush()
                elif event.type == "tool_execution_start":
                    status.update(f"Running {event.tool_name}…")
                elif event.type == "tool_execution_end":
                    status.stop()
                    status_active = False
                    status_str = "[green]completed[/green]" if event.success else "[red]failed[/red]"
                    console.print(f"  [dim]Tool {event.tool_name} {status_str}[/dim]")
                    if event.error:
                        console.print(f"[red]Error: {event.error}[/red]")
                elif event.type == "message_start":
                    if status_active:
                        status.stop()
                        status_active = False
                    console.print("\n[bold green]Nanoscrypt[/bold green] ", end="")
                    has_printed_response_header = True
                elif event.type == "message_delta":
                    console.print(event.delta, end="", markup=False, highlight=False)
                    console.file.flush()

            harness.subscribe(event_listener)

            # Execute via the harness prompt stream
            try:
                async for _ in harness.prompt(user_prompt):
                    pass
            finally:
                if status_active:
                    status.stop()
                
            # Retrieve final result structure
            result = harness.last_result or {}

            console.print()
            if result.get("status") == "completed":
                output_content = str(result.get("output") or result.get("response") or "")

                # Only print the response if it was not already streamed live
                if not has_printed_response_header:
                    if "\n" in output_content or "#" in output_content or "`" in output_content:
                        console.print("[bold green]Nanoscrypt[/bold green]")
                        console.print(Markdown(output_content))
                    else:
                        console.print(f"[bold green]Nanoscrypt[/bold green] {output_content}")

                if result.get("tool_name"):
                    console.print(
                        f" [dim]tool: [bold]{result.get('tool_name')}[/bold] (v{result.get('version')}) "
                        f"• duration: {result.get('runtime_ms')}ms "
                        f"• execution: {result.get('execution_id')}[/dim]"
                    )
            elif result.get("status") == "clarification_needed":
                if not has_printed_response_header:
                    console.print(
                        Panel(
                            str(result.get("response")),
                            title="[bold yellow]Clarification Needed[/bold yellow]",
                            border_style="yellow",
                            padding=(1, 2),
                        )
                    )
            elif result.get("status") == "denied":
                console.print(
                    Panel(
                        str(
                            result.get("error")
                            or "Execution blocked by security permissions."
                        ),
                        title="[bold red]Security Block[/bold red]",
                        border_style="red",
                        padding=(1, 2),
                    )
                )
            elif result.get("action_taken") == "evolve_tool" and result.get("status") == "failed":
                rb_ver = result.get("rollback_version", 1)
                console.print(
                    Panel(
                        f"[yellow]Tool evolution was rejected to preserve stability.[/yellow]\n\n"
                        f"{result.get('error')}\n\n"
                        f"[green]✓ Automatic Rollback:[/green] System reverted safely to verified base [bold]v{rb_ver}[/bold].",
                        title="[bold yellow]Evolution Test Regression - Rolled Back[/bold yellow]",
                        border_style="yellow",
                        padding=(1, 2),
                    )
                )
            else:
                error_detail = str(result.get("error") or "Unknown error occurred")
                if result.get("execution_id") is not None:
                    error_detail += (
                        f"\n\nExecution ID for tool-outcome feedback: "
                        f"{result['execution_id']}"
                    )
                console.print(
                    Panel(
                        error_detail,
                        title="[bold red]Execution Failed[/bold red]",
                        border_style="red",
                        padding=(1, 2),
                    )
                )

            stats_after = meter.get_stats()
            spent = stats_after["cost"] - stats_before["cost"]
            in_diff = stats_after["input_tokens"] - stats_before["input_tokens"]
            out_diff = stats_after["output_tokens"] - stats_before["output_tokens"]
            console.print(
                f"[dim]  turn ${spent:.5f}  ·  in {in_diff:,} / out {out_diff:,} tokens  ·  "
                f"session total ${stats_after['cost']:.5f}[/dim]"
            )

        def handle_slash(command_line: str) -> bool:
            cmd, _, rest = command_line.strip().partition(" ")
            cmd = cmd.lower()
            if cmd in ("/", "/help"):
                console.print("[bold]Available Commands:[/bold]")
                console.print("  /help                Show this help menu")
                console.print("  /setup               Re-run initial provider & API key setup wizard")
                console.print("  /config              Display stored user root configuration")
                console.print("  /cost                Show cumulative token and USD cost")
                console.print("  /model               Show current LLM model configuration")
                console.print("  /model <model_name>  Change the active model dynamically")
                console.print("  /profile             View stored personal user memory profile")
                console.print("  /profile set <k> <v> Set or update a personal user trait manually")
                console.print("  /memory search <q>   Search memories semantically using MemMachine")
                console.print("  /clear               Clear the conversation session history")
                console.print("  /history             Print history of the current session")
                console.print("  /exit                Exit the REPL session")
                return True
            elif cmd in ("/setup", "/configure"):
                from nanoscrypt.cli.setup import prompt_provider_and_key
                if prompt_provider_and_key(force=True):
                    from nanoscrypt.config.settings import reload_settings
                    new_cfg = reload_settings()
                    if hasattr(orchestrator.planner.llm, "default_model"):
                        orchestrator.planner.llm.default_model = new_cfg.llm.model
                    if hasattr(orchestrator.generator.llm, "default_model"):
                        orchestrator.generator.llm.default_model = new_cfg.llm.model
                    if orchestrator.repair_loop and hasattr(orchestrator.repair_loop.llm, "default_model"):
                        orchestrator.repair_loop.llm.default_model = new_cfg.llm.model
                return True
            elif cmd == "/config":
                from nanoscrypt.config.user_config import GLOBAL_CONFIG_FILE, load_global_config
                cfg = load_global_config()
                if not cfg:
                    console.print(f"  [yellow]No configuration found in {GLOBAL_CONFIG_FILE}. Run /setup to configure.[/yellow]")
                else:
                    console.print(f"[bold]Global User Configuration ({GLOBAL_CONFIG_FILE}):[/bold]")
                    for section, values in cfg.items():
                        console.print(f"  [cyan][{section}][/cyan]")
                        if isinstance(values, dict):
                            for k, v in values.items():
                                display_v = "••••••••" if "key" in k.lower() and v else v
                                console.print(f"    {k} = {display_v}")
                return True
            elif cmd == "/memory":
                rest = rest.strip()
                if rest.startswith("search "):
                    query = rest[7:].strip()
                    if query:
                        from nanoscrypt.utils.async_runner import run_sync
                        res = run_sync(orchestrator.memmachine.search_memories(user_id="default_user", query=query))
                        if res:
                            source_desc = "MemMachine" if getattr(orchestrator.memmachine, "_connected", False) else "SQLite"
                            console.print(f"[bold]{source_desc} Memory Search Results for '{query}':[/bold]")
                            for idx, item in enumerate(res, 1):
                                text = item.get("text", str(item))
                                score_info = f" [dim](match: {int(item.get('score', 1.0) * 100)}%)[/dim]" if "score" in item else ""
                                console.print(f"  {idx}. [cyan]{text}[/cyan]{score_info}")
                        else:
                            console.print(f"  [dim]No memory matches found for '{query}'.[/dim]")
                    else:
                        console.print("  [yellow]Usage:[/yellow] /memory search <query>")
                elif rest.startswith("add "):
                    mem_text = rest[4:].strip()
                    if mem_text:
                        from nanoscrypt.utils.async_runner import run_sync
                        ok = run_sync(orchestrator.memmachine.add_memory(user_id="default_user", agent_id="orchestrator", text=mem_text))
                        if ok:
                            console.print(f"  [green]Saved memory:[/green] {mem_text}")
                        else:
                            console.print("  [red]Failed to save memory.[/red]")
                    else:
                        console.print("  [yellow]Usage:[/yellow] /memory add <text to remember>")
                else:
                    engine_type = "MemMachine (Live)" if getattr(orchestrator.memmachine, "_connected", False) else "SQLite (Local Persistent)"
                    console.print(f"  [bold]Memory Engine:[/bold] [cyan]{engine_type}[/cyan]")
                    console.print("  [bold]Commands:[/bold]")
                    console.print("    /memory search <query>  Search memories semantically or by keywords")
                    console.print("    /memory add <text>      Add a persistent memory manually")
                return True
            elif cmd == "/profile":
                rest = rest.strip()
                if rest.startswith("set "):
                    _, _, set_args = rest.partition(" ")
                    trait_key, _, trait_val = set_args.partition(" ")
                    if trait_key and trait_val:
                        from nanoscrypt.utils.async_runner import run_sync
                        run_sync(orchestrator.user_personal_memory.set_trait(trait_key, trait_val))
                        console.print(f"  [green]Updated personal memory trait:[/green] {trait_key} -> {trait_val}")
                    else:
                        console.print("  [yellow]Usage:[/yellow] /profile set <trait_key> <trait_value>")
                else:
                    from nanoscrypt.utils.async_runner import run_sync
                    prof = run_sync(orchestrator.user_personal_memory.get_profile())
                    if prof:
                        console.print("[bold]User Personal Memory Profile:[/bold]")
                        for k, v in prof.items():
                            console.print(f"  • [cyan]{k}:[/cyan] {v}")
                    else:
                        console.print("  [dim]No personal user memory recorded yet.[/dim]")
                return True
            elif cmd == "/cost":
                console.print(f"  [bold]Cumulative Cost:[/bold] {meter.status()}")
                return True
            elif cmd == "/model":
                rest = rest.strip()
                if rest:
                    from nanoscrypt.config.settings import settings
                    settings.llm.model = rest
                    if hasattr(orchestrator.planner.llm, "default_model"):
                        orchestrator.planner.llm.default_model = rest
                    if hasattr(orchestrator.generator.llm, "default_model"):
                        orchestrator.generator.llm.default_model = rest
                    if orchestrator.repair_loop and hasattr(orchestrator.repair_loop.llm, "default_model"):
                        orchestrator.repair_loop.llm.default_model = rest
                    console.print(f"  [green]Model updated to:[/green] {rest}")
                else:
                    from nanoscrypt.config.settings import settings
                    console.print(f"  [bold]Active Model:[/bold] {settings.llm.model}")
                return True
            elif cmd == "/clear":
                session.history.clear()
                session.conversation_history.clear()
                session_store.save(session)
                console.print("  [dim]Session history cleared.[/dim]")
                return True
            elif cmd == "/history":
                if not session.history:
                    console.print("  [dim]No history in this session yet.[/dim]")
                else:
                    console.print("[bold]Session Execution History:[/bold]")
                    for idx, item in enumerate(session.history):
                        status_str = "[green]SUCCESS[/green]" if item.success else "[red]FAILED[/red]"
                        console.print(
                            f"  {idx+1}. Tool: [bold]{item.tool_name}[/bold] (v{item.version}) • Status: {status_str}"
                        )
                        if item.error:
                            console.print(f"     Error: {item.error}")
                return True
            elif cmd in ("/exit", "/quit"):
                console.print("[dim]Exiting REPL session cleanly...[/dim]")
                sys.exit(0)
            else:
                console.print(f" [red]Unknown command: {cmd}. Type /help for available commands.[/red]")
                return True

        # One-shot mode vs Interactive REPL mode
        if prompt:
            console.print(
                Panel.fit(
                    Text(prompt),
                    title="[bold blue]You[/bold blue]",
                    border_style="blue",
                )
            )
            console.print()
            await execute_prompt(prompt)
        else:
            console.print("[dim]/help for commands · ctrl-c or ctrl-d to exit[/dim]\n")
            
            completer = FileCompleter(Path("."))
            style = Style.from_dict({
                "prompt": "bold #38BDF8",
                "bottom-toolbar": "bg:#0F172A #CBD5E1",
                "completion-menu": "bg:#111827 #E2E8F0",
                "completion-menu.completion": "bg:#111827 #CBD5E1",
                "completion-menu.completion.current": "bg:#2563EB #FFFFFF bold",
                "completion-menu.meta": "bg:#111827 #94A3B8",
                "completion-menu.meta.current": "bg:#2563EB #DBEAFE",
                "scrollbar.background": "bg:#1F2937",
                "scrollbar.button": "bg:#64748B",
            })

            def get_bottom_toolbar():
                stats = meter.get_stats()
                cwd_name = Path(".").resolve().name
                model_name = settings.llm.model
                return f"  {cwd_name}  ·  ${stats['cost']:.5f}  ·  {model_name}  ·  Ctrl+C to cancel"

            use_prompt_toolkit = True
            try:
                session_prompt = PromptSession(
                    completer=completer,
                    style=style,
                    bottom_toolbar=get_bottom_toolbar
                )
            except Exception:
                use_prompt_toolkit = False
            
            while True:
                try:
                    if use_prompt_toolkit:
                        line = await session_prompt.prompt_async(
                            [('class:prompt', '> ')]
                        )
                    else:
                        line = console.input("[bold blue]>[/bold blue] ")
                except (EOFError, KeyboardInterrupt):
                    print()
                    console.print(f"[dim]final cost: {meter.status()}[/dim]")
                    break
                if not line.strip():
                    continue
                if line.startswith("/"):
                    if handle_slash(line):
                        continue
                else:
                    await execute_prompt(line)

    from nanoscrypt.utils.async_runner import run_sync

    run_sync(async_run())
