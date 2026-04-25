"""Credential references for persisted scheduler jobs.

Scheduler jobs are stored in Blob Storage. This module intentionally refuses to
persist client secrets in those job records. Jobs keep only tenant/client IDs and
an external secret reference that is resolved at execution time.
"""
from __future__ import annotations

import os
import re
import uuid
from typing import Any, Dict, Mapping, Tuple
from urllib.parse import urlparse

import httpx


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalise_ref(ref: Mapping[str, Any]) -> Dict[str, Any]:
    ref_type = str(ref.get("type") or ref.get("kind") or "").strip()
    if ref_type in {"keyvault", "key-vault", "key_vault"}:
        ref_type = "key_vault_secret_uri"
    if ref_type in {"env", "environment"}:
        ref_type = "environment_variable"
    if ref_type not in {"key_vault_secret_uri", "environment_variable", "connection_id"}:
        raise ValueError("Unsupported scheduler client secret reference type")

    if ref_type == "key_vault_secret_uri":
        uri = str(ref.get("uri") or ref.get("secretUri") or "").strip()
        if not uri:
            raise ValueError("Key Vault secret URI is required for scheduler credential reference")
        parsed = urlparse(uri)
        if parsed.scheme != "https" or ".vault.azure.net" not in parsed.netloc:
            raise ValueError("schedulerClientSecretUri must be an HTTPS Azure Key Vault secret URI")
        return {"type": ref_type, "uri": uri}

    if ref_type == "environment_variable":
        name = str(ref.get("name") or ref.get("env") or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("scheduler client secret environment variable name is invalid")
        return {"type": ref_type, "name": name}

    connection_id = str(ref.get("id") or ref.get("connectionId") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", connection_id):
        raise ValueError("scheduler connection ID is invalid")
    return {"type": ref_type, "id": connection_id}


def make_scheduler_credential_config(creds: Mapping[str, Any]) -> Dict[str, Any]:
    """Return safe scheduler credential config with no client_secret value.

    A secret reference can be supplied by request/config as ``client_secret_ref``
    or by environment:
    - ``SCHEDULER_CLIENT_SECRET_URI`` for a Key Vault secret URI resolved via Managed Identity/DefaultAzureCredential.
    - ``SCHEDULER_CLIENT_SECRET_ENV`` for local/dev environments where the secret lives in an environment variable.
    - ``SCHEDULER_CONNECTION_ID`` for a named tenant connection whose secret is in
      ``SCHEDULER_CONNECTION_<ID>_CLIENT_SECRET``.
    """
    tenant_id = str(creds.get("tenant_id") or "").strip()
    client_id = str(creds.get("client_id") or "").strip()
    if not tenant_id or not client_id:
        raise ValueError("tenant_id and client_id are required for scheduled jobs")

    ref = creds.get("client_secret_ref") or creds.get("credentialRef")
    if ref:
        client_secret_ref = _normalise_ref(ref)
    elif os.environ.get("SCHEDULER_CLIENT_SECRET_URI"):
        client_secret_ref = _normalise_ref({
            "type": "key_vault_secret_uri",
            "uri": os.environ["SCHEDULER_CLIENT_SECRET_URI"],
        })
    elif os.environ.get("SCHEDULER_CLIENT_SECRET_ENV"):
        client_secret_ref = _normalise_ref({
            "type": "environment_variable",
            "name": os.environ["SCHEDULER_CLIENT_SECRET_ENV"],
        })
    elif os.environ.get("SCHEDULER_CONNECTION_ID"):
        client_secret_ref = _normalise_ref({
            "type": "connection_id",
            "id": os.environ["SCHEDULER_CONNECTION_ID"],
        })
    elif str(creds.get("client_secret") or "").strip():
        # No new Azure resource required: keep the submitted secret only in this
        # process and persist a connection ID reference in Blob. Scheduled jobs
        # created this way will not survive App Service restarts unless the same
        # connection secret is supplied through environment configuration.
        connection_id = f"ephemeral-{uuid.uuid4().hex}"
        os.environ[_connection_env_name(connection_id)] = str(creds["client_secret"]).strip()
        client_secret_ref = {"type": "connection_id", "id": connection_id}
    else:
        raise ValueError(
            "Scheduled jobs cannot persist client_secret. Configure SCHEDULER_CLIENT_SECRET_ENV "
            "for local/App Service settings, SCHEDULER_CONNECTION_ID for a named connection, or an "
            "existing Key Vault secret URI if you already use Key Vault. Rotate any secrets that were "
            "previously stored in _scheduler/jobs.json."
        )

    return {
        "tenant_id": tenant_id,
        "client_id": client_id,
        "client_secret_ref": client_secret_ref,
    }


def _resolve_key_vault_secret(uri: str) -> str:
    try:
        from azure.identity import DefaultAzureCredential
    except Exception as exc:  # pragma: no cover - depends on optional deployment environment
        raise ValueError("azure-identity is required to resolve Key Vault scheduler secrets") from exc

    credential = DefaultAzureCredential()
    token = credential.get_token("https://vault.azure.net/.default").token
    separator = "&" if "?" in uri else "?"
    url = f"{uri}{separator}api-version=7.4"
    with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code != 200:
        raise ValueError(f"Failed to read scheduler client secret from Key Vault [{resp.status_code}]")
    secret = str(resp.json().get("value") or "").strip()
    if not secret:
        raise ValueError("Key Vault scheduler secret is empty")
    return secret


def _connection_env_name(connection_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]", "_", connection_id).upper()
    return f"SCHEDULER_CONNECTION_{safe}_CLIENT_SECRET"


def resolve_scheduler_credentials(config: Mapping[str, Any]) -> Tuple[str, str, str]:
    """Resolve scheduler credentials at runtime without reading persisted secrets."""
    if config.get("client_secret") and not _truthy(os.environ.get("SCHEDULER_ALLOW_LEGACY_INLINE_SECRET")):
        raise ValueError(
            "Legacy scheduled job contains a persisted client_secret. Disable the job, rotate the SPN secret, "
            "and recreate the schedule with a Key Vault or environment secret reference."
        )

    tenant_id = str(config.get("tenant_id") or "").strip()
    client_id = str(config.get("client_id") or "").strip()
    ref = config.get("client_secret_ref") or config.get("credentialRef")
    if not tenant_id or not client_id or not ref:
        raise ValueError("Scheduled job is missing tenant/client ID or client secret reference")

    ref = _normalise_ref(ref)
    if ref["type"] == "environment_variable":
        secret = str(os.environ.get(ref["name"], "")).strip()
        if not secret:
            raise ValueError(f"Scheduler client secret environment variable '{ref['name']}' is not set")
    elif ref["type"] == "connection_id":
        env_name = _connection_env_name(ref["id"])
        secret = str(os.environ.get(env_name, "")).strip()
        if not secret:
            raise ValueError(f"Scheduler connection '{ref['id']}' is missing environment variable {env_name}")
    else:
        secret = _resolve_key_vault_secret(ref["uri"])

    return tenant_id, client_id, secret


def redact_scheduler_config(value: Any) -> Any:
    """Recursively remove accidental secrets before storing or returning jobs."""
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in {"client_secret", "secret", "password", "tap", "token", "access_token"}:
                out[f"{key}_removed"] = True
                continue
            out[key] = redact_scheduler_config(item)
        return out
    if isinstance(value, list):
        return [redact_scheduler_config(item) for item in value]
    return value
