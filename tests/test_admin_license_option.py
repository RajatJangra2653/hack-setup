import pytest

from onedrive_provisioner.config import AzureConfig
from onedrive_provisioner.entra.models import EntraConfig
from onedrive_provisioner.entra.orchestrator import EntraOrchestrator


@pytest.mark.asyncio
async def test_dry_run_excludes_admin_licenses_by_default():
    cfg = EntraConfig.from_dict({
        "domain": "contoso.onmicrosoft.com",
        "mode": "flat",
        "usersPerTeam": 1,
        "adminUsers": 1,
        "licenses": ["M365_E3"],
        "dryRun": True,
    })
    report = await EntraOrchestrator(AzureConfig(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
    )).provision(cfg)

    admin = next(user for user in report.users if user.is_admin)
    participant = next(user for user in report.users if not user.is_admin)
    assert participant.licenses == ["M365_E3"]
    assert admin.licenses == []


@pytest.mark.asyncio
async def test_dry_run_can_include_admin_licenses():
    cfg = EntraConfig.from_dict({
        "domain": "contoso.onmicrosoft.com",
        "mode": "flat",
        "usersPerTeam": 1,
        "adminUsers": 1,
        "licenses": ["M365_E3"],
        "assignLicensesToAdmins": True,
        "dryRun": True,
    })
    report = await EntraOrchestrator(AzureConfig(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
    )).provision(cfg)

    admin = next(user for user in report.users if user.is_admin)
    assert admin.licenses == ["M365_E3"]
