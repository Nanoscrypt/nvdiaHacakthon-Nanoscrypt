"""Make one real inference call through Nebius Token Factory."""

import asyncio
import os

from nanoscrypt.config.settings import settings
from nanoscrypt.llm.litellm_provider import LiteLLMProvider


async def main() -> None:
    if not settings.nebius_api_key and not os.getenv("NEBIUS_API_KEY") and not settings.llm.api_key:
        raise SystemExit("Set NEBIUS_API_KEY (or NANOSCRYPT_LLM__API_KEY) first.")
    model = settings.llm.model
    if not model.startswith("nebius/"):
        raise SystemExit(
            "Set NANOSCRYPT_LLM__MODEL to nebius/<exact-model-id-from-your-catalog>."
        )
    provider = LiteLLMProvider(model)
    answer = await provider.generate(
        "Reply with exactly: Nebius Token Factory connection works."
    )
    print(f"Model: {model}")
    print(answer)
    print(
        f"Tokens: input={provider.last_input_tokens}, "
        f"output={provider.last_output_tokens}"
    )


if __name__ == "__main__":
    asyncio.run(main())
