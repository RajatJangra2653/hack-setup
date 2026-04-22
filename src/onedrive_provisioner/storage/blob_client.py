"""Low-level Azure Blob Storage client for hack state persistence.

Authenticates using the same SPN credentials (client_credentials) that are
already available per-request, so no extra configuration is needed.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from azure.storage.blob import BlobServiceClient, ContainerClient

logger = logging.getLogger(__name__)

DEFAULT_CONTAINER = "hack-provisioning-data"


class BlobStateClient:
    """Thin wrapper around azure-storage-blob for JSON state blobs."""

    def __init__(
        self,
        storage_account_name: str,
        container_name: str = DEFAULT_CONTAINER,
        *,
        connection_string: Optional[str] = None,
        credential: Optional[Any] = None,
    ) -> None:
        if connection_string:
            self._service = BlobServiceClient.from_connection_string(connection_string)
        elif credential:
            account_url = f"https://{storage_account_name}.blob.core.windows.net"
            self._service = BlobServiceClient(account_url, credential=credential)
        else:
            raise ValueError("Either connection_string or credential is required")

        self._container_name = container_name
        self._ensure_container()

    def _ensure_container(self) -> None:
        try:
            self._service.create_container(self._container_name)
            logger.info("Created container %s", self._container_name)
        except Exception:
            pass  # already exists

    @property
    def _container(self) -> ContainerClient:
        return self._service.get_container_client(self._container_name)

    def read_json(self, blob_path: str) -> Optional[Dict[str, Any]]:
        """Read and parse a JSON blob. Returns None if not found."""
        try:
            blob = self._container.get_blob_client(blob_path)
            data = blob.download_blob().readall()
            return json.loads(data)
        except Exception as exc:
            if "BlobNotFound" in str(exc) or "404" in str(exc):
                return None
            logger.warning("blob.read_failed path=%s err=%s", blob_path, exc)
            raise

    def write_json(self, blob_path: str, data: Dict[str, Any]) -> None:
        """Write a JSON blob (overwrite if exists)."""
        blob = self._container.get_blob_client(blob_path)
        content = json.dumps(data, indent=2, default=str)
        blob.upload_blob(content, overwrite=True, content_settings=_json_content())
        logger.info("blob.written path=%s size=%d", blob_path, len(content))

    def list_blobs(self, prefix: str) -> List[str]:
        """List blob names under a prefix."""
        return [b.name for b in self._container.list_blobs(name_starts_with=prefix)]

    def delete_blob(self, blob_path: str) -> bool:
        """Delete a blob. Returns True if deleted, False if not found."""
        try:
            blob = self._container.get_blob_client(blob_path)
            blob.delete_blob()
            return True
        except Exception:
            return False


def _json_content():
    from azure.storage.blob import ContentSettings
    return ContentSettings(content_type="application/json")
