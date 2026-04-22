import os
from pathlib import Path

import pytest

from onedrive_provisioner.config import load_config


def test_env_overrides(tmp_path, monkeypatch):
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        """
azure:
  tenant_id: t
  client_id: c
  client_secret: s
execution:
  concurrency: 2
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEDRIVE_CONCURRENCY", "16")
    monkeypatch.setenv("ONEDRIVE_DRY_RUN", "true")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "from-env")
    cfg = load_config(cfg_path)
    assert cfg.execution.concurrency == 16
    assert cfg.execution.dry_run is True
    assert cfg.azure.client_secret == "from-env"


def test_requires_some_auth(tmp_path):
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text("azure:\n  tenant_id: t\n  client_id: c\n", encoding="utf-8")
    with pytest.raises(Exception):
        load_config(cfg_path)


def test_cert_auth_ok(tmp_path):
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        """
azure:
  tenant_id: t
  client_id: c
  cert_path: /tmp/x.pem
  cert_thumbprint: ABCDEF
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.azure.cert_thumbprint == "ABCDEF"
