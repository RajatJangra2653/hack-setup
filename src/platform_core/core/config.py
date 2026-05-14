"""Layered configuration system.

Resolution order (later wins):
  1. Built-in defaults
  2. YAML config file
  3. Environment variables
  4. Runtime overrides

All config models use Pydantic v2 BaseSettings so env vars are
auto-resolved, and .model_validate() accepts YAML-parsed dicts.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── Azure / SPN credentials ─────────────────────────────────────────

class AzureAuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AZURE_")

    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    cert_path: str = ""
    cert_thumbprint: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.tenant_id and self.client_id and (self.client_secret or self.cert_path))


# ── Storage ─────────────────────────────────────────────────────────

class StorageSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLATFORM_STORAGE_")

    backend: str = Field("blob", description="blob | postgres | sqlite")
    blob_connection_string: str = Field("", alias="AZURE_STORAGE_CONNECTION_STRING")
    blob_container: str = "hack-provisioning-data"
    database_url: str = Field("sqlite+aiosqlite:///platform.db", description="SQLAlchemy async URL")


# ── Graph API ───────────────────────────────────────────────────────

class GraphSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GRAPH_")

    base_url: str = "https://graph.microsoft.com/v1.0"
    timeout: int = 120
    max_retries: int = 6
    concurrency: int = 8


# ── Execution defaults ──────────────────────────────────────────────

class ExecutionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLATFORM_")

    concurrency: int = Field(8, ge=1, le=64)
    max_retries: int = Field(6, ge=0, le=12)
    dry_run: bool = False
    operation_timeout: int = Field(3600, description="Max seconds for a single operation")


# ── Scheduler ───────────────────────────────────────────────────────

class SchedulerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLATFORM_SCHEDULER_")

    enabled: bool = True
    poll_interval: int = Field(60, description="Seconds between scheduler ticks")
    auto_cleanup_after_days: int = Field(30, description="Days after hack date to auto-cleanup")


# ── Telemetry ───────────────────────────────────────────────────────

class TelemetrySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLATFORM_TELEMETRY_")

    log_level: str = "INFO"
    json_logs: bool = True
    app_insights_key: str = ""
    enable_tracing: bool = False


# ── API server ──────────────────────────────────────────────────────

class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLATFORM_API_")

    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 2
    allowed_origins: list[str] = Field(default_factory=lambda: ["*"])
    auth_required: bool = True
    allowed_domains: list[str] = Field(
        default_factory=lambda: ["spektrasystems.com"]
    )


# ── GitHub EMU ──────────────────────────────────────────────────────

class GitHubSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GITHUB_")

    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    emu_group_id: str = ""


# ── OpenAI / Chatbot ───────────────────────────────────────────────

class AiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AZURE_OPENAI_")

    endpoint: str = ""
    key: str = ""
    deployment: str = "gpt-4o"
    api_version: str = "2024-12-01-preview"


# ═══════════════════════════════════════════════════════════════════
# Root configuration aggregate
# ═══════════════════════════════════════════════════════════════════

class PlatformConfig(BaseSettings):
    """Top-level configuration that aggregates all subsystem settings."""

    model_config = SettingsConfigDict(env_prefix="PLATFORM_")

    azure: AzureAuthSettings = Field(default_factory=AzureAuthSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    graph: GraphSettings = Field(default_factory=GraphSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    ai: AiSettings = Field(default_factory=AiSettings)

    @model_validator(mode="before")
    @classmethod
    def _merge_yaml(cls, values: Any) -> Any:
        """If a YAML path is provided, load and merge it under defaults."""
        yaml_path = os.environ.get("PLATFORM_CONFIG_FILE", "config.yaml")
        p = Path(yaml_path)
        if p.is_file():
            with p.open() as f:
                file_data = yaml.safe_load(f) or {}
            # Merge: file values are base; env-derived values override
            for key, val in file_data.items():
                if key not in values or values[key] is None:
                    values[key] = val
        return values


@lru_cache(maxsize=1)
def get_config() -> PlatformConfig:
    """Singleton accessor. Call ``get_config.cache_clear()`` to reload."""
    return PlatformConfig()
