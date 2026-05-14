"""Abstract repository interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar
from uuid import UUID

T = TypeVar("T")


class BaseRepository(ABC, Generic[T]):
    """Abstract repository providing CRUD operations."""

    @abstractmethod
    async def get(self, id: str | UUID) -> T | None: ...

    @abstractmethod
    async def list(self, *, limit: int = 100, offset: int = 0, **filters: Any) -> list[T]: ...

    @abstractmethod
    async def create(self, entity: T) -> T: ...

    @abstractmethod
    async def update(self, entity: T) -> T: ...

    @abstractmethod
    async def delete(self, id: str | UUID) -> bool: ...

    @abstractmethod
    async def count(self, **filters: Any) -> int: ...


class HackRepository(BaseRepository["Any"]):
    """Repository for HackEnvironment entities."""

    @abstractmethod
    async def get_by_prefix(self, prefix: str) -> Any | None: ...

    @abstractmethod
    async def list_active(self) -> list[Any]: ...

    @abstractmethod
    async def list_archived(self) -> list[Any]: ...

    @abstractmethod
    async def archive(self, prefix: str, *, reason: str = "cleanup") -> bool: ...


class AuditRepository(BaseRepository["Any"]):
    """Repository for audit events."""

    @abstractmethod
    async def query(
        self,
        prefix: str,
        *,
        event_type: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[Any]: ...


class OperationRepository(BaseRepository["Any"]):
    """Repository for operations."""

    @abstractmethod
    async def get_active(self) -> list[Any]: ...

    @abstractmethod
    async def get_by_prefix(self, prefix: str, *, limit: int = 50) -> list[Any]: ...


class ScheduleRepository(BaseRepository["Any"]):
    """Repository for scheduled jobs."""

    @abstractmethod
    async def get_due(self) -> list[Any]: ...

    @abstractmethod
    async def get_by_prefix(self, prefix: str) -> list[Any]: ...
