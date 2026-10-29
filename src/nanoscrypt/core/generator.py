import json
from typing import Any

import structlog

from nanoscrypt.llm.base import LLMProvider
from nanoscrypt.llm.prompts.generator import (
    TOOL_GENERATION_SYSTEM_PROMPT,
    TOOL_GENERATION_USER_TEMPLATE,
)
from nanoscrypt.models.plan import PlannerDecision
from nanoscrypt.models.tool import GeneratedTool

logger = structlog.get_logger()


class ToolGenerator:
    """Invokes the LLM to synthesize a complete tool package (code, tests, readme, manifest, requirements)."""

    def __init__(self, llm: LLMProvider):
        self.llm = llm

    async def generate(self, decision: PlannerDecision, user_prompt: str = "", **kwargs: Any) -> GeneratedTool:
        """Takes a PlannerDecision specification and returns a fully synthesized GeneratedTool package."""
        log = logger.bind(component="tool_generator", tool_name=decision.tool_name)
        log.info("tool_generation_started", purpose=decision.tool_purpose)

        # Build user prompt
        gen_prompt = TOOL_GENERATION_USER_TEMPLATE.format(
            tool_name=decision.tool_name or "unnamed_tool",
            tool_purpose=decision.tool_purpose or "no purpose specified",
            user_request=user_prompt or "No additional context provided",
            input_description=decision.input_description or "any",
            output_description=decision.output_description or "any",
            dependencies_hint=", ".join(decision.dependencies_hint)
            if decision.dependencies_hint
            else "none",
        )

        try:
            # We call the structured generation using the GeneratedTool model
            generated_tool = await self.llm.generate_structured(
                prompt=gen_prompt,
                response_model=GeneratedTool,
                system_prompt=TOOL_GENERATION_SYSTEM_PROMPT,
                **kwargs,
            )

            # A parser fallback such as "unnamed_tool" is truthy, so treat it
            # as missing and preserve the planner's requested tool name.
            if decision.tool_name and generated_tool.name.strip().lower() in {"", "unnamed_tool", "tool"}:
                generated_tool.name = decision.tool_name
                generated_tool.manifest.name = decision.tool_name

            log.info(
                "tool_generation_completed",
                lines_of_code=len(generated_tool.code.splitlines()) if generated_tool.code else 0,
                requirements_count=len(generated_tool.requirements),
            )
            return generated_tool
        except Exception as e:
            log.error("tool_generation_failed", error=str(e))
            raise
