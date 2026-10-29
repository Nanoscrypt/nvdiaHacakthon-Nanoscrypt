import asyncio
import inspect
import json
import os
import re
from typing import Any

import litellm
import structlog
from pydantic import BaseModel

from nanoscrypt.config.settings import settings
from nanoscrypt.llm.base import LLMProvider
from nanoscrypt.models.tool import GeneratedTool, ToolManifest

logger = structlog.get_logger()


class LiteLLMProvider(LLMProvider):
    """Upgraded LiteLLM implementation supporting cost tracking, token counters, retries, and fallback models."""

    def __init__(
        self, default_model: str, temperature: float = 0.2, max_tokens: int = 131072
    ):
        self.default_model = default_model
        self.temperature = temperature
        self.max_tokens = max_tokens

        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost = 0.0
        self.last_input_tokens = 0
        self.last_output_tokens = 0
        self.last_cost = 0.0

    def count_tokens(self, text: str, model: str | None = None) -> int:
        """Statically counts tokens for a string of text."""
        target_model = model or self.default_model
        try:
            return litellm.token_counter(model=target_model, text=text)
        except Exception:
            # Fallback estimation: ~4 characters per token
            return len(text) // 4

    def estimate_cost(
        self, input_tokens: int, output_tokens: int, model: str | None = None
    ) -> float:
        """Estimates model invocation costs in USD."""
        target_model = model or self.default_model
        try:
            # Get pricing dict from LiteLLM database
            pricing = litellm.get_model_info(target_model)
            input_cost = (
                pricing.get("input_cost_per_token", 0.0) or 0.0
            ) * input_tokens
            output_cost = (
                pricing.get("output_cost_per_token", 0.0) or 0.0
            ) * output_tokens
            return float(input_cost + output_cost)
        except Exception:
            return 0.0

    @staticmethod
    def _inject_llm_credentials(kwargs: dict[str, Any], model: str) -> None:
        # Token Factory uses an OpenAI-compatible endpoint. Keep its key separate
        # from provider-specific credentials so a Nebius key is never sent to a
        # different provider by accident.
        nebius_key = os.getenv("NEBIUS_API_KEY") or settings.nebius_api_key
        if "api_key" not in kwargs:
            api_key = settings.llm.api_key or nebius_key
            if api_key:
                kwargs["api_key"] = api_key

        if "api_base" not in kwargs:
            api_base = settings.llm.api_base
            if not api_base and nebius_key and model.startswith(("nebius/", "openai/")):
                api_base = "https://api.tokenfactory.nebius.com/v1"
            if api_base:
                kwargs["api_base"] = api_base

    async def _execute_with_retry(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Executes LiteLLM completion calls with automatic backoff and fallback model rotation."""
        model = kwargs.get("model", self.default_model)
        retries = settings.llm.max_retries
        backoff = settings.resilience.retry_delay_seconds

        fallback_model = settings.resilience.fallback_model
        attempt = 0

        while attempt <= retries:
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                attempt += 1
                logger.warning(
                    "llm_completion_attempt_failed",
                    attempt=attempt,
                    model=model,
                    error=str(e),
                )
                if attempt > retries:
                    if fallback_model and model != fallback_model:
                        logger.info(
                            "llm_completion_switching_to_fallback",
                            fallback=fallback_model,
                        )
                        kwargs["model"] = fallback_model
                        # Reset attempts for fallback model
                        attempt = 0
                        model = fallback_model
                        continue
                    raise
                await asyncio.sleep(backoff * (2 ** (attempt - 1)))

    async def generate(
        self, prompt: str, system_prompt: str | None = None, **kwargs: Any
    ) -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # Inject default options
        model = kwargs.pop("model", self.default_model)
        temp = kwargs.pop("temperature", self.temperature)

        # Enterprise-grade distinction: max context window vs max output tokens
        tokens = kwargs.pop("max_tokens", settings.llm.max_output_tokens)

        # Execute completion inside the retry context
        if "timeout" not in kwargs:
            kwargs["timeout"] = 1800.0
        self._inject_llm_credentials(kwargs, model)

        # Only stream user-facing text when a callback is explicitly provided.
        # Internal planner/generator calls continue to receive a normal response.
        stream_callback = kwargs.pop("stream_callback", None)
        if stream_callback is not None:
            response_stream = await self._execute_with_retry(
                litellm.acompletion,
                model=model,
                messages=messages,
                temperature=temp,
                max_tokens=tokens,
                stream=True,
                **kwargs,
            )
            response_parts: list[str] = []
            usage = None
            async for chunk in response_stream:
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                content = getattr(delta, "content", None) if delta else None
                if not isinstance(content, str) or not content:
                    continue
                response_parts.append(content)
                callback_result = stream_callback(content)
                if inspect.isawaitable(callback_result):
                    await callback_result

            response_text = "".join(response_parts)
            self.last_input_tokens = (
                usage.prompt_tokens if usage and usage.prompt_tokens is not None
                else self.count_tokens(prompt, model)
            )
            self.last_output_tokens = (
                usage.completion_tokens if usage and usage.completion_tokens is not None
                else self.count_tokens(response_text, model)
            )
            self.total_input_tokens += self.last_input_tokens
            self.total_output_tokens += self.last_output_tokens
            self.last_cost = self.estimate_cost(
                self.last_input_tokens, self.last_output_tokens, model
            )
            self.total_cost += self.last_cost
            return response_text

        response = await self._execute_with_retry(
            litellm.acompletion,
            model=model,
            messages=messages,
            temperature=temp,
            max_tokens=tokens,
            **kwargs,
        )

        # Track metrics
        try:
            usage = response.usage
            self.last_input_tokens = usage.prompt_tokens
            self.last_output_tokens = usage.completion_tokens
            self.total_input_tokens += usage.prompt_tokens
            self.total_output_tokens += usage.completion_tokens

            # LiteLLM cost calculation helper
            cost = litellm.completion_cost(completion_response=response) or 0.0
            self.last_cost = float(cost)
            self.total_cost += float(cost)
        except Exception:
            self.last_input_tokens = 0
            self.last_output_tokens = 0
            self.last_cost = 0.0

        return response.choices[0].message.content or ""

    async def generate_structured(
        self,
        prompt: str,
        response_model: Any,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Calls the LLM and forces a structured output using Pydantic schemas, with retry and fallback support."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        model = kwargs.pop("model", self.default_model)
        temp = kwargs.pop("temperature", self.temperature)
        tokens = kwargs.pop("max_tokens", settings.llm.max_output_tokens)

        if response_model == GeneratedTool:
            raw_response = await self.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                model=model,
                temperature=temp,
                max_tokens=tokens,
                **kwargs
            )

            def strip_markdown_code_block(text: str) -> str:
                text = text.strip()
                if text.startswith("```"):
                    newline_idx = text.find("\n")
                    if newline_idx != -1:
                        text = text[newline_idx + 1 :].strip()
                    else:
                        text = text[3:].strip()
                if text.endswith("```"):
                    text = text[:-3].strip()
                return text.strip()

            def extract_tag(tag: str) -> str:
                pattern = rf"<\s*{re.escape(tag)}\b[^>]*>(.*?)</\s*{re.escape(tag)}\s*>"
                matches = list(re.finditer(pattern, raw_response, re.IGNORECASE | re.DOTALL))
                return matches[-1].group(1).strip() if matches else ""

            # Models sometimes return the requested fields as JSON despite the XML
            # contract. Parse that form before falling back to code fences.
            json_payload = None
            json_candidates = [raw_response.strip()]
            json_candidates.extend(
                match.group(1).strip()
                for match in re.finditer(r"```(?:json)?\s*(.*?)```", raw_response, re.IGNORECASE | re.DOTALL)
            )
            for candidate in json_candidates:
                candidate = strip_markdown_code_block(candidate)
                try:
                    parsed = json.loads(candidate)
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, dict):
                    json_payload = parsed
                    break

            def json_field(*names: str, default: Any = "") -> Any:
                if not isinstance(json_payload, dict):
                    return default
                for field_name in names:
                    value = json_payload.get(field_name)
                    if value is not None:
                        return value
                return default

            name = strip_markdown_code_block(extract_tag("tool_name")) or str(
                json_field("tool_name", "name", default="")
            ).strip()
            code = strip_markdown_code_block(extract_tag("code")) or str(
                json_field("code", "source", "implementation", default="")
            )
            requirements_value = json_field("requirements", default=None)
            requirements_raw = strip_markdown_code_block(extract_tag("requirements"))
            if requirements_value is not None:
                if isinstance(requirements_value, list):
                    requirements = [str(item).strip() for item in requirements_value if str(item).strip()]
                else:
                    requirements = [line.strip() for line in str(requirements_value).splitlines() if line.strip()]
            else:
                requirements = [line.strip() for line in requirements_raw.splitlines() if line.strip()]

            manifest_value = json_field("manifest", default=None)
            manifest_raw = extract_tag("manifest")
            manifest = None
            try:
                if isinstance(manifest_value, dict):
                    manifest = ToolManifest(**manifest_value)
                elif manifest_raw:
                    manifest = ToolManifest(**json.loads(manifest_raw))
                elif isinstance(manifest_value, str) and manifest_value.strip():
                    manifest = ToolManifest(**json.loads(manifest_value))
            except (TypeError, ValueError):
                manifest = None

            tests = strip_markdown_code_block(extract_tag("tests")) or str(
                json_field("tests", default="")
            )
            readme = strip_markdown_code_block(extract_tag("readme")) or str(
                json_field("readme", default="")
            )

            if not code:
                fenced_blocks = [
                    match.group(1).strip()
                    for match in re.finditer(r"```(?:[\w+-]+)?\s*(.*?)```", raw_response, re.DOTALL)
                ]
                run_pattern = re.compile(r"(?m)^\s*(?:async\s+)?def\s+run\s*\(")
                code = next((block for block in fenced_blocks if run_pattern.search(block)), "")
                if not code:
                    # Accept a response that is itself Python source, but never treat
                    # surrounding XML/JSON prose as executable code.
                    plain_source = raw_response.strip()
                    if run_pattern.search(plain_source) and not re.search(r"</?\w+[^>]*>", plain_source):
                        code = plain_source

            if not name:
                name = str(json_field("tool_name", "name", default="unnamed_tool")).strip() or "unnamed_tool"
            if not manifest:
                manifest = ToolManifest(
                    name=name,
                    dependencies=requirements,
                    input_schema={},
                    output_schema={},
                    network=any(item.split("=", 1)[0].lower() in {"requests", "httpx"} for item in requirements),
                )

            has_run = bool(re.search(r"(?m)^\s*(?:async\s+)?def\s+run\s*\(", code))
            if not code or not has_run:
                logger.warning(
                    "generated_tool_response_contract_incomplete",
                    tool_name=name,
                    code_found=bool(code),
                    run_found=has_run,
                    response_chars=len(raw_response),
                    xml_code_tag_found=bool(extract_tag("code")),
                    json_object_found=isinstance(json_payload, dict),
                )

            return GeneratedTool(
                name=name,
                code=code,
                requirements=requirements,
                manifest=manifest,
                tests=tests,
                readme=readme,
            )

        if "timeout" not in kwargs:
            kwargs["timeout"] = 1800.0
        self._inject_llm_credentials(kwargs, model)
        response = await self._execute_with_retry(
            litellm.acompletion,
            model=model,
            messages=messages,
            temperature=temp,
            max_tokens=tokens,
            response_format=response_model,
            **kwargs,
        )

        # Track metrics
        try:
            usage = getattr(response, "usage", None)
            if usage and getattr(usage, "prompt_tokens", None) is not None:
                self.last_input_tokens = usage.prompt_tokens
                self.last_output_tokens = usage.completion_tokens or 0
            else:
                raw_text = response.choices[0].message.content or ""
                self.last_input_tokens = self.count_tokens(prompt)
                self.last_output_tokens = self.count_tokens(raw_text)

            self.total_input_tokens += self.last_input_tokens
            self.total_output_tokens += self.last_output_tokens
            cost = litellm.completion_cost(completion_response=response) or 0.0
            self.last_cost = float(cost)
            self.total_cost += float(cost)
        except Exception:
            self.last_input_tokens = self.count_tokens(prompt)
            self.last_output_tokens = self.count_tokens(str(response))
            self.total_input_tokens += self.last_input_tokens
            self.total_output_tokens += self.last_output_tokens
            self.last_cost = 0.0

        raw_content = response.choices[0].message.content or ""
        if not raw_content and issubclass(response_model, BaseModel):
            # Fallback: prompt for JSON without response_format if model returns empty response
            json_prompt = f"{prompt}\n\nIMPORTANT: Respond with ONLY a valid JSON object matching schema: {response_model.model_json_schema()}"
            raw_content = await self.generate(
                prompt=json_prompt,
                system_prompt=system_prompt,
                model=model,
                temperature=temp,
                max_tokens=tokens,
                **kwargs
            )
            # Clean markdown code blocks if present
            raw_content = raw_content.strip()
            if raw_content.startswith("```"):
                lines = raw_content.splitlines()
                if len(lines) >= 2 and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                raw_content = "\n".join(lines).strip()

        logger.info("llm_raw_structured_response", raw_content=raw_content)
        if issubclass(response_model, BaseModel):
            return response_model.model_validate_json(raw_content)
        return raw_content
