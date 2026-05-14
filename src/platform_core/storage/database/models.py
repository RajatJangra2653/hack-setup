"""SQLModel ORM models and async engine setup.

Uses SQLModel (Pydantic + SQLAlchemy) for type-safe ORM with
async support via aiosqlite (SQLite) or asyncpg (PostgreSQL).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import Field, SQLModel, Column, JSON


# ═══════════════════════════════════════════════════════════════════
# ORM Models — map to database tables
# ═══════════════════════════════════════════════════════════════════

class HackRecord(SQLModel, table=True):
    __tablename__ = "hacks"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    prefix: str = Field(index=True, unique=True)
    name: str = ""
    domain: str = ""
    status: str = "draft"
    created_by: str = ""
    schema_version: str = "2.0"
    config: dict = Field(default_factory=dict, sa_column=Column(JSON))
    state: dict = Field(default_factory=dict, sa_column=Column(JSON))

    total_users: int = 0
    provisioned_users: int = 0
    failed_users: int = 0

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    provisioned_at: Optional[datetime] = None
    archived_at: Optional[datetime] = None


class OperationRecord(SQLModel, table=True):
    __tablename__ = "operations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    hack_prefix: str = Field(index=True)
    type: str = ""
    status: str = "pending"
    actor: str = "system"

    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: str = ""
    result: dict = Field(default_factory=dict, sa_column=Column(JSON))
    steps: list = Field(default_factory=list, sa_column=Column(JSON))
    progress_pct: float = 0.0
    retry_count: int = 0

    created_at: datetime = Field(default_factory=datetime.utcnow)


class AuditRecord(SQLModel, table=True):
    __tablename__ = "audit_events"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    hack_prefix: str = Field(index=True)
    event_type: str = Field(index=True)
    actor: str = "system"
    severity: str = "info"
    correlation_id: str = ""
    operation_id: str = ""
    details: dict = Field(default_factory=dict, sa_column=Column(JSON))
    target_entity: str = ""
    target_id: str = ""

    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)


class ScheduleRecord(SQLModel, table=True):
    __tablename__ = "schedules"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    hack_prefix: str = Field(index=True)
    action: str = ""
    status: str = "pending"
    scheduled_at: datetime
    created_by: str = "system"
    config: dict = Field(default_factory=dict, sa_column=Column(JSON))
    result: dict = Field(default_factory=dict, sa_column=Column(JSON))
    error: str = ""
    operation_id: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3

    created_at: datetime = Field(default_factory=datetime.utcnow)
    executed_at: Optional[datetime] = None


class InventoryRecord(SQLModel, table=True):
    __tablename__ = "inventory"

    resource_id: str = Field(primary_key=True)
    resource_type: str = ""
    hack_prefix: str = Field(index=True)
    display_name: str = ""
    owner: str = ""
    team: str = ""
    provider: str = ""
    status: str = "active"
    drift_status: str = "unknown"
    metadata: dict = Field(default_factory=dict, sa_column=Column(JSON))

    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    last_checked: Optional[datetime] = None


# ═══════════════════════════════════════════════════════════════════
# Engine factory
# ═══════════════════════════════════════════════════════════════════

_engine: AsyncEngine | None = None


async def get_engine(database_url: str = "sqlite+aiosqlite:///platform.db") -> AsyncEngine:
    """Get or create the async database engine."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            database_url,
            echo=False,
            future=True,
        )
        async with _engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
    return _engine


async def get_session(engine: AsyncEngine | None = None) -> AsyncSession:
    """Create a new async session."""
    if engine is None:
        engine = await get_engine()
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return async_session()
