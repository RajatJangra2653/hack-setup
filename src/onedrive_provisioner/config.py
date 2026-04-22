"""Configuration loading: YAML + environment variables (env wins)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator


class AzureConfig(BaseModel):
    tenant_id: str
    client_id: str
    client_secret: Optional[str] = None
    cert_path: Optional[str] = None
    cert_thumbprint: Optional[str] = None

    @model_validator(mode="after")
    def _check_auth(self) -> "AzureConfig":
        has_secret = bool(self.client_secret)
        has_cert = bool(self.cert_path and self.cert_thumbprint)
        if not (has_secret or has_cert):
            raise ValueError(
                "Azure auth not configured: provide either client_secret or "
                "(cert_path AND cert_thumbprint)."
            )
        return self


class UploadConfig(BaseModel):
    source: str = "./payload"
    destination: str = ""
    chunk_size_mb: int = Field(default=10, ge=1, le=60)
    large_file_threshold_mb: int = Field(default=4, ge=1, le=60)


class ExecutionConfig(BaseModel):
    concurrency: int = Field(default=8, ge=1, le=64)
    max_retries: int = Field(default=6, ge=0, le=12)
    dry_run: bool = False


class UsersConfig(BaseModel):
    list: List[str] = Field(default_factory=list)
    all_users: bool = False


class ReportingConfig(BaseModel):
    output_dir: str = "./reports"
    formats: List[Literal["json", "csv"]] = Field(default_factory=lambda: ["json"])


class AppConfig(BaseModel):
    azure: AzureConfig
    upload: UploadConfig = Field(default_factory=UploadConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    users: UsersConfig = Field(default_factory=UsersConfig)
    reporting: ReportingConfig = Field(default_factory=ReportingConfig)
    log_level: str = "INFO"


_ENV_OVERRIDES = {
    ("azure", "tenant_id"): "AZURE_TENANT_ID",
    ("azure", "client_id"): "AZURE_CLIENT_ID",
    ("azure", "client_secret"): "AZURE_CLIENT_SECRET",
    ("azure", "cert_path"): "AZURE_CERT_PATH",
    ("azure", "cert_thumbprint"): "AZURE_CERT_THUMBPRINT",
    ("execution", "concurrency"): "ONEDRIVE_CONCURRENCY",
    ("execution", "max_retries"): "ONEDRIVE_MAX_RETRIES",
    ("execution", "dry_run"): "ONEDRIVE_DRY_RUN",
    ("upload", "chunk_size_mb"): "ONEDRIVE_CHUNK_SIZE_MB",
    ("upload", "large_file_threshold_mb"): "ONEDRIVE_LARGE_FILE_THRESHOLD_MB",
    ("log_level", None): "ONEDRIVE_LOG_LEVEL",
}


def _coerce(value: str) -> object:
    low = value.lower()
    if low in {"true", "false"}:
        return low == "true"
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def _apply_env(data: dict) -> dict:
    for (section, key), env_name in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        if key is None:
            data[section] = _coerce(raw)
        else:
            data.setdefault(section, {})[key] = _coerce(raw)
    return data


def load_config(path: Optional[str | Path] = None) -> AppConfig:
    """Load YAML config (optional) and overlay environment variables."""
    data: dict = {}
    if path:
        p = Path(path)
        if p.exists():
            with p.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
    # If no YAML, allow building purely from env (azure section required)
    data.setdefault("azure", {})
    _apply_env(data)
    return AppConfig.model_validate(data)
