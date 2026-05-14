"""Provider abstraction layer.

Every external service integration implements the Provider protocol
and registers with the ProviderRegistry.
"""

from platform_core.providers.base import ProviderBase, ProviderRegistry, get_registry

__all__ = ["ProviderBase", "ProviderRegistry", "get_registry"]
