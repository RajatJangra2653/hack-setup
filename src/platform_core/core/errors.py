"""Platform-wide exception hierarchy.

Every layer raises domain-specific exceptions that inherit from
PlatformError so callers can catch broadly or narrowly.
"""

from __future__ import annotations

from typing import Any


class PlatformError(Exception):
    """Root exception for all platform errors."""

    def __init__(self, message: str, *, code: str = "PLATFORM_ERROR", details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


# ── Provider errors ─────────────────────────────────────────────────
class ProviderError(PlatformError):
    """An external provider call failed."""

    def __init__(self, provider: str, message: str, *, status: int | None = None, **kw: Any):
        super().__init__(f"[{provider}] {message}", code="PROVIDER_ERROR", **kw)
        self.provider = provider
        self.status = status


class GraphApiError(ProviderError):
    """Microsoft Graph API error."""

    def __init__(self, message: str, *, status: int | None = None, graph_code: str | None = None, **kw: Any):
        super().__init__("graph", message, status=status, **kw)
        self.graph_code = graph_code


class ThrottledError(ProviderError):
    """Provider returned 429 — caller should back off."""

    def __init__(self, provider: str, retry_after: float | None = None):
        super().__init__(provider, "Rate limited", status=429)
        self.retry_after = retry_after


# ── Reconciliation errors ──────────────────────────────────────────
class ReconciliationError(PlatformError):
    """Reconciliation engine failure."""

    def __init__(self, message: str, **kw: Any):
        super().__init__(message, code="RECONCILIATION_ERROR", **kw)


class DriftDetectionError(ReconciliationError):
    """Could not determine drift."""


class PlanExecutionError(ReconciliationError):
    """Plan could not be fully applied."""

    def __init__(self, message: str, *, applied: int = 0, failed: int = 0, **kw: Any):
        super().__init__(message, **kw)
        self.applied = applied
        self.failed = failed


# ── Operation errors ────────────────────────────────────────────────
class OperationError(PlatformError):
    """An operation lifecycle error."""

    def __init__(self, message: str, **kw: Any):
        super().__init__(message, code="OPERATION_ERROR", **kw)


class OperationTimeoutError(OperationError):
    """Operation exceeded its deadline."""


class OperationCancelledError(OperationError):
    """Operation was cancelled by the user or system."""


# ── Storage / repository errors ─────────────────────────────────────
class StorageError(PlatformError):
    """Storage backend failure."""

    def __init__(self, message: str, **kw: Any):
        super().__init__(message, code="STORAGE_ERROR", **kw)


class NotFoundError(StorageError):
    """Requested entity does not exist."""

    def __init__(self, entity: str, identifier: str):
        super().__init__(f"{entity} '{identifier}' not found", code="NOT_FOUND")
        self.entity = entity
        self.identifier = identifier


class ConflictError(StorageError):
    """Entity already exists or version conflict."""

    def __init__(self, message: str):
        super().__init__(message, code="CONFLICT")


# ── Auth / security errors ──────────────────────────────────────────
class AuthError(PlatformError):
    """Authentication or authorization failure."""

    def __init__(self, message: str, **kw: Any):
        super().__init__(message, code="AUTH_ERROR", **kw)


class CredentialError(AuthError):
    """Credentials missing, invalid, or expired."""


class PermissionDeniedError(AuthError):
    """Caller lacks the required permission."""


# ── Validation errors ───────────────────────────────────────────────
class ValidationError(PlatformError):
    """Input validation failed."""

    def __init__(self, message: str, *, field: str | None = None, **kw: Any):
        super().__init__(message, code="VALIDATION_ERROR", **kw)
        self.field = field


# ── Configuration errors ────────────────────────────────────────────
class ConfigError(PlatformError):
    """Configuration is missing or invalid."""

    def __init__(self, message: str, **kw: Any):
        super().__init__(message, code="CONFIG_ERROR", **kw)
