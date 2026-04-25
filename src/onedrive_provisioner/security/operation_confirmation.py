"""Server-side confirmation tokens for privileged operations."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)


class OperationConfirmationError(ValueError):
    """Raised when a privileged operation is not confirmed correctly."""


@dataclass
class _ConfirmationRecord:
    operation_id: str
    operation: str
    expected: Dict[str, Any]
    token_hash: str
    operator: str
    created_at: datetime
    expires_at: datetime


class OperationConfirmationStore:
    """In-memory confirmation token store.

    The token proves that the caller first requested a server-generated preview.
    Expected prefix/count fields are bound to the token and must match on the
    second request. In production auth, the operator should be a real user ID.
    """

    def __init__(self, ttl_seconds: int = 300) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._records: Dict[str, _ConfirmationRecord] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _canonical(value: Mapping[str, Any]) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

    def _purge_expired(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [key for key, record in self._records.items() if record.expires_at <= now]
        for key in expired:
            self._records.pop(key, None)

    def create(self, operation: str, expected: Mapping[str, Any], operator: str = "unknown") -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        operation_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        expected_dict = dict(expected)
        record = _ConfirmationRecord(
            operation_id=operation_id,
            operation=operation,
            expected=expected_dict,
            token_hash=self._hash_token(token),
            operator=operator or "unknown",
            created_at=now,
            expires_at=now + self._ttl,
        )
        with self._lock:
            self._purge_expired()
            self._records[operation_id] = record

        confirm_text = build_confirm_text(operation, expected_dict)
        logger.warning(
            "privileged_operation.confirmation_issued operation=%s operation_id=%s operator=%s expected=%s",
            operation,
            operation_id,
            record.operator,
            self._canonical(expected_dict),
        )
        return {
            "confirmationRequired": True,
            "operation": operation,
            "operationId": operation_id,
            "token": token,
            "expiresAt": record.expires_at.isoformat(),
            "confirmText": confirm_text,
            "expected": expected_dict,
            "message": (
                "Server-side confirmation required. Review the server-generated preview, then resend "
                "the same request with confirmation.operationId, confirmation.token, and the exact confirmText."
            ),
        }

    def validate(self, operation: str, expected: Mapping[str, Any], confirmation: Optional[Mapping[str, Any]], operator: str = "unknown") -> None:
        if not confirmation:
            raise OperationConfirmationError("confirmation is required")
        operation_id = str(confirmation.get("operationId") or "")
        token = str(confirmation.get("token") or "")
        confirm_text = str(confirmation.get("confirmText") or "")
        if not operation_id or not token or not confirm_text:
            raise OperationConfirmationError("confirmation.operationId, confirmation.token, and confirmation.confirmText are required")

        with self._lock:
            self._purge_expired()
            record = self._records.get(operation_id)
            if not record:
                raise OperationConfirmationError("confirmation token was not found or expired")
            if record.operation != operation:
                raise OperationConfirmationError("confirmation operation mismatch")
            if record.expected != dict(expected):
                raise OperationConfirmationError("confirmation expected values do not match current request")
            if not hmac.compare_digest(record.token_hash, self._hash_token(token)):
                raise OperationConfirmationError("confirmation token is invalid")
            expected_text = build_confirm_text(operation, record.expected)
            if not hmac.compare_digest(confirm_text, expected_text):
                raise OperationConfirmationError("confirmation text does not match")
            self._records.pop(operation_id, None)

        logger.warning(
            "privileged_operation.confirmed operation=%s operation_id=%s operator=%s expected=%s",
            operation,
            operation_id,
            operator or record.operator,
            self._canonical(record.expected),
        )


def build_confirm_text(operation: str, expected: Mapping[str, Any]) -> str:
    prefix = str(expected.get("prefix") or "manual")
    resource_count = int(expected.get("resourceCount") or expected.get("principalCount") or expected.get("targetUserCount") or 0)
    subscription_count = int(expected.get("subscriptionCount") or 0)
    return f"CONFIRM {operation} {prefix} resources={resource_count} subscriptions={subscription_count}"


DEFAULT_CONFIRMATION_STORE = OperationConfirmationStore()
