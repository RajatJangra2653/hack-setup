"""Abstract interfaces (protocols) that define the contracts every
subsystem must implement.

Using Protocol instead of ABC so implementations don't need to
inherit — duck-typing with static-checker support.
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence, TypeVar, runtime_checkable
from uuid import UUID

from platform_core.core import HackPrefix, JsonDict

T = TypeVar("T")


# ═══════════════════════════════════════════════════════════════════
# Provider contracts
# ═══════════════════════════════════════════════════════════════════

@runtime_checkable
class TokenProvider(Protocol):
    """Supplies bearer tokens for a given scope."""

    async def get_token(self, scopes: Sequence[str] | None = None) -> str: ...


@runtime_checkable
class Provider(Protocol):
    """Lifecycle contract that every external-service provider implements."""

    name: str

    async def validate(self) -> bool:
        """Validate credentials / connectivity."""
        ...

    async def provision(self, desired: JsonDict, *, dry_run: bool = False) -> JsonDict:
        """Create or update resources to match *desired* state."""
        ...

    async def reconcile(self, desired: JsonDict, actual: JsonDict) -> JsonDict:
        """Compare desired vs actual and return a change plan."""
        ...

    async def cleanup(self, state: JsonDict, *, dry_run: bool = False) -> JsonDict:
        """Remove resources described by *state*."""
        ...


# ═══════════════════════════════════════════════════════════════════
# Repository contracts
# ═══════════════════════════════════════════════════════════════════

@runtime_checkable
class Repository(Protocol[T]):
    """Generic CRUD repository."""

    async def get(self, id: str | UUID) -> T | None: ...
    async def list(self, **filters: Any) -> list[T]: ...
    async def create(self, entity: T) -> T: ...
    async def update(self, entity: T) -> T: ...
    async def delete(self, id: str | UUID) -> bool: ...


# ═══════════════════════════════════════════════════════════════════
# Event contracts
# ═══════════════════════════════════════════════════════════════════

@runtime_checkable
class Event(Protocol):
    """Marker for domain events."""

    event_type: str
    timestamp: str


@runtime_checkable
class EventPublisher(Protocol):
    """Publishes domain events."""

    async def publish(self, event: Event) -> None: ...


@runtime_checkable
class EventSubscriber(Protocol):
    """Handles domain events."""

    async def handle(self, event: Event) -> None: ...


# ═══════════════════════════════════════════════════════════════════
# Audit contracts
# ═══════════════════════════════════════════════════════════════════

@runtime_checkable
class AuditSink(Protocol):
    """Persists audit events."""

    async def log(
        self,
        prefix: HackPrefix,
        event_type: str,
        *,
        actor: str = "system",
        severity: str = "info",
        details: JsonDict | None = None,
    ) -> None: ...

    async def query(
        self,
        prefix: HackPrefix,
        *,
        event_type: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[JsonDict]: ...


# ═══════════════════════════════════════════════════════════════════
# Reconciliation contracts
# ═══════════════════════════════════════════════════════════════════

@runtime_checkable
class ReconciliationEngine(Protocol):
    """Compares desired vs actual state and produces a plan."""

    async def detect_drift(self, prefix: HackPrefix) -> JsonDict: ...

    async def plan(self, prefix: HackPrefix, *, dry_run: bool = True) -> JsonDict: ...

    async def apply(self, plan: JsonDict) -> JsonDict: ...


# ═══════════════════════════════════════════════════════════════════
# Operation tracker contract
# ═══════════════════════════════════════════════════════════════════

@runtime_checkable
class OperationTracker(Protocol):
    """Tracks lifecycle of long-running operations."""

    def start(
        self, prefix: HackPrefix, op_type: str, *, actor: str = "system"
    ) -> Any: ...

    def finish(self, operation: Any, *, result: JsonDict | None = None) -> None: ...

    def fail(self, operation: Any, error: str) -> None: ...

    async def get_history(self, prefix: HackPrefix) -> list[JsonDict]: ...

    async def get_active(self) -> list[JsonDict]: ...
