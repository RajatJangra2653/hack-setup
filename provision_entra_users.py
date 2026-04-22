"""CLI to run Entra ID bulk provisioning (without web UI).

Usage:
  python provision_entra_users.py sample_provision_config.json [--dry-run]

Reads creds from .env (AZURE_TENANT_ID/CLIENT_ID/CLIENT_SECRET).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from onedrive_provisioner.config import AzureConfig
from onedrive_provisioner.entra import EntraConfig, EntraOrchestrator
from onedrive_provisioner.logging_setup import configure_logging

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _on_user_done(result, done, total):
    badge = {"created": "✓", "existing": "•", "failed": "✗", "dry_run": "·"}.get(
        result.status.value, "?")
    tap = f" tap={result.tap[:8]}…" if result.tap else ""
    print(f"  [{done:>3}/{total}] {badge} {result.user_principal_name}{tap}")


async def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cfg_path = Path(sys.argv[1])
    if not cfg_path.exists():
        print(f"Config file not found: {cfg_path}")
        return 2

    cfg_dict = json.loads(cfg_path.read_text())
    if "--dry-run" in sys.argv:
        cfg_dict["dryRun"] = True

    cfg = EntraConfig.from_dict(cfg_dict)
    if not cfg.domain:
        print("config.domain is required")
        return 2

    azure = AzureConfig(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AZURE_CLIENT_ID"],
        client_secret=os.environ["AZURE_CLIENT_SECRET"],
    )
    configure_logging("INFO")

    print(f"Provisioning {cfg.teams * cfg.users_per_team + cfg.admin_users} users "
          f"(mode={cfg.mode}, dry_run={cfg.dry_run})")
    orch = EntraOrchestrator(azure, concurrency=int(cfg_dict.get("concurrency", 6)))
    report = await orch.provision(cfg, on_user_done=_on_user_done)

    print(f"\nDone. created={report.created}  existing={report.existing}  "
          f"failed={report.failed}  groups_created={report.groups_created}")

    out_path = cfg_path.with_suffix(".result.json")
    out_path.write_text(json.dumps(report.to_dict(), indent=2))
    print(f"Full report: {out_path}")
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
