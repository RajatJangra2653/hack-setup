"""Tests for platform_core telemetry."""

from platform_core.telemetry import (
    MetricsCollector,
    new_correlation_id,
    correlation_id_var,
)


def test_counter():
    m = MetricsCollector()
    m.increment("api.requests", provider="entra")
    m.increment("api.requests", provider="entra")
    m.increment("api.requests", provider="graph")
    assert m.get_counter("api.requests") == 3


def test_histogram():
    m = MetricsCollector()
    m.observe("api.latency", 100.0)
    m.observe("api.latency", 200.0)
    m.observe("api.latency", 150.0)
    vals = m.get_histogram("api.latency")
    assert len(vals) == 3
    assert min(vals) == 100.0
    assert max(vals) == 200.0


def test_timer():
    m = MetricsCollector()
    with m.timer("operation.duration", op="provision"):
        total = sum(range(1000))
    vals = m.get_histogram("operation.duration")
    assert len(vals) == 1
    assert vals[0] > 0


def test_correlation_id():
    cid = new_correlation_id()
    assert cid
    assert correlation_id_var.get() == cid


def test_snapshot():
    m = MetricsCollector()
    m.increment("a")
    m.observe("b", 1.0)
    snap = m.snapshot()
    assert "counters" in snap
    assert "histograms" in snap
