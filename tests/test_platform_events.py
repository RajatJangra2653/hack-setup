"""Tests for platform_core event bus."""

import pytest
from platform_core.events import EventBus, DomainEvent


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.mark.asyncio
async def test_subscribe_and_publish(event_bus):
    received = []

    async def handler(event: DomainEvent):
        received.append(event)

    event_bus.subscribe("test.event", handler)
    await event_bus.publish(DomainEvent(
        event_type="test.event",
        hack_prefix="test-",
        data={"key": "value"},
    ))
    assert len(received) == 1
    assert received[0].data["key"] == "value"


@pytest.mark.asyncio
async def test_wildcard_subscription(event_bus):
    received = []

    async def handler(event: DomainEvent):
        received.append(event.event_type)

    event_bus.subscribe("provision.*", handler)
    await event_bus.publish(DomainEvent(event_type="provision.started", hack_prefix="t-"))
    await event_bus.publish(DomainEvent(event_type="provision.completed", hack_prefix="t-"))
    await event_bus.publish(DomainEvent(event_type="cleanup.started", hack_prefix="t-"))
    assert received == ["provision.started", "provision.completed"]


@pytest.mark.asyncio
async def test_history_limit(event_bus):
    for i in range(600):
        await event_bus.publish(DomainEvent(
            event_type="test.flood",
            hack_prefix="t-",
            data={"i": i},
        ))
    assert len(event_bus._history) <= 500


@pytest.mark.asyncio
async def test_no_subscribers(event_bus):
    # Should not raise
    await event_bus.publish(DomainEvent(event_type="nobody.listens", hack_prefix="t-"))
