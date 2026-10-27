import asyncio
import json
import threading
from typing import Any

import structlog

from nanoscrypt.config.settings import settings
from nanoscrypt.core.approval import ApprovalGate, ApprovalType
from nanoscrypt.core.audit import AuditEventType, AuditLogger
from nanoscrypt.core.context import ContextBuilder
from nanoscrypt.core.generator import ToolGenerator
from nanoscrypt.core.guardrails import PolicyEngine
from nanoscrypt.core.postprocessor import CodePostProcessor

# Enterprise imports v0.2.0
from nanoscrypt.core.hooks import HookManager, HookType
from nanoscrypt.core.memory import EntityMemory, LongTermMemory, ShortTermMemory
from nanoscrypt.core.pipeline import Pipeline, PipelineExecutor, PipelineStep
from nanoscrypt.core.planner import Planner
from nanoscrypt.core.registry import ToolRegistry
from nanoscrypt.core.repair import RepairLoop
from nanoscrypt.core.runtime import ExecutionResult, RuntimeManager
from nanoscrypt.core.validator import ToolValidator
from nanoscrypt.core.versioning import VersionManager
from nanoscrypt.models.agent import Agent, AgentRole
from nanoscrypt.models.plan import PlannerDecision
from nanoscrypt.models.session import Session, SessionToolOutput
from nanoscrypt.models.tool import GeneratedTool, ToolManifest

logger = structlog.get_logger()


class Orchestrator:
    """Enterprise-grade Orchestrator coordinating Agent Roles, Memory, Guardrails, Web-Tool Warnings, and human-in-the-loop approvals."""

    def __init__(
        self,
        context_builder: ContextBuilder,
        planner: Planner,
        generator: ToolGenerator,
        validator: ToolValidator,
        runtime_manager: RuntimeManager,
        registry: ToolRegistry,
        version_manager: VersionManager,
        repair_loop: RepairLoop | None = None,
        hook_manager: HookManager | None = None,
        approval_gate: ApprovalGate | None = None,
        audit_logger: AuditLogger | None = None,
        short_term_memory: ShortTermMemory | None = None,
        long_term_memory: LongTermMemory | None = None,
    ):
        self.context_builder = context_builder
        self.planner = planner
        self.generator = generator
        self.validator = validator
        self.runtime_manager = runtime_manager
        self.registry = registry
        self.version_manager = version_manager
        self.repair_loop = repair_loop
        if self.repair_loop and getattr(self.repair_loop, "session_factory", None) is None:
            self.repair_loop.session_factory = getattr(self.registry, "session_factory", None)

        # Initialize enterprise modules with defaults
        self.hook_manager = hook_manager or HookManager()
        self.approval_gate = approval_gate or ApprovalGate()
        self.audit_logger = audit_logger or AuditLogger(registry.session_factory)
        self.short_term_memory = short_term_memory or ShortTermMemory(
            max_entries=settings.memory.short_term_max_entries
        )
        self.long_term_memory = long_term_memory or LongTermMemory(
            registry.session_factory
        )
        self.entity_memory = EntityMemory(registry.session_factory)
        from nanoscrypt.core.memory import UserPersonalMemory
        self.user_personal_memory = UserPersonalMemory(registry.session_factory)
        from nanoscrypt.core.memmachine_engine import MemMachineEngine
        self.memmachine = MemMachineEngine(registry.session_factory)
        self.policy_engine = PolicyEngine()
        self.pipeline_executor = PipelineExecutor(self)
        from nanoscrypt.core.mutator import ToolMutator
        from nanoscrypt.core.similarity import SimilarityMatcher
        self.tool_mutator = ToolMutator(llm=self.generator.llm)
        self.similarity_matcher = SimilarityMatcher()

    async def _execute_tool_cancellable(
        self,
        session_id: str,
        input_data: str,
        requirements: list[str],
        cancellation_event: threading.Event | None,
        sandbox_output_callback=None,
        sandbox_operation_callback=None,
    ) -> ExecutionResult:
        """Run the blocking tool runtime off-loop and await process cleanup on cancel."""
        cancel_event = cancellation_event or threading.Event()
        worker = asyncio.create_task(
            asyncio.to_thread(
                self.runtime_manager.execute_tool,
                session_id=session_id,
                input_data=input_data,
                requirements=requirements,
                cancellation_event=cancel_event,
                sandbox_output_callback=(
                    (lambda channel, text: sandbox_output_callback(channel, text))
                    if sandbox_output_callback is not None
                    else None
                ),
                sandbox_operation_callback=(
                    (lambda operation_id: sandbox_operation_callback(operation_id))
                    if sandbox_operation_callback is not None
                    else None
                ),
            )
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancel_event.set()
            try:
                await asyncio.shield(worker)
            except Exception:
                pass
            raise

    async def _extract_parameters(
        self, user_prompt: str, input_schema: dict, agent_name: str, session_id: str
    ) -> str:
        """Asks the LLM to extract execution arguments matching the input schema from the user prompt."""
        if not input_schema:
            return "{}"

        # If user_prompt is already a JSON dictionary matching/containing parameters, use it directly
        try:
            parsed = json.loads(user_prompt)
            if isinstance(parsed, dict):
                clean_params = {}
                data_dict = parsed.get("input_data", parsed) if isinstance(parsed.get("input_data"), dict) else parsed
                for k, v in data_dict.items():
                    if k == "tool_name":
                        continue
                    if isinstance(v, str):
                        val = v.strip()
                        if (val.startswith("'") and val.endswith("'")) or (val.startswith('"') and val.endswith('"')):
                            val = val[1:-1].strip()
                        clean_params[k] = val
                    else:
                        clean_params[k] = v
                return json.dumps(clean_params)
        except Exception:
            pass

        system_prompt = (
            "You are a parameter extraction assistant. Your task is to extract execution arguments "
            "from the user prompt matching the target input schema, returning a JSON dictionary.\n"
            "If a parameter is a filename or file path mentioned in the user prompt (e.g. 'main.py' or 'test_folder/main.py'), extract that exact path as the filename.\n"
            "If a content or text parameter is expected and not explicitly provided in full, infer reasonable initial content based on the filename/extension (e.g. '# main.py entry point\\nprint(\"Hello, World!\")').\n"
            "Respond ONLY with a valid JSON dictionary containing the extracted parameters. Do not add markdown formatting or explanation."
        )

        user_msg = f"User Prompt: {user_prompt}\nTarget Input Schema: {input_schema}\nJSON Output:"

        try:
            # We call the planner's LLM to generate the param dict
            raw_res = await self.planner.llm.generate(
                prompt=user_msg, system_prompt=system_prompt, temperature=0.0
            )
            raw_res = raw_res.strip()
            if raw_res.startswith("```json"):
                raw_res = raw_res[7:]
            if raw_res.startswith("```"):
                raw_res = raw_res[3:]
            if raw_res.endswith("```"):
                raw_res = raw_res[:-3]
            raw_res = raw_res.strip()

            # Verify it is valid JSON
            params = json.loads(raw_res)

            # Inject type-safe default fallback values for missing parameters to prevent wrapper crash on initial run
            if isinstance(params, dict):
                modified = False
                for param_name, param_desc in input_schema.items():
                    if param_name not in params:
                        desc_lower = str(param_desc).lower()
                        if "list" in desc_lower or "array" in desc_lower or "categories" in param_name.lower():
                            params[param_name] = ["test"]
                        elif "int" in desc_lower or "integer" in desc_lower or "count" in desc_lower or "number" in desc_lower:
                            params[param_name] = 1
                        elif "bool" in desc_lower or "boolean" in desc_lower:
                            params[param_name] = True
                        elif "path" in param_name.lower() or "file" in param_name.lower():
                            params[param_name] = "test_file.txt"
                        else:
                            params[param_name] = "test"
                        modified = True
                if modified:
                    raw_res = json.dumps(params)

            # Log the parameter extraction audit
            await self.audit_logger.log_event(
                event_type=AuditEventType.LLM_CALL,
                session_id=session_id,
                agent_name=agent_name,
                details={
                    "action": "parameter_extraction",
                    "prompt_len": len(user_prompt),
                },
                cost=getattr(self.planner.llm, "last_cost", 0.0),
                token_usage=getattr(self.planner.llm, "last_input_tokens", 0) + getattr(self.planner.llm, "last_output_tokens", 0),
            )
            return raw_res
        except Exception as e:
            # If parsing fails, create a type-safe dict fallback of mock values to prevent execution TypeError
            try:
                fallback_params = {}
                for param_name, param_desc in input_schema.items():
                    desc_lower = str(param_desc).lower()
                    if "list" in desc_lower or "array" in desc_lower or "categories" in param_name.lower():
                        fallback_params[param_name] = ["test"]
                    elif "int" in desc_lower or "integer" in desc_lower or "count" in desc_lower or "number" in desc_lower:
                        fallback_params[param_name] = 1
                    elif "bool" in desc_lower or "boolean" in desc_lower:
                        fallback_params[param_name] = True
                    elif "path" in param_name.lower() or "file" in param_name.lower():
                        fallback_params[param_name] = "test_file.txt"
                    else:
                        fallback_params[param_name] = "test"
                return json.dumps(fallback_params)
            except Exception:
                return "{}"

    async def execute_task(
        self,
        user_prompt: str,
        session: Session,
        agent: Agent | None = None,
        pre_execute_hook=None,
        cancellation_event: threading.Event | None = None,
        stream_callback=None,
        progress_callback=None,
    ) -> dict[str, Any]:
        """Executes a task under the context of an Agent, running hooks, memory retrieval, guardrails, and approvals."""

        # Determine the active agent context
        active_agent = agent or Agent(
            name="orchestrator",
            role=AgentRole.PLANNER,
            goal="Coordinate tool lifecycle to answer user query.",
        )
        session.active_agent = active_agent.name

        log = logger.bind(
            session_id=session.id, agent=active_agent.name, role=active_agent.role.value
        )
        log.info("orchestrator_task_execution_started", prompt=user_prompt)

        async def report_progress(message: str) -> None:
            if progress_callback is None:
                return
            result = progress_callback(message)
            if hasattr(result, "__await__"):
                await result

        loop = asyncio.get_running_loop()

        def report_sandbox_operation(operation_id: str) -> None:
            if progress_callback is None:
                return
            from nanoscrypt.core.events import SandboxOperationEvent

            async def publish() -> None:
                callback = getattr(progress_callback, "sandbox_operation", None)
                if callback is not None:
                    result = callback(SandboxOperationEvent(operation_id=operation_id))
                    if hasattr(result, "__await__"):
                        await result

            future = asyncio.run_coroutine_threadsafe(publish(), loop)
            future.result(timeout=15)

        def report_sandbox_output(channel: str, text: str) -> None:
            if progress_callback is None:
                return
            from nanoscrypt.core.events import SandboxOutputEvent

            async def publish() -> None:
                callback = getattr(progress_callback, "sandbox_output", None)
                if callback is not None:
                    result = callback(SandboxOutputEvent(channel=channel, text=text))
                    if hasattr(result, "__await__"):
                        await result

            future = asyncio.run_coroutine_threadsafe(publish(), loop)
            future.result(timeout=15)

        # Check for Prefix Commands
        from nanoscrypt.core.command_router import PrefixCommandRouter
        cmd_type, payload = PrefixCommandRouter.parse(user_prompt)
        if cmd_type != "normal":
            log.info("routing_to_prefix_command_handler", command_type=cmd_type)
            from nanoscrypt.core.command_handlers import handle_todo, handle_inject, handle_confluence
            if cmd_type == "todo":
                return await handle_todo(self, payload, session)
            elif cmd_type == "inject":
                return await handle_inject(self, payload, session)
            elif cmd_type == "confluence":
                return await handle_confluence(self, payload, session)
            elif cmd_type == "invalid":
                return {
                    "status": "failed",
                    "error": f"Unknown special command: '{payload}'. Available special commands: //TODO, //inject, //confluence"
                }

        # Avoid a full planner + LLM round trip for common conversational openers.
        # The CLI can return these immediately without waiting on a model endpoint.
        import re

        latest_request = user_prompt.rsplit("Current user request:\n", 1)[-1].strip()
        normalized_request = re.sub(
            r"[^\w\s']", "", latest_request.lower()
        ).strip()
        greeting_requests = {
            "hi", "hello", "hey", "howdy", "yo", "sup",
            "good morning", "good afternoon", "good evening",
            "thanks", "thank you", "bye", "goodbye",
        }
        greeting = normalized_request.removesuffix(" nanoscrypt")
        if greeting in greeting_requests:
            response = (
                "Hello! How can I help?"
                if greeting not in {"thanks", "thank you"}
                else "You’re welcome!"
            )
            return {
                "status": "completed",
                "action_taken": "direct_response",
                "response": response,
            }

        # Route to CodeAgentExecutor if enabled
        if settings.runtime.code_agent_enabled:
            from nanoscrypt.core.code_agent import CodeAgentExecutor
            executor = CodeAgentExecutor(self)
            return await executor.execute(
                user_prompt, session, active_agent, cancellation_event
            )

        # 1. Fire BEFORE_PLAN Lifecycle Hook
        await report_progress("Gathering project context…")
        hook_context = {
            "session": session,
            "prompt": user_prompt,
            "agent": active_agent,
        }
        hook_context = await self.hook_manager.fire(HookType.BEFORE_PLAN, hook_context)
        user_prompt = hook_context.get("prompt", user_prompt)

        # 2. Query Long-Term Memory for contextual recall
        past_memories = []
        if settings.memory.enabled:
            try:
                past_memories = await asyncio.wait_for(
                    self.long_term_memory.recall(user_prompt[:50], category="tasks"),
                    timeout=2.0,
                )
            except Exception:
                past_memories = []
            self.short_term_memory.add(
                "user", user_prompt, {"agent": active_agent.name}
            )

        # 3. Search existing tools in registry for context assembly
        all_tools = await self.registry.search("", shared_only=True)
        serialized_tools = []
        for t in all_tools:
            serialized_tools.append(
                {
                    "name": t.name,
                    "purpose": t.purpose,
                    "input_schema": t.input_schema,
                    "output_schema": t.output_schema,
                    "success_rate": t.success_rate,
                }
            )

        # 4. Extract personal facts & fetch user profile
        personal_profile = None
        semantic_memories = []
        if settings.memory.enabled:
            # 4a. Extract personal facts (gated regex/llm)
            try:
                await asyncio.wait_for(
                    self.user_personal_memory.extract_and_store(user_prompt),
                    timeout=3.0,
                )
            except Exception as e:
                log.debug("orchestrator_personal_memory_extract_error", error=str(e))

            try:
                personal_profile = await asyncio.wait_for(
                    self.user_personal_memory.get_profile(), timeout=2.0
                )
            except Exception as e:
                log.debug("orchestrator_personal_memory_profile_error", error=str(e))

            # 4b. Add to persistent semantic memory
            try:
                await self.memmachine.add_memory(
                    user_id="default_user",
                    agent_id=active_agent.name,
                    text=user_prompt,
                )
            except Exception as e:
                log.debug("orchestrator_memmachine_add_error", error=str(e))

            # 4c. Search matching semantic memories for prompt context
            try:
                semantic_memories = await self.memmachine.search_memories(
                    user_id="default_user", query=user_prompt
                )
            except Exception as e:
                log.debug("orchestrator_memmachine_search_error", error=str(e))

        # Build Context Prompt
        assembled_prompt = self.context_builder.assemble(
            user_prompt=user_prompt,
            session=session,
            registered_tools=serialized_tools,
            short_term_memory=self.short_term_memory.get_context()
            if settings.memory.enabled
            else None,
            personal_profile=personal_profile,
            semantic_memories=semantic_memories,
        )

        # Inject memory recall results into prompt if present
        if past_memories:
            memory_context = "\n=== RECALLED MEMORIES ===\n" + "\n".join(
                f"- Task pattern: {m['key']} -> Result: {m['value']}"
                for m in past_memories
            )
            assembled_prompt = memory_context + "\n" + assembled_prompt

        # Inject Agent goals & backstory
        agent_context = (
            f"=== ACTIVE AGENT CONTROLLER ===\n"
            f"Agent Name: {active_agent.name}\n"
            f"Role: {active_agent.role.value}\n"
            f"Goal: {active_agent.goal}\n"
            f"Backstory: {active_agent.backstory}\n\n"
        )
        assembled_prompt = agent_context + assembled_prompt

        # 5. Call Planner LLM (or bypass if user_prompt is direct parameter payload from a pipeline step)
        direct_tool_name = None
        direct_params = None
        try:
            parsed = json.loads(user_prompt)
            if isinstance(parsed, dict):
                # Check if prompt specifies tool_name or matches registered tool schema
                if "tool_name" in parsed and await self.registry.get(parsed["tool_name"]):
                    direct_tool_name = parsed["tool_name"]
                    direct_params = parsed.get("input_data", parsed)
                else:
                    # Prefer the most specific registered tool match based on all required input keys.
                    for t in all_tools:
                        if t.input_schema and all(k in parsed for k in t.input_schema.keys()):
                            direct_tool_name = t.name
                            direct_params = parsed
                            break
        except Exception:
            pass

        explain_keywords = [
            "explain",
            "what are",
            "what is",
            "summarize",
            "describe",
            "analyze",
            "overview",
            "tell me",
        ]
        action_keywords = [
            "build",
            "create",
            "generate",
            "make",
            "develop",
            "implement",
            "write",
            "delete",
            "mkdir",
            "remove",
            "run",
            "reuse",
        ]
        prompt_lower = latest_request.lower()
        web_search_phrases = (
            "search the web", "search web", "web search", "browse the web",
            "search online", "look up online", "look online", "find online",
        )
        is_explicit_web_search = any(phrase in prompt_lower for phrase in web_search_phrases)
        has_action_intent = any(kw in prompt_lower for kw in action_keywords) or is_explicit_web_search
        question_starters = (
            "who ", "what ", "when ", "where ", "why ", "how ",
            "is ", "are ", "am ", "do ", "does ", "did ",
            "can ", "could ", "would ", "will ", "should ",
            "have ", "has ", "tell me ",
        )
        is_natural_language_question = (
            user_prompt.rstrip().endswith("?")
            or prompt_lower.startswith(question_starters)
        )
        is_explain_query = (
            (
                any(kw in prompt_lower for kw in explain_keywords)
                or is_natural_language_question
                or ("@" in latest_request and not has_action_intent)
            )
            and not has_action_intent
            and not is_explicit_web_search
        )

        if direct_tool_name:
            log.info("direct_pipeline_tool_execution_bypassing_planner", tool_name=direct_tool_name)
            target_tool_db = await self.registry.get(direct_tool_name)
            decision = PlannerDecision(
                action="reuse_tool",
                tool_name=direct_tool_name,
                tool_purpose=target_tool_db.purpose if target_tool_db else "tool execution",
                reasoning="Direct pipeline step payload execution.",
            )
        elif is_explain_query:
            log.info("direct_text_query_bypassing_planner", prompt=user_prompt)
            decision = PlannerDecision(
                action="direct_response",
                reasoning="Direct text explanation query with file context.",
            )
        else:
            # Check similarity matcher for potential tool evolution candidates
            evo_candidate = self.similarity_matcher.find_candidate(
                user_prompt=user_prompt,
                registered_tools=serialized_tools,
            )
            await report_progress("Planning the task…")
            decision = await self.planner.decide(assembled_prompt)

            # If planner chose generate_tool but a high-scoring evolution candidate exists, elevate to evolve_tool
            if decision.action == "generate_tool" and evo_candidate:
                log.info(
                    "orchestrator_elevating_to_evolve_tool",
                    candidate_tool=evo_candidate.tool_name,
                    base_version=evo_candidate.base_version,
                    score=evo_candidate.similarity_score,
                )
                decision.action = "evolve_tool"
                decision.tool_name = evo_candidate.tool_name
                decision.base_version = evo_candidate.base_version
                decision.similarity_score = evo_candidate.similarity_score
                if not decision.mutation_goals and evo_candidate.missing_parameters:
                    decision.mutation_goals = [f"Add support for {p}" for p in evo_candidate.missing_parameters]
        log.info("orchestrator_planner_decision", action=decision.action)

        # Log LLM call to audit logger
        await self.audit_logger.log_event(
            event_type=AuditEventType.LLM_CALL,
            session_id=session.id,
            agent_name=active_agent.name,
            details={
                "action": "planning_decision",
                "decision_action": decision.action,
                "tool_name": decision.tool_name,
            },
            cost=getattr(self.planner.llm, "last_cost", 0.0),
            token_usage=getattr(self.planner.llm, "last_input_tokens", 0) + getattr(self.planner.llm, "last_output_tokens", 0),
        )

        # Fire AFTER_PLAN Lifecycle Hook
        hook_context.update({"decision": decision})
        hook_context = await self.hook_manager.fire(HookType.AFTER_PLAN, hook_context)

        # 6. Handle planning actions
        if decision.pipeline_steps and len(decision.pipeline_steps) > 1:
            all_registered = True
            for s in decision.pipeline_steps:
                t_name = s.get("tool_name")
                if t_name and not (await self.registry.get(t_name)):
                    all_registered = False
                    break
            if all_registered:
                log.info("routing_to_execute_pipeline_from_steps", steps_count=len(decision.pipeline_steps))
                decision.action = "execute_pipeline"

        if decision.action not in ("reuse_tool", "execute_pipeline") and not decision.tool_name:
            if (any(kw in prompt_lower for kw in explain_keywords) or "@" in user_prompt) and not is_explicit_web_search:
                if not any(kw in prompt_lower for kw in ["create ", "delete ", "mkdir ", "remove ", "write ", "reuse ", "run "]):
                    log.info("forcing_direct_response_for_explanation_request", prompt=user_prompt)
                    decision.action = "direct_response"

        op_keywords = ["create", "folder", "directory", "make", "mkdir", "write", "file", "delete", "remove"]
        if (
            decision.action == "direct_response"
            and any(kw in prompt_lower for kw in op_keywords)
            and not any(kw in prompt_lower for kw in explain_keywords)
        ):
            log.warning("overriding_direct_response_for_workspace_operation", prompt=user_prompt)
            decision.action = "generate_tool"
            decision.tool_name = "workspace_file_ops_tool"
            decision.tool_purpose = f"Perform requested workspace operation: {user_prompt}"
            decision.input_description = "workspace parameters"
            decision.output_description = "status of operation"

        if decision.action == "direct_response":
            resp_val = decision.response
            streamed_response = False
            if not resp_val or resp_val == decision.reasoning:
                system_prompt = (
                    "You are Nanoscrypt, an expert AI software assistant. "
                    "Provide a concise, direct, and helpful answer to the user's question based on the provided file context. "
                    "Do not suggest writing or generating external tools."
                )
                clean_prompt = f"User Request: {user_prompt}\n\n"
                if hasattr(self.context_builder, "workspace_root"):
                    import re
                    from pathlib import Path
                    matches = re.findall(r'@([a-zA-Z0-9_\.\-/\\~]+)', user_prompt)
                    for match in matches:
                        p = Path(match) if Path(match).exists() else Path(self.context_builder.workspace_root) / match
                        if p.exists() and p.is_file():
                            clean_prompt += f"--- Content of {match} ---\n{p.read_text(encoding='utf-8', errors='replace')[:10000]}\n\n"

                try:
                    await report_progress("Writing the response…")
                    resp_val = await self.planner.llm.generate(
                        prompt=clean_prompt,
                        system_prompt=system_prompt,
                        timeout=1800.0,
                        stream_callback=stream_callback,
                    )
                    streamed_response = stream_callback is not None
                except Exception as e:
                    # Provide local fallback if LLM generation times out or fails
                    file_summaries = []
                    if hasattr(self.context_builder, "workspace_root"):
                        import re
                        from pathlib import Path
                        matches = re.findall(r'@([a-zA-Z0-9_\.\-/\\~]+)', user_prompt)
                        for match in matches:
                            p = Path(match) if Path(match).exists() else Path(self.context_builder.workspace_root) / match
                            if p.exists() and p.is_file():
                                file_summaries.append(f"### {match} Content:\n```\n{p.read_text(encoding='utf-8', errors='replace')[:2000]}\n```")
                    if file_summaries:
                        resp_val = f"Here is the content of the referenced file(s):\n\n" + "\n\n".join(file_summaries)
                    else:
                        resp_val = decision.response or decision.reasoning or str(e)

            if settings.memory.enabled:
                self.short_term_memory.add(
                    "assistant", resp_val, {"action": "direct_response"}
                )
            return {
                "status": "completed",
                "action_taken": "direct_response",
                "response": resp_val,
                "reasoning": decision.reasoning,
                "streamed": streamed_response,
            }

        if decision.action == "clarify":
            return {
                "status": "clarification_needed",
                "action_taken": "clarify",
                "response": decision.response or decision.reasoning,
            }

        if decision.action == "execute_pipeline":
            # Handle multi-tool pipelines (chaining)
            steps = [
                PipelineStep(
                    tool_name=s.get("tool_name", ""),
                    input_mapping=s.get("input_mapping") if s.get("input_mapping") is not None else s.get("inputs", {}),
                )
                for s in decision.pipeline_steps
            ]

            pipeline = Pipeline(name=f"pipeline_{session.id}", steps=steps)

            # Risk check the pipeline
            pipeline_risk = decision.risk_level or "medium"
            if self.approval_gate.should_require_approval(
                ApprovalType.HIGH_RISK_OPERATION, pipeline_risk
            ):
                approved = await self.approval_gate.request_approval(
                    session_id=session.id,
                    approval_type=ApprovalType.HIGH_RISK_OPERATION,
                    description=f"Execute multi-tool pipeline: {', '.join(s.tool_name for s in steps)}",
                    risk_level=pipeline_risk,
                    resource_details={"pipeline": pipeline.model_dump()},
                    agent_name=active_agent.name,
                )
                if not approved:
                    return {
                        "status": "denied",
                        "action_taken": "execute_pipeline",
                        "error": "Pipeline execution denied by security approval policy.",
                    }

            res = await self.pipeline_executor.execute(pipeline, session, {})
            return res

        tool_name = decision.tool_name
        if not tool_name:
            return {
                "status": "error",
                "message": "Planner requested tool action but failed to name the tool.",
            }

        target_tool = None
        version_number = 1

        # 7. Check if tool already exists and can be reused
        if decision.action == "reuse_tool" or decision.reuse_existing:
            db_tool = await self.registry.get(tool_name)
            if db_tool:
                log.info(
                    "orchestrator_reusing_tool",
                    tool_name=tool_name,
                    version=db_tool.current_version,
                )

                v_dir = self.version_manager.get_version_directory(
                    tool_name, db_tool.current_version
                )
                if v_dir and (v_dir / "tool.py").exists():
                    code = (v_dir / "tool.py").read_text(encoding="utf-8")
                    reqs = (
                        (v_dir / "requirements.txt")
                        .read_text(encoding="utf-8")
                        .splitlines()
                        if (v_dir / "requirements.txt").exists()
                        else []
                    )
                    readme = (
                        (v_dir / "README.md").read_text(encoding="utf-8")
                        if (v_dir / "README.md").exists()
                        else ""
                    )
                    tests = (
                        (v_dir / "tests.py").read_text(encoding="utf-8")
                        if (v_dir / "tests.py").exists()
                        else ""
                    )

                    target_tool = GeneratedTool(
                        name=db_tool.name,
                        code=code,
                        requirements=reqs,
                        manifest=ToolManifest(
                            name=db_tool.name,
                            language=db_tool.language,
                            entry=db_tool.entry_point,
                            dependencies=db_tool.dependencies,
                            input_schema=db_tool.input_schema,
                            output_schema=db_tool.output_schema,
                        ),
                        tests=tests,
                        readme=readme,
                    )
                    version_number = db_tool.current_version
                else:
                    log.warning(
                        "orchestrator_stored_version_files_missing_regenerating"
                    )
            else:
                log.warning(
                    "orchestrator_tool_requested_for_reuse_not_found_generating"
                )

        # 8. Generate New Tool if not found or requested
        if not target_tool:
            log.info("orchestrator_generating_new_tool", tool_name=tool_name)

            # Fire BEFORE_GENERATE hook
            hook_context = await self.hook_manager.fire(
                HookType.BEFORE_GENERATE, hook_context
            )

            # Security/HITL Check: Does tool generation require approval?
            if self.approval_gate.should_require_approval(
                ApprovalType.TOOL_GENERATION, decision.risk_level
            ):
                approved = await self.approval_gate.request_approval(
                    session_id=session.id,
                    approval_type=ApprovalType.TOOL_GENERATION,
                    description=f"Synthesize new tool: {tool_name} ({decision.tool_purpose})",
                    risk_level=decision.risk_level,
                    resource_details={
                        "tool_name": tool_name,
                        "purpose": decision.tool_purpose,
                    },
                    agent_name=active_agent.name,
                )
                if not approved:
                    await self.audit_logger.log_event(
                        event_type=AuditEventType.APPROVAL_DENIED,
                        session_id=session.id,
                        agent_name=active_agent.name,
                        details={"tool_name": tool_name, "operation": "generation"},
                    )
                    return {
                        "status": "denied",
                        "action_taken": "generate_tool",
                        "error": "Tool synthesis denied by security policy approval gate.",
                    }

            # Check if this is an evolution action
            is_evolution = decision.action == "evolve_tool"
            base_tool_for_evolution = None

            if is_evolution:
                base_ver = decision.base_version or self.version_manager.get_current_version_number(tool_name)
                base_tool_for_evolution = self.version_manager.load_version_tool(tool_name, base_ver)
                if base_tool_for_evolution:
                    log.info("orchestrator_evolving_tool_via_mutator", tool_name=tool_name, base_version=base_ver)
                    target_tool = await self.tool_mutator.evolve(
                        base_tool=base_tool_for_evolution,
                        base_version=base_ver,
                        user_prompt=user_prompt,
                        mutation_goals=decision.mutation_goals,
                    )
                else:
                    log.warning("base_tool_snapshot_missing_falling_back_to_generator", tool_name=tool_name)
                    target_tool = await self.generator.generate(decision, user_prompt=user_prompt)
                    is_evolution = False
            else:
                # Generate new tool from scratch
                await report_progress("Creating the requested tool…")
                target_tool = await self.generator.generate(decision, user_prompt=user_prompt)

            # Post-process: auto-fix common LLM code issues
            post_processor = CodePostProcessor(llm=self.generator.llm)
            target_tool = post_processor.process(target_tool)

            # Audit generate
            await self.audit_logger.log_event(
                event_type=AuditEventType.TOOL_GENERATED,
                session_id=session.id,
                agent_name=active_agent.name,
                details={
                    "tool_name": tool_name,
                    "requirements": target_tool.requirements,
                },
                cost=getattr(self.generator.llm, "last_cost", 0.0),
                token_usage=getattr(self.generator.llm, "last_input_tokens", 0) + getattr(self.generator.llm, "last_output_tokens", 0),
            )

            # Fire AFTER_GENERATE hook
            hook_context.update({"tool": target_tool})
            hook_context = await self.hook_manager.fire(
                HookType.AFTER_GENERATE, hook_context
            )

            # Validate tool with policies
            await report_progress("Checking the tool for safety…")
            val_result = self.validator.validate(target_tool)
            if not val_result.is_valid:
                errors = [
                    iss.message for iss in val_result.issues if iss.severity == "error"
                ]
                log.warning(
                    "orchestrator_tool_initial_validation_failed", errors=errors
                )

                await self.audit_logger.log_event(
                    event_type=AuditEventType.POLICY_VIOLATION,
                    session_id=session.id,
                    agent_name=active_agent.name,
                    details={"tool_name": tool_name, "issues": errors},
                )

                if self.repair_loop:
                    await report_progress("Repairing a validation issue…")
                    log.info(
                        "orchestrator_triggering_repair_loop_on_validation_failure"
                    )

                    dummy_failure = ExecutionResult(
                        stdout="",
                        stderr="\n".join(errors),
                        return_code=-1,
                        runtime_ms=0,
                        timed_out=False,
                        workspace_path=self.runtime_manager.get_session_workspace(
                            session.id
                        ),
                    )

                    self.runtime_manager.setup_workspace(session.id, target_tool)

                    repaired, _ = await self.repair_loop.repair_tool(
                        session_id=session.id,
                        tool=target_tool,
                        failure_result=dummy_failure,
                        tool_purpose=decision.tool_purpose or "utility",
                        user_prompt=user_prompt,
                    )
                    if repaired:
                        target_tool = repaired
                        val_result = self.validator.validate(target_tool)
                    else:
                        return {
                            "status": "failed",
                            "action_taken": "validation",
                            "errors": errors,
                        }
                else:
                    return {
                        "status": "failed",
                        "action_taken": "validation",
                        "errors": errors,
                    }

            if val_result.formatted_code:
                target_tool.code = val_result.formatted_code

            # Dual-Suite Regression Testing for evolved tools
            if is_evolution and base_tool_for_evolution and self.repair_loop:
                passed, reg_msg, reg_res = self.repair_loop.run_dual_suite_tests(
                    session_id=session.id,
                    base_tool=base_tool_for_evolution,
                    evolved_tool=target_tool,
                )
                if not passed:
                    log.error("orchestrator_evolution_dual_suite_failed", reason=reg_msg)
                    # Notify user of regression failure and rollback
                    return {
                        "status": "failed",
                        "action_taken": "evolve_tool",
                        "error": f"Evolution rejected due to test regression:\n{reg_msg}",
                        "rollback_version": base_ver,
                    }

            # Version tool snapshot on disk
            version_number = self.version_manager.create_version(
                tool_name=tool_name,
                code=target_tool.code,
                requirements=target_tool.requirements,
                manifest=target_tool.manifest.model_dump(),
                tests=target_tool.tests,
                readme=target_tool.readme,
                prompt=user_prompt,
                parent_version=base_ver if is_evolution else None,
                change_reason="evolution" if is_evolution else "initial",
            )

            # Register in database
            await self.registry.register(
                tool=target_tool,
                code_hash=self.version_manager.diff(
                    tool_name, base_ver if is_evolution else version_number, version_number
                )
                or ("evolution" if is_evolution else "initial"),
                prompt_used=user_prompt,
            )

        # 9. Resource Scan and HITL warnings before execution
        scan_res = self.validator.scan_resource_access(target_tool.code)

        # User warned/approved for web tools specifically
        if scan_res["network_access"]:
            log.warn("orchestrator_web_tool_detected", tool_name=tool_name)

            # Fire ON_APPROVAL_REQUIRED hook
            await self.hook_manager.fire(
                HookType.ON_APPROVAL_REQUIRED,
                {"tool_name": tool_name, "scan": scan_res},
            )

            # Check approval gate specifically for web access
            approved = await self.approval_gate.request_approval(
                session_id=session.id,
                approval_type=ApprovalType.WEB_ACCESS,
                description=f"Execute tool '{tool_name}' which performs web network connections.",
                risk_level="high",
                resource_details=scan_res,
                agent_name=active_agent.name,
            )

            if not approved:
                # Callback support backward compatibility (e.g. CLI pre_execute_hook)
                if pre_execute_hook:
                    import inspect

                    if inspect.iscoroutinefunction(pre_execute_hook):
                        approved = await pre_execute_hook(tool_name, scan_res)
                    else:
                        approved = pre_execute_hook(tool_name, scan_res)

                if not approved:
                    log.warning("orchestrator_execution_denied_by_user")
                    await self.audit_logger.log_event(
                        event_type=AuditEventType.APPROVAL_DENIED,
                        session_id=session.id,
                        agent_name=active_agent.name,
                        details={
                            "tool_name": tool_name,
                            "operation": "execution",
                            "reason": "web_access_denied",
                        },
                    )
                    return {
                        "status": "denied",
                        "action_taken": "execute_tool",
                        "error": "Execution denied by user because tool accesses external network resources.",
                    }

        # Check approvals for file access or generic execution
        elif scan_res["file_access"] and self.approval_gate.should_require_approval(
            ApprovalType.FILE_ACCESS, decision.risk_level
        ):
            approved = await self.approval_gate.request_approval(
                session_id=session.id,
                approval_type=ApprovalType.FILE_ACCESS,
                description=f"Execute tool '{tool_name}' which accesses the local file system.",
                risk_level=decision.risk_level,
                resource_details=scan_res,
                agent_name=active_agent.name,
            )
            if not approved:
                return {
                    "status": "denied",
                    "action_taken": "execute_tool",
                    "error": "Execution denied by user due to local file system access restrictions.",
                }

        # 10. Execute Tool inside Runtime Sandbox
        await report_progress("Running the tool in its workspace…")
        log.info("orchestrator_setting_up_runtime", tool_name=tool_name)

        # Fire BEFORE_EXECUTE Hook
        await self.hook_manager.fire(
            HookType.BEFORE_EXECUTE, {"tool": target_tool, "session": session}
        )

        self.runtime_manager.setup_workspace(session.id, target_tool)

        log.info("orchestrator_executing_tool_run", tool_name=tool_name)
        tool_input = await self._extract_parameters(
            user_prompt=user_prompt,
            input_schema=target_tool.manifest.input_schema
            if target_tool.manifest
            else {},
            agent_name=active_agent.name,
            session_id=session.id,
        )

        exec_res = await self._execute_tool_cancellable(
            session_id=session.id,
            input_data=tool_input,
            requirements=target_tool.requirements or [],
            cancellation_event=cancellation_event,
            sandbox_output_callback=report_sandbox_output,
            sandbox_operation_callback=report_sandbox_operation,
        )
        
        json_error = None
        if exec_res.return_code == 0 and exec_res.stdout:
            try:
                wrapped_out = json.loads(exec_res.stdout.strip())
                output_val = wrapped_out.get("output")
                if isinstance(output_val, dict) and "error" in output_val:
                    json_error = str(output_val["error"])
                elif isinstance(wrapped_out, dict) and "error" in wrapped_out:
                    json_error = str(wrapped_out["error"])
            except Exception:
                pass
                
        if json_error:
            exec_res.stderr = json_error
            
        success = exec_res.return_code == 0 and not exec_res.timed_out and not json_error

        # Self-repair logic
        if not success and self.repair_loop and not exec_res.cancelled:
            await report_progress("Repairing a failed tool run…")
            log.warning(
                "orchestrator_execution_failed_triggering_repair_loop",
                error=exec_res.stderr,
            )

            await self.audit_logger.log_event(
                event_type=AuditEventType.REPAIR_ATTEMPTED,
                session_id=session.id,
                agent_name=active_agent.name,
                details={"tool_name": tool_name, "error": exec_res.stderr},
            )

            repaired_tool, _ = await self.repair_loop.repair_tool(
                session_id=session.id,
                tool=target_tool,
                failure_result=exec_res,
                tool_purpose=decision.tool_purpose or "utility",
                user_prompt=user_prompt,
            )
            if repaired_tool:
                target_tool = repaired_tool
                log.info("orchestrator_re_executing_after_repair")

                version_number = self.version_manager.create_version(
                    tool_name=tool_name,
                    code=target_tool.code,
                    requirements=target_tool.requirements,
                    manifest=target_tool.manifest.model_dump(),
                    tests=target_tool.tests,
                    readme=target_tool.readme,
                    prompt=user_prompt,
                )

                await self.registry.register(
                    tool=target_tool,
                    code_hash=self.version_manager.diff(
                        tool_name, version_number, version_number
                    )
                    or "repaired",
                    prompt_used=user_prompt,
                )

                self.runtime_manager.setup_workspace(session.id, target_tool)

                tool_input = await self._extract_parameters(
                    user_prompt=user_prompt,
                    input_schema=target_tool.manifest.input_schema
                    if target_tool.manifest
                    else {},
                    agent_name=active_agent.name,
                    session_id=session.id,
                )

                exec_res = await self._execute_tool_cancellable(
                    session_id=session.id,
                    input_data=tool_input,
                    requirements=target_tool.requirements or [],
                    cancellation_event=cancellation_event,
                    sandbox_output_callback=report_sandbox_output,
                    sandbox_operation_callback=report_sandbox_operation,
                )
                
                json_error = None
                if exec_res.return_code == 0 and exec_res.stdout:
                    try:
                        wrapped_out = json.loads(exec_res.stdout.strip())
                        output_val = wrapped_out.get("output")
                        if isinstance(output_val, dict) and "error" in output_val:
                            json_error = str(output_val["error"])
                        elif isinstance(wrapped_out, dict) and "error" in wrapped_out:
                            json_error = str(wrapped_out["error"])
                    except Exception:
                        pass
                        
                if json_error:
                    exec_res.stderr = json_error
                    
                success = exec_res.return_code == 0 and not exec_res.timed_out and not json_error

        output_data = None
        error_msg = None

        if success:
            try:
                wrapped_out = json.loads(exec_res.stdout.strip())
                output_data = str(wrapped_out.get("output"))
            except Exception:
                output_data = exec_res.stdout
        else:
            error_msg = exec_res.stderr or f"Exit code: {exec_res.return_code}"

        # 11. Update database metrics and session run records
        execution_id = await self.registry.update_stats(
            tool_name=tool_name,
            success=success,
            runtime_ms=exec_res.runtime_ms,
            input_data={"prompt": user_prompt},
            output_data={"result": output_data} if success else None,
            error=error_msg,
        )

        session_output = SessionToolOutput(
            tool_name=tool_name,
            version=version_number,
            success=success,
            input_data={"prompt": user_prompt},
            output_data=output_data,
            error=error_msg,
        )
        session.history.append(session_output)

        repair_cost = 0.0
        repair_tokens = 0
        if self.repair_loop and hasattr(self.repair_loop.llm, "total_cost"):
            repair_cost = self.repair_loop.llm.total_cost
            repair_tokens = self.repair_loop.llm.total_input_tokens + self.repair_loop.llm.total_output_tokens
            # Reset repair loop LLM metrics for future runs
            self.repair_loop.llm.total_cost = 0.0
            self.repair_loop.llm.total_input_tokens = 0
            self.repair_loop.llm.total_output_tokens = 0

        # Audit execute
        await self.audit_logger.log_event(
            event_type=AuditEventType.TOOL_EXECUTED,
            session_id=session.id,
            agent_name=active_agent.name,
            details={
                "tool_name": tool_name,
                "success": success,
                "runtime_ms": exec_res.runtime_ms,
            },
            cost=repair_cost,
            token_usage=repair_tokens,
        )

        # Store to long-term memory if successful
        if success and settings.memory.enabled:
            await self.long_term_memory.store(
                key=user_prompt[:100], value=str(output_data)[:200], category="tasks"
            )
            self.short_term_memory.add(
                "assistant", str(output_data), {"tool_name": tool_name}
            )

        # Cleanup sandbox workspace
        if settings.runtime.cleanup_after:
            self.runtime_manager.cleanup_workspace(session.id)

        # Fire AFTER_EXECUTE Hook
        await self.hook_manager.fire(
            HookType.AFTER_EXECUTE, {"result": session_output, "session": session}
        )

        log.info("orchestrator_task_execution_completed", success=success)
        return {
            "status": "completed" if success else "failed",
            "action_taken": "execute_tool",
            "tool_name": tool_name,
            "version": version_number,
            "output": output_data,
            "error": error_msg,
            "runtime_ms": exec_res.runtime_ms,
            "execution_id": execution_id,
            "reasoning": getattr(decision, "reasoning", None),
            "response": getattr(decision, "response", None),
        }
