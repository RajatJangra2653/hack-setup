"""SQL repository implementations using SQLModel."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.storage.base import (
    HackRepository,
    AuditRepository,
    OperationRepository,
    ScheduleRepository,
)
from platform_core.storage.database.models import (
    HackRecord,
    AuditRecord,
    OperationRecord,
    ScheduleRecord,
)


class SqlHackRepository(HackRepository):
    """SQL-backed hack repository."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def _session(self) -> AsyncSession:
        return await self._session_factory()

    async def get(self, id: str | UUID) -> HackRecord | None:
        async with await self._session() as session:
            return await session.get(HackRecord, id)

    async def get_by_prefix(self, prefix: str) -> HackRecord | None:
        async with await self._session() as session:
            stmt = select(HackRecord).where(HackRecord.prefix == prefix)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def list(self, *, limit: int = 100, offset: int = 0, **filters: Any) -> list[HackRecord]:
        async with await self._session() as session:
            stmt = select(HackRecord).offset(offset).limit(limit).order_by(HackRecord.created_at.desc())
            if "status" in filters:
                stmt = stmt.where(HackRecord.status == filters["status"])
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def list_active(self) -> list[HackRecord]:
        return await self.list(status="active")

    async def list_archived(self) -> list[HackRecord]:
        return await self.list(status="archived")

    async def create(self, entity: HackRecord) -> HackRecord:
        async with await self._session() as session:
            session.add(entity)
            await session.commit()
            await session.refresh(entity)
            return entity

    async def update(self, entity: HackRecord) -> HackRecord:
        async with await self._session() as session:
            entity.updated_at = datetime.utcnow()
            session.add(entity)
            await session.commit()
            await session.refresh(entity)
            return entity

    async def delete(self, id: str | UUID) -> bool:
        async with await self._session() as session:
            entity = await session.get(HackRecord, id)
            if entity:
                await session.delete(entity)
                await session.commit()
                return True
            return False

    async def count(self, **filters: Any) -> int:
        async with await self._session() as session:
            stmt = select(func.count()).select_from(HackRecord)
            result = await session.execute(stmt)
            return result.scalar() or 0

    async def archive(self, prefix: str, *, reason: str = "cleanup") -> bool:
        entity = await self.get_by_prefix(prefix)
        if entity:
            entity.status = "archived"
            entity.archived_at = datetime.utcnow()
            await self.update(entity)
            return True
        return False


class SqlAuditRepository(AuditRepository):
    """SQL-backed audit repository."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def _session(self) -> AsyncSession:
        return await self._session_factory()

    async def get(self, id: str | UUID) -> AuditRecord | None:
        async with await self._session() as session:
            return await session.get(AuditRecord, id)

    async def list(self, *, limit: int = 100, offset: int = 0, **filters: Any) -> list[AuditRecord]:
        async with await self._session() as session:
            stmt = select(AuditRecord).offset(offset).limit(limit).order_by(AuditRecord.timestamp.desc())
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def create(self, entity: AuditRecord) -> AuditRecord:
        async with await self._session() as session:
            session.add(entity)
            await session.commit()
            return entity

    async def update(self, entity: AuditRecord) -> AuditRecord:
        async with await self._session() as session:
            session.add(entity)
            await session.commit()
            return entity

    async def delete(self, id: str | UUID) -> bool:
        return False  # Audit events are immutable

    async def count(self, **filters: Any) -> int:
        async with await self._session() as session:
            stmt = select(func.count()).select_from(AuditRecord)
            result = await session.execute(stmt)
            return result.scalar() or 0

    async def query(
        self,
        prefix: str,
        *,
        event_type: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[AuditRecord]:
        async with await self._session() as session:
            stmt = (
                select(AuditRecord)
                .where(AuditRecord.hack_prefix == prefix)
                .order_by(AuditRecord.timestamp.desc())
                .limit(limit)
            )
            if event_type:
                stmt = stmt.where(AuditRecord.event_type == event_type)
            if actor:
                stmt = stmt.where(AuditRecord.actor == actor)
            if since:
                stmt = stmt.where(AuditRecord.timestamp >= since)
            result = await session.execute(stmt)
            return list(result.scalars().all())


class SqlOperationRepository(OperationRepository):
    """SQL-backed operation repository."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def _session(self) -> AsyncSession:
        return await self._session_factory()

    async def get(self, id: str | UUID) -> OperationRecord | None:
        async with await self._session() as session:
            return await session.get(OperationRecord, id)

    async def list(self, *, limit: int = 100, offset: int = 0, **filters: Any) -> list[OperationRecord]:
        async with await self._session() as session:
            stmt = select(OperationRecord).offset(offset).limit(limit).order_by(OperationRecord.created_at.desc())
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def create(self, entity: OperationRecord) -> OperationRecord:
        async with await self._session() as session:
            session.add(entity)
            await session.commit()
            return entity

    async def update(self, entity: OperationRecord) -> OperationRecord:
        async with await self._session() as session:
            session.add(entity)
            await session.commit()
            return entity

    async def delete(self, id: str | UUID) -> bool:
        return False

    async def count(self, **filters: Any) -> int:
        async with await self._session() as session:
            stmt = select(func.count()).select_from(OperationRecord)
            result = await session.execute(stmt)
            return result.scalar() or 0

    async def get_active(self) -> list[OperationRecord]:
        async with await self._session() as session:
            stmt = select(OperationRecord).where(OperationRecord.status == "running")
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_by_prefix(self, prefix: str, *, limit: int = 50) -> list[OperationRecord]:
        async with await self._session() as session:
            stmt = (
                select(OperationRecord)
                .where(OperationRecord.hack_prefix == prefix)
                .order_by(OperationRecord.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())


class SqlScheduleRepository(ScheduleRepository):
    """SQL-backed schedule repository."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def _session(self) -> AsyncSession:
        return await self._session_factory()

    async def get(self, id: str | UUID) -> ScheduleRecord | None:
        async with await self._session() as session:
            return await session.get(ScheduleRecord, id)

    async def list(self, *, limit: int = 100, offset: int = 0, **filters: Any) -> list[ScheduleRecord]:
        async with await self._session() as session:
            stmt = select(ScheduleRecord).offset(offset).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def create(self, entity: ScheduleRecord) -> ScheduleRecord:
        async with await self._session() as session:
            session.add(entity)
            await session.commit()
            return entity

    async def update(self, entity: ScheduleRecord) -> ScheduleRecord:
        async with await self._session() as session:
            session.add(entity)
            await session.commit()
            return entity

    async def delete(self, id: str | UUID) -> bool:
        async with await self._session() as session:
            entity = await session.get(ScheduleRecord, id)
            if entity:
                await session.delete(entity)
                await session.commit()
                return True
            return False

    async def count(self, **filters: Any) -> int:
        async with await self._session() as session:
            stmt = select(func.count()).select_from(ScheduleRecord)
            result = await session.execute(stmt)
            return result.scalar() or 0

    async def get_due(self) -> list[ScheduleRecord]:
        async with await self._session() as session:
            now = datetime.utcnow()
            stmt = (
                select(ScheduleRecord)
                .where(ScheduleRecord.status == "pending")
                .where(ScheduleRecord.scheduled_at <= now)
                .order_by(ScheduleRecord.scheduled_at)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_by_prefix(self, prefix: str) -> list[ScheduleRecord]:
        async with await self._session() as session:
            stmt = select(ScheduleRecord).where(ScheduleRecord.hack_prefix == prefix)
            result = await session.execute(stmt)
            return list(result.scalars().all())
