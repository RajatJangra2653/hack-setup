"""Core type aliases used across the platform."""

from __future__ import annotations

from typing import Any, TypeAlias
from uuid import UUID

# ── Identity types ──────────────────────────────────────────────────
TenantId: TypeAlias = str
PrincipalId: TypeAlias = str
ObjectId: TypeAlias = str
SubscriptionId: TypeAlias = str
HackPrefix: TypeAlias = str
OperationId: TypeAlias = UUID
CorrelationId: TypeAlias = str

# ── JSON-compatible dict ────────────────────────────────────────────
JsonDict: TypeAlias = dict[str, Any]
JsonList: TypeAlias = list[JsonDict]

# ── Callback signatures ────────────────────────────────────────────
ProgressCallback = Any  # callable[[str, float], None]
