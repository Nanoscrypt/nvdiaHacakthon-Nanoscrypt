from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from nanoscrypt.config.settings import settings
from nanoscrypt.llm.litellm_provider import LiteLLMProvider


class StructuredAnswer(BaseModel):
    answer: str


@pytest.mark.asyncio
async def test_generate_structured_passes_configured_api_credentials(monkeypatch):
    monkeypatch.setattr(settings.llm, "api_key", "test-nvidia-api-key")
    monkeypatch.setattr(settings.llm, "api_base", "https://example.invalid/v1")
    request_kwargs = {}

    async def fake_acompletion(**kwargs):
        request_kwargs.update(kwargs)
        return SimpleNamespace(
            usage=None,
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"answer":"ok"}')
                )
            ],
        )

    monkeypatch.setattr(
        "nanoscrypt.llm.litellm_provider.litellm.acompletion",
        fake_acompletion,
    )

    result = await LiteLLMProvider("nvidia_nim/example/model").generate_structured(
        "Return a structured answer", StructuredAnswer
    )

    assert result == StructuredAnswer(answer="ok")
    assert request_kwargs["api_key"] == "test-nvidia-api-key"
    assert request_kwargs["api_base"] == "https://example.invalid/v1"


def test_nebius_environment_key_selects_token_factory_endpoint(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "test-nebius-key")
    monkeypatch.setattr(settings.llm, "api_key", None)
    monkeypatch.setattr(settings.llm, "api_base", None)
    monkeypatch.setattr(settings, "nebius_api_key", None)
    request_kwargs = {}

    LiteLLMProvider._inject_llm_credentials(
        request_kwargs, "nebius/zai-org/GLM-5.2"
    )

    assert request_kwargs["api_key"] == "test-nebius-key"
    assert request_kwargs["api_base"] == "https://api.tokenfactory.nebius.com/v1"


def test_nebius_key_does_not_override_non_nebius_model_endpoint(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "test-nebius-key")
    monkeypatch.setattr(settings.llm, "api_key", None)
    monkeypatch.setattr(settings.llm, "api_base", None)
    monkeypatch.setattr(settings, "nebius_api_key", None)
    request_kwargs = {}

    LiteLLMProvider._inject_llm_credentials(request_kwargs, "gpt-4o-mini")

    assert request_kwargs["api_key"] == "test-nebius-key"
    assert "api_base" not in request_kwargs
