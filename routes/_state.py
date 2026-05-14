"""Shared state, singletons, and helper functions for all route blueprints."""
from __future__ import annotations

import os
import threading
from typing import Any, Dict

from flask import request, jsonify

# ── Add src to path (idempotent) ──
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from onedrive_provisioner.auth import MsalTokenProvider
from onedrive_provisioner.config import AppConfig, AzureConfig, UploadConfig, ExecutionConfig
from onedrive_provisioner.storage import HackStateManager
from onedrive_provisioner.storage.blob_client import BlobStateClient
from onedrive_provisioner.security import DEFAULT_CONFIRMATION_STORE, OperationConfirmationError
from onedrive_provisioner.security.scheduler_credentials import make_scheduler_credential_config
from onedrive_provisioner.audit import AuditLogger
from onedrive_provisioner.operations import OperationTracker


FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")

# ── Blob Storage state persistence ──
_state_mgr: HackStateManager | None = None
_state_mgr_lock = threading.Lock()


def get_state_manager() -> HackStateManager | None:
    global _state_mgr
    if _state_mgr is not None:
        return _state_mgr
    with _state_mgr_lock:
        if _state_mgr is not None:
            return _state_mgr
        conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
        if not conn_str:
            return None
        try:
            client = BlobStateClient("", connection_string=conn_str)
            _state_mgr = HackStateManager(client)
            return _state_mgr
        except Exception as exc:
            print(f"[WARN] Could not init blob state manager: {exc}")
            return None


# ── In-memory job store ──
jobs: Dict[str, Dict[str, Any]] = {}
jobs_lock = threading.Lock()
MAX_JOBS = 200

# ── In-memory device-code provisioning sessions ──
prov_sessions: Dict[str, Dict[str, Any]] = {}
prov_lock = threading.Lock()

# ── In-memory Entra provisioning sessions ──
entra_sessions: Dict[str, Dict[str, Any]] = {}
entra_lock = threading.Lock()
MAX_ENTRA_SESSIONS = 100

# ── In-memory GitHub-enable sessions ──
github_sessions: Dict[str, Dict[str, Any]] = {}
github_lock = threading.Lock()
MAX_GITHUB_SESSIONS = 100

# ── In-memory generated docs store ──
generated_docs: Dict[str, Dict[str, Any]] = {}
docs_lock = threading.Lock()
MAX_DOCS = 50

# ── Audit & operation tracking singletons ──
audit_logger = AuditLogger(get_state_manager)
operation_tracker = OperationTracker(get_state_manager)


# ────────────────────── Helpers ──────────────────────

def make_token_provider(t, c, s):
    return MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))


def build_config(tenant_id, client_id, client_secret, concurrency=8, dry_run=False):
    return AppConfig(
        azure=AzureConfig(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret),
        upload=UploadConfig(),
        execution=ExecutionConfig(concurrency=min(max(1, concurrency), 64), dry_run=dry_run),
    )


def extract_creds(data: dict):
    t = (data.get("tenant_id") or "").strip()
    c = (data.get("client_id") or "").strip()
    s = (data.get("client_secret") or "").strip()
    if not t or not c or not s:
        return None
    return t, c, s


def operator_from_request(data: dict) -> str:
    return (
        request.headers.get("X-Operator")
        or request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
        or data.get("operator")
        or data.get("createdBy")
        or "unknown"
    )


def confirmation_response(operation: str, expected: dict, data: dict):
    confirmation = DEFAULT_CONFIRMATION_STORE.create(
        operation,
        expected,
        operator=operator_from_request(data),
    )
    return jsonify(confirmation), 409


def require_confirmation(operation: str, expected: dict, data: dict):
    try:
        DEFAULT_CONFIRMATION_STORE.validate(
            operation,
            expected,
            data.get("confirmation"),
            operator=operator_from_request(data),
        )
        return None
    except OperationConfirmationError:
        return confirmation_response(operation, expected, data)


def scheduler_creds_dict(creds, data: dict) -> dict:
    t, c, s = creds
    return {
        "tenant_id": t,
        "client_id": c,
        "client_secret": s,
        "client_secret_ref": (
            data.get("client_secret_ref")
            or data.get("credentialRef")
            or data.get("schedulerClientSecretRef")
        ),
    }


def is_archived_state(state: dict) -> bool:
    return bool(state.get("isArchived") or state.get("archivedAt") or state.get("lifecycleStatus") == "archived")
