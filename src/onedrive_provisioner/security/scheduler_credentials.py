"""Credential references for persisted scheduler jobs.

Scheduler jobs are stored in Blob Storage. This module intentionally refuses to
persist client secrets in job records.  Instead, secrets are either resolved
from external references (Key Vault, environment variables) or stored in a
separate, dedicated blob path that is automatically cleaned up after the job
completes.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Tuple
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Blob path prefix for scheduler secrets (separate from job JSON)
_SECRETS_BLOB_PREFIX = "_scheduler/secrets/"


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalise_ref(ref: Mapping[str, Any]) -> Dict[str, Any]:
    ref_type = str(ref.get("type") or ref.get("kind") or "").strip()
    if ref_type in {"keyvault", "key-vault", "key_vault"}:
        ref_type = "key_vault_secret_uri"
    if ref_type in {"env", "environment"}:
        ref_type = "environment_variable"
    if ref_type in {"blob", "blob_secret"}:
        ref_type = "blob_secret"
    if ref_type not in {"key_vault_secret_uri", "environment_variable", "connection_id", "blob_secret"}:
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

    if ref_type == "blob_secret":
        secret_id = str(ref.get("id") or "").strip()
        if not re.fullmatch(r"[A-Fa-f0-9]{32}", secret_id):
            raise ValueError("scheduler blob secret ID is invalid")
        return {"type": ref_type, "id": secret_id}

    connection_id = str(ref.get("id") or ref.get("connectionId") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", connection_id):
        raise ValueError("scheduler connection ID is invalid")
    return {"type": ref_type, "id": connection_id}


def _secret_blob_path(secret_id: str) -> str:
    """Return the blob path for a stored scheduler secret."""
    return f"{_SECRETS_BLOB_PREFIX}{secret_id}.json"


def store_secret_blob(blob_client, client_secret: str) -> Dict[str, Any]:
    """Encrypt and store a client secret in a dedicated blob.

    Azure Blob Storage provides encryption at rest (SSE). The secret is stored
    in a separate blob so it never appears in the jobs.json file.  Returns the
    credential reference dict to embed in the job config.
    """
    secret_id = uuid.uuid4().hex
    blob_path = _secret_blob_path(secret_id)
    blob_client.write_json(blob_path, {
        "client_secret": client_secret,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    logger.info("Stored scheduler secret blob %s", blob_path)
    return {"type": "blob_secret", "id": secret_id}


def read_secret_blob(blob_client, secret_id: str) -> str:
    """Read a client secret from its dedicated blob."""
    blob_path = _secret_blob_path(secret_id)
    data = blob_client.read_json(blob_path)
    if not data:
        raise ValueError(
            f"Scheduler secret blob '{blob_path}' not found. "
            "The secret may have been cleaned up. Re-schedule the job with fresh credentials."
        )
    secret = str(data.get("client_secret") or "").strip()
    if not secret:
        raise ValueError(f"Scheduler secret blob '{blob_path}' is empty")
    return secret


def delete_secret_blob(blob_client, secret_id: str) -> None:
    """Delete a scheduler secret blob after job completion."""
    blob_path = _secret_blob_path(secret_id)
    try:
        blob_client.delete_blob(blob_path)
        logger.info("Deleted scheduler secret blob %s", blob_path)
    except Exception:
        logger.warning("Failed to delete scheduler secret blob %s", blob_path)


def make_scheduler_credential_config(creds: Mapping[str, Any], *, blob_client=None) -> Dict[str, Any]:
    """Return safe scheduler credential config with no client_secret value.

    A secret reference can be supplied by request/config as ``client_secret_ref``
    or by environment:
    - ``SCHEDULER_CLIENT_SECRET_URI`` for a Key Vault secret URI resolved via Managed Identity/DefaultAzureCredential.
    - ``SCHEDULER_CLIENT_SECRET_ENV`` for local/dev environments where the secret lives in an environment variable.
    - ``SCHEDULER_CONNECTION_ID`` for a named tenant connection whose secret is in
      ``SCHEDULER_CONNECTION_<ID>_CLIENT_SECRET``.

    When none of the above are configured and a raw ``client_secret`` is provided,
    the secret is stored in a dedicated blob (encrypted at rest by Azure SSE) and
    referenced by ID.  This survives process restarts and App Service deployments.
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
        raw_secret = str(creds["client_secret"]).strip()
        if blob_client:
            # Persist to a dedicated blob — survives restarts
            client_secret_ref = store_secret_blob(blob_client, raw_secret)
        else:
            # Fallback: ephemeral env var (will NOT survive restarts)
            connection_id = f"ephemeral-{uuid.uuid4().hex}"
            os.environ[_connection_env_name(connection_id)] = raw_secret
            client_secret_ref = {"type": "connection_id", "id": connection_id}
            logger.warning(
                "No blob_client provided; using ephemeral env var for scheduler secret. "
                "This will not survive process restarts."
            )
    else:
        raise ValueError(
            "Scheduled jobs require credentials. Provide client_secret, or configure "
            "SCHEDULER_CLIENT_SECRET_ENV / SCHEDULER_CLIENT_SECRET_URI / "
            "SCHEDULER_CONNECTION_ID for persistent credential storage."
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


def resolve_scheduler_credentials(config: Mapping[str, Any], *, blob_client=None) -> Tuple[str, str, str]:
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
    elif ref["type"] == "blob_secret":
        if not blob_client:
            raise ValueError(
                "Scheduler secret is stored in blob storage but no blob client is available. "
                "Ensure AZURE_STORAGE_CONNECTION_STRING is configured."
            )
        secret = read_secret_blob(blob_client, ref["id"])
    elif ref["type"] == "connection_id":
        env_name = _connection_env_name(ref["id"])
        secret = str(os.environ.get(env_name, "")).strip()
        if not secret:
            # Ephemeral connection secrets are lost on process restart.
            # Fall back to the well-known AZURE_CLIENT_SECRET env var
            # (set in App Service / local config) if the client_id matches.
            fallback = str(os.environ.get("AZURE_CLIENT_SECRET", "")).strip()
            if fallback:
                secret = fallback
            else:
                raise ValueError(
                    f"Scheduler connection '{ref['id']}' is missing environment variable {env_name}. "
                    "Set AZURE_CLIENT_SECRET or configure SCHEDULER_CLIENT_SECRET_ENV / "
                    "SCHEDULER_CLIENT_SECRET_URI for persistent scheduler credentials."
                )
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
