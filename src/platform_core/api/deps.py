"""Dependency injection for FastAPI routes."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from platform_core.audit import AuditService
from platform_core.events import EventBus, get_event_bus
from platform_core.inventory import InventoryService
from platform_core.operations import OperationEngine
from platform_core.providers.base import get_registry
from platform_core.reconciliation import ReconciliationEngine
from platform_core.scheduler import SchedulerService
from platform_core.telemetry import get_metrics, MetricsCollector


def get_event_bus_dep() -> EventBus:
    return get_event_bus()


@lru_cache
def get_operation_engine() -> OperationEngine:
    return OperationEngine(event_bus=get_event_bus())


@lru_cache
def get_audit_service() -> AuditService:
    return AuditService(event_bus=get_event_bus())


@lru_cache
def get_inventory_service() -> InventoryService:
    return InventoryService(event_bus=get_event_bus())


@lru_cache
def get_reconciliation_engine() -> ReconciliationEngine:
    return ReconciliationEngine(get_registry(), event_bus=get_event_bus())


@lru_cache
def get_scheduler_service() -> SchedulerService:
    return SchedulerService(event_bus=get_event_bus())


def get_metrics_dep() -> MetricsCollector:
    return get_metrics()


# Type aliases for Depends
EventBusDep = Annotated[EventBus, Depends(get_event_bus_dep)]
OperationDep = Annotated[OperationEngine, Depends(get_operation_engine)]
AuditDep = Annotated[AuditService, Depends(get_audit_service)]
InventoryDep = Annotated[InventoryService, Depends(get_inventory_service)]
ReconciliationDep = Annotated[ReconciliationEngine, Depends(get_reconciliation_engine)]
SchedulerDep = Annotated[SchedulerService, Depends(get_scheduler_service)]
MetricsDep = Annotated[MetricsCollector, Depends(get_metrics_dep)]
