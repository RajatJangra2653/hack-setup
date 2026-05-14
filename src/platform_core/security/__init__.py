"""Security module — secret redaction, safeguards, and validation."""

from __future__ import annotations

import re
from typing import Any


# ── Secret redaction ─────────────────────────────────────────────────

_SECRET_KEYS = frozenset({
    "password", "secret", "token", "key", "credential",
    "tap", "tap_code", "temporaryAccessPass", "client_secret",
    "access_token", "refresh_token",
})

_TOKEN_PATTERN = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def redact_dict(data: dict[str, Any], *, depth: int = 0) -> dict[str, Any]:
    """Deep-redact sensitive values in a dictionary."""
    if depth > 10:
        return data

    result = {}
    for key, value in data.items():
        if any(s in key.lower() for s in _SECRET_KEYS):
            result[key] = "***REDACTED***" if value else ""
        elif isinstance(value, dict):
            result[key] = redact_dict(value, depth=depth + 1)
        elif isinstance(value, list):
            result[key] = [
                redact_dict(v, depth=depth + 1) if isinstance(v, dict) else v
                for v in value
            ]
        elif isinstance(value, str) and _TOKEN_PATTERN.search(value):
            result[key] = "***REDACTED_TOKEN***"
        else:
            result[key] = value
    return result


def redact_state_for_archive(state: dict[str, Any]) -> dict[str, Any]:
    """Redact a full hack state for archival."""
    state = redact_dict(state)
    for user in state.get("users", []):
        if isinstance(user, dict):
            user["password"] = "***REDACTED***" if user.get("password") else ""
            if "tap" in user and isinstance(user["tap"], dict):
                user["tap"]["temporaryAccessPass"] = "***REDACTED***"
            user["tap_code"] = "***REDACTED***" if user.get("tap_code") else ""
    return state


# ── Destructive action safeguards ────────────────────────────────────

class SafeguardError(Exception):
    """Raised when a destructive action fails safeguard checks."""


def require_confirmation(action: str, target: str, *, force: bool = False) -> None:
    """Check that destructive actions are explicitly confirmed.

    In API context, ``force=True`` maps to a request body flag.
    In CLI context, ``force`` comes from --yes / --force flags.
    """
    if not force:
        raise SafeguardError(
            f"Destructive action '{action}' on '{target}' requires explicit confirmation. "
            f"Set force=True or pass --yes flag."
        )


def validate_prefix(prefix: str) -> str:
    """Validate and normalize a hack prefix."""
    prefix = prefix.strip().lower()
    if not prefix:
        raise ValueError("Prefix cannot be empty")
    if not prefix.endswith("-"):
        prefix += "-"
    if not re.match(r"^[a-z0-9][a-z0-9-]*-$", prefix):
        raise ValueError(f"Invalid prefix format: {prefix}")
    return prefix
