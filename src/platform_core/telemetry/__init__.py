"""Telemetry & observability — structured logging, correlation IDs, metrics.

Provides:
  - Structured JSON logging with correlation context
  - Request/operation correlation ID propagation
  - Metrics collection (counters, histograms)
  - OpenTelemetry-compatible tracing hooks
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from typing import Any
from uuid import uuid4

# ── Correlation context ──────────────────────────────────────────

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)
operation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "operation_id", default=""
)
hack_prefix_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "hack_prefix", default=""
)


def new_correlation_id() -> str:
    cid = str(uuid4())
    correlation_id_var.set(cid)
    return cid


def set_context(
    *,
    correlation_id: str = "",
    operation_id: str = "",
    hack_prefix: str = "",
) -> None:
    if correlation_id:
        correlation_id_var.set(correlation_id)
    if operation_id:
        operation_id_var.set(operation_id)
    if hack_prefix:
        hack_prefix_var.set(hack_prefix)


# ── Structured JSON formatter ───────────────────────────────────

class JsonFormatter(logging.Formatter):
    """Structured JSON log formatter with correlation context."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add correlation context
        cid = correlation_id_var.get("")
        if cid:
            log_entry["correlation_id"] = cid
        oid = operation_id_var.get("")
        if oid:
            log_entry["operation_id"] = oid
        prefix = hack_prefix_var.get("")
        if prefix:
            log_entry["hack_prefix"] = prefix

        # Add exception info
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Add extra fields
        for key in ("data", "duration_ms", "status_code", "provider", "resource_type"):
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val

        return json.dumps(log_entry, default=str)


def setup_logging(
    *,
    level: str = "INFO",
    json_format: bool = True,
    log_file: str = "",
) -> None:
    """Configure structured logging for the platform."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    if json_format:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
        ))
    root.addHandler(handler)

    if log_file:
        fh = logging.FileHandler(log_file)
        if json_format:
            fh.setFormatter(JsonFormatter())
        root.addHandler(fh)

    # Quiet noisy libraries
    for lib in ("azure", "msal", "urllib3", "httpx", "aiohttp"):
        logging.getLogger(lib).setLevel(logging.WARNING)


# ── Metrics ──────────────────────────────────────────────────────

class MetricsCollector:
    """Simple in-memory metrics collector."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._histograms: dict[str, list[float]] = {}

    def increment(self, name: str, value: int = 1, **tags: str) -> None:
        key = self._key(name, tags)
        self._counters[key] = self._counters.get(key, 0) + value

    def observe(self, name: str, value: float, **tags: str) -> None:
        key = self._key(name, tags)
        self._histograms.setdefault(key, []).append(value)

    def timer(self, name: str, **tags: str) -> _Timer:
        return _Timer(self, name, tags)

    def get_counter(self, name: str) -> int:
        return sum(v for k, v in self._counters.items() if k.startswith(name))

    def get_histogram(self, name: str) -> list[float]:
        result = []
        for k, v in self._histograms.items():
            if k.startswith(name):
                result.extend(v)
        return result

    def snapshot(self) -> dict[str, Any]:
        return {
            "counters": dict(self._counters),
            "histograms": {
                k: {
                    "count": len(v),
                    "min": min(v) if v else 0,
                    "max": max(v) if v else 0,
                    "avg": sum(v) / len(v) if v else 0,
                }
                for k, v in self._histograms.items()
            },
        }

    def reset(self) -> None:
        self._counters.clear()
        self._histograms.clear()

    @staticmethod
    def _key(name: str, tags: dict[str, str]) -> str:
        if tags:
            tag_str = ",".join(f"{k}={v}" for k, v in sorted(tags.items()))
            return f"{name}{{{tag_str}}}"
        return name


class _Timer:
    """Context manager for timing operations."""

    def __init__(self, collector: MetricsCollector, name: str, tags: dict[str, str]) -> None:
        self._collector = collector
        self._name = name
        self._tags = tags
        self._start = 0.0

    def __enter__(self) -> _Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *args: Any) -> None:
        elapsed = (time.perf_counter() - self._start) * 1000
        self._collector.observe(self._name, elapsed, **self._tags)


# Singleton
_metrics = MetricsCollector()


def get_metrics() -> MetricsCollector:
    return _metrics
