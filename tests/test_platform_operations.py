"""Tests for platform_core operations engine."""

import pytest
from platform_core.operations import OperationEngine
from platform_core.events import EventBus
from platform_core.models.operation import OperationType


@pytest.fixture
def ops_engine():
    return OperationEngine(event_bus=EventBus())


@pytest.mark.asyncio
async def test_operation_lifecycle(ops_engine):
    op = await ops_engine.start(
        OperationType.PROVISION,
        "test-",
        actor="test",
    )
    assert op.status.value == "running"

    await ops_engine.add_step(op.id, "step1")
    await ops_engine.complete_step(op.id, "step1")
    await ops_engine.complete(op.id)

    assert op.status.value == "completed"


@pytest.mark.asyncio
async def test_operation_failure(ops_engine):
    op = await ops_engine.start(
        OperationType.CLEANUP,
        "test-",
        actor="test",
    )
    await ops_engine.fail(op.id, "Something broke")
    assert op.status.value == "failed"
    assert "broke" in op.error


@pytest.mark.asyncio
async def test_operation_cancel(ops_engine):
    op = await ops_engine.start(
        OperationType.RECONCILE,
        "test-",
        actor="test",
    )
    result = await ops_engine.cancel(op.id)
    assert result is not None
    assert op.status.value == "cancelled"
