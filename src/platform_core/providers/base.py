"""Provider base class and registry."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from platform_core.core import HackPrefix, JsonDict
from platform_core.events import DomainEvent, EventBus

logger = logging.getLogger(__name__)


class ProviderBase(ABC):
    """Abstract base for all external-service providers.

    Providers are stateless service facades.  They receive credentials
    and configuration at construction time and expose a consistent
    lifecycle API: validate → provision → reconcile → cleanup.

    Providers MUST NOT call other providers directly.  Cross-provider
    coordination goes through the orchestration engine.
    """

    name: str = "base"

    def __init__(self, *, event_bus: EventBus | None = None) -> None:
        self._event_bus = event_bus
        self._log = logging.getLogger(f"provider.{self.name}")

    # ── Lifecycle methods ────────────────────────────────────────

    @abstractmethod
    async def validate(self) -> bool:
        """Validate credentials and connectivity."""
        ...

    @abstractmethod
    async def provision(
        self,
        desired: JsonDict,
        *,
        dry_run: bool = False,
        on_progress: Any = None,
    ) -> JsonDict:
        """Create or update resources to match desired state."""
        ...

    @abstractmethod
    async def reconcile(self, desired: JsonDict, actual: JsonDict) -> JsonDict:
        """Compare desired vs actual and return a change plan."""
        ...

    @abstractmethod
    async def cleanup(
        self,
        state: JsonDict,
        *,
        dry_run: bool = False,
    ) -> JsonDict:
        """Remove resources described by state."""
        ...

    # ── Optional hooks ───────────────────────────────────────────

    async def preflight(self, desired: JsonDict) -> list[dict[str, Any]]:
        """Run pre-flight checks before provisioning.

        Returns a list of check results:
          [{"check": "license_availability", "passed": True, "detail": "..."}]
        """
        return []

    async def discover(self, prefix: HackPrefix) -> JsonDict:
        """Discover existing resources for a hack prefix."""
        return {"prefix": prefix, "resources": []}

    # ── Helpers ──────────────────────────────────────────────────

    async def _emit(self, event: DomainEvent) -> None:
        if self._event_bus:
            await self._event_bus.publish(event)


class ProviderRegistry:
    """Registry of available providers.

    Allows the orchestration engine to look up providers by name
    without hard-coding dependencies.
    """

    def __init__(self) -> None:
        self._providers: dict[str, ProviderBase] = {}

    def register(self, provider: ProviderBase) -> None:
        self._providers[provider.name] = provider
        logger.info("Registered provider: %s", provider.name)

    def get(self, name: str) -> ProviderBase:
        if name not in self._providers:
            raise KeyError(f"Provider '{name}' not registered")
        return self._providers[name]

    def list(self) -> list[str]:
        return list(self._providers.keys())

    def has(self, name: str) -> bool:
        return name in self._providers

    def __contains__(self, name: str) -> bool:
        return name in self._providers


_registry: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry
