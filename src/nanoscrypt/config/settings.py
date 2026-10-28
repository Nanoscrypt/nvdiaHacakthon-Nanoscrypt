from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    import tomli as tomllib
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseModel):
    provider: str = "ollama"
    model: str = "ollama/qwen2.5-coder"
    api_key: str | None = None
    api_base: str | None = None
    temperature: float = 0.2
    max_tokens: int = 131072  # Qwen 2.5 Coder 128K max
    max_output_tokens: int = 4096  # Max generation length (streamlined for local Ollama)
    retry_on_failure: bool = True
    max_retries: int = 3
    token_budget: int = 0  # 0 = unlimited


class RuntimeSettings(BaseModel):
    timeout_seconds: int = 90
    max_agent_turns: int = 12
    max_memory_mb: int = 512
    cleanup_after: bool = True
    workspace_root: str = "./workspaces"
    venv_cache_dir: str = "./venv_cache"  # For caching virtual environments
    use_venv: bool = False  # Set to False to run directly with sys.executable (no isolated venv)
    capsem_enabled: bool = False
    code_agent_enabled: bool = False
    execution_backend: str = "local"  # local or nebius_sandbox
    nebius_sandbox_image: str = "python:3.12-slim"


class RegistrySettings(BaseModel):
    database_url: str = "sqlite+aiosqlite:///./registry/tools.db"
    tools_dir: str = "./generated_tools"


class SecuritySettings(BaseModel):
    approval_mode: str = "interactive"  # interactive, auto, webhook
    default_risk_threshold: str = (
        "medium"  # auto-approve below this (low, medium, high, critical)
    )
    blocked_domains: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    max_file_write_mb: int = 10
    pii_detection: bool = False


class MemorySettings(BaseModel):
    enabled: bool = True
    short_term_max_entries: int = 50
    long_term_enabled: bool = True
    entity_tracking: bool = True


class ResilienceSettings(BaseModel):
    max_repair_attempts: int = 5
    retry_delay_seconds: float = 1.0
    exponential_backoff: bool = True
    circuit_breaker_threshold: int = 5
    fallback_model: str | None = None


class LoggingSettings(BaseModel):
    level: str = "INFO"
    json_output: bool = False


class HeadroomSettings(BaseModel):
    enabled: bool = True
    smart_crusher: bool = True
    code_compression: bool = True
    max_tool_output_tokens: int = 1500


class MemMachineSettings(BaseModel):
    enabled: bool = True
    base_url: str = "http://localhost:8080"
    project_id: str = "nanoscrypt"
    fallback_to_sqlite: bool = True


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NANOSCRYPT_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    nebius_api_key: str | None = Field(default=None, validation_alias="NEBIUS_API_KEY")
    nebius_project_id: str | None = Field(
        default=None, validation_alias="NEBIUS_PROJECT_ID"
    )
    tavily_api_key: str | None = Field(default=None, validation_alias="TAVILY_API_KEY")
    llm: LLMSettings = Field(default_factory=LLMSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    registry: RegistrySettings = Field(default_factory=RegistrySettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    resilience: ResilienceSettings = Field(default_factory=ResilienceSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    headroom: HeadroomSettings = Field(default_factory=HeadroomSettings)
    memmachine: MemMachineSettings = Field(default_factory=MemMachineSettings)


def load_settings(config_path: Path | str | None = None) -> Settings:
    """Loads configuration settings merging user root ~/.nanoscrypt/config.toml,
    project-level nanoscrypt.toml, and environment variables."""
    from nanoscrypt.config.user_config import apply_global_env, load_global_config

    # Apply global environment variables from ~/.nanoscrypt/config.toml
    apply_global_env()

    toml_data: dict[str, Any] = {}

    # 1. Load project-level config (nanoscrypt.toml) as base
    if config_path is None:
        local_path = Path("nanoscrypt.toml")
    else:
        local_path = Path(config_path)

    if local_path.exists():
        try:
            with open(local_path, "rb") as f:
                local_data = tomllib.load(f)
                for k, v in local_data.items():
                    toml_data[k] = v
        except Exception as e:
            import logging

            logging.warning(
                f"Failed to parse TOML configuration from {local_path}: {e}"
            )

    # 2. Merge user root config (~/.nanoscrypt/config.toml) with high priority
    global_cfg = load_global_config()
    if global_cfg:
        for k, v in global_cfg.items():
            if isinstance(v, dict) and isinstance(toml_data.get(k), dict):
                toml_data[k].update(v)
            else:
                toml_data[k] = v

    return Settings(**toml_data)


settings = load_settings()


def reload_settings(config_path: Path | str | None = None) -> Settings:
    """Re-reads global and local configuration files and updates the global settings instance."""
    global settings
    new_settings = load_settings(config_path)
    # Mutate in-place so existing module references get updated
    for field in settings.model_fields.keys():
        setattr(settings, field, getattr(new_settings, field))
    return settings
