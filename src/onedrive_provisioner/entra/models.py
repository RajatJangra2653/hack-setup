"""Models for Entra provisioning: input config, per-user plan, results."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Status(str, Enum):
    CREATED = "created"
    EXISTING = "existing"  # already existed; idempotent skip
    FAILED = "failed"
    DRY_RUN = "dry_run"


# Friendly license names → SKU partNumber substring(s) to match in /subscribedSkus.
# We match any SKU whose skuPartNumber CONTAINS one of the listed tokens.
# This handles tenant-specific suffixes (e.g. SPE_E3 vs SPE_E3_GOV).
LICENSE_CATALOG: Dict[str, List[str]] = {
    "M365_BUSINESS":   ["O365_BUSINESS_PREMIUM", "SPB"],
    "M365_E3":         ["SPE_E3", "ENTERPRISEPACK"],
    "M365_E5":         ["SPE_E5", "ENTERPRISEPREMIUM"],
    "COPILOT":         ["Microsoft_365_Copilot", "COPILOT_FOR_MICROSOFT_365"],
    "TEAMS_ESSENTIALS":["Teams_Ess", "TEAMS_ESSENTIALS"],
    "TEAMS_PREMIUM":   ["Teams_Premium", "TEAMS_PREMIUM"],
    "POWER_BI_PRO":    ["POWER_BI_PRO"],
    "POWER_APPS":      ["POWERAPPS_PER_USER", "POWER_APPS"],
    "COPILOT_STUDIO":  ["CCIBOTS_PRIVPREV_VIRAL", "Microsoft_Copilot_Studio_User", "COPILOT_STUDIO"],
}


LICENSE_DISPLAY_NAMES: Dict[str, str] = {
    "SPE_E3": "Microsoft 365 E3",
    "SPE_E5": "Microsoft 365 E5",
    "SPE_F1": "Microsoft 365 F1",
    "SPE_F3": "Microsoft 365 F3",
    "ENTERPRISEPACK": "Office 365 E3",
    "ENTERPRISEPREMIUM": "Office 365 E5",
    "DEVELOPERPACK_E5": "Microsoft 365 E5 Developer",
    "M365_G3_GOV": "Microsoft 365 G3 GCC",
    "M365_G5_GOV": "Microsoft 365 G5 GCC",
    "M365_G3_GOVERNMENT": "Microsoft 365 G3 GCC",
    "M365_G5_GOVERNMENT": "Microsoft 365 G5 GCC",
    "EMSPREMIUM": "Enterprise Mobility + Security E5",
    "EMS": "Enterprise Mobility + Security E3",
    "SPB": "Microsoft 365 Business Premium",
    "O365_BUSINESS_PREMIUM": "Microsoft 365 Business Standard",
    "O365_BUSINESS_ESSENTIALS": "Microsoft 365 Business Basic",
    "TEAMS_ESSENTIALS": "Microsoft Teams Essentials",
    "Teams_Ess": "Microsoft Teams Essentials",
    "TEAMS_PREMIUM": "Microsoft Teams Premium",
    "Teams_Premium": "Microsoft Teams Premium",
    "POWER_BI_PRO": "Power BI Pro",
    "PBI_PREMIUM_PER_USER": "Power BI Premium Per User",
    "POWERAPPS_PER_USER": "Power Apps Premium",
    "POWER_APPS_PER_USER": "Power Apps Premium",
    "FLOW_FREE": "Power Automate Free",
    "Microsoft_365_Copilot": "Microsoft 365 Copilot",
    "COPILOT_FOR_MICROSOFT_365": "Microsoft 365 Copilot",
    "M365_COPILOT": "Microsoft 365 Copilot",
    "Microsoft_Copilot_Studio_User": "Microsoft Copilot Studio User",
    "CCIBOTS_PRIVPREV_VIRAL": "Microsoft Copilot Studio",
    "COPILOT_STUDIO": "Microsoft Copilot Studio",
}


def license_display_name(sku: str, *, include_sku: bool = False) -> str:
    """Return a friendly product name for a Microsoft license SKU when known."""
    raw = (sku or "").strip()
    if not raw:
        return ""
    friendly = ""
    for key, value in LICENSE_DISPLAY_NAMES.items():
        if key.upper() == raw.upper():
            friendly = value
            break
    if not friendly:
        upper = raw.upper()
        if "SPE_E5" in upper:
            friendly = "Microsoft 365 E5"
        elif "SPE_E3" in upper:
            friendly = "Microsoft 365 E3"
        elif "SPE_F3" in upper:
            friendly = "Microsoft 365 F3"
        elif "SPE_F1" in upper:
            friendly = "Microsoft 365 F1"
        elif "ENTERPRISEPREMIUM" in upper:
            friendly = "Office 365 E5"
        elif "ENTERPRISEPACK" in upper:
            friendly = "Office 365 E3"
        elif "POWER_BI" in upper or "PBI_" in upper:
            friendly = "Power BI Premium Per User" if "PREMIUM" in upper else "Power BI Pro"
        elif "COPILOT" in upper and "STUDIO" in upper:
            friendly = "Microsoft Copilot Studio"
        elif "COPILOT" in upper:
            friendly = "Microsoft 365 Copilot"
        elif "TEAMS" in upper and "PREMIUM" in upper:
            friendly = "Microsoft Teams Premium"
        elif "TEAMS" in upper:
            friendly = "Microsoft Teams"
        elif "POWERAPPS" in upper or "POWER_APPS" in upper:
            friendly = "Power Apps"
    friendly = friendly or raw
    return f"{friendly} ({raw})" if include_sku and friendly != raw else friendly


@dataclass
class EntraConfig:
    prefix: str = "nyc-esri-gcc-"
    domain: str = ""  # e.g. "WWPS319.onmicrosoft.com" — required
    teams: int = 0
    users_per_team: int = 10
    mode: str = "team"  # "team" | "flat"
    licenses: List[str] = field(default_factory=list)
    assign_licenses_to_admins: bool = False
    admin_users: int = 0
    tap_lifetime: int = 120  # minutes
    dry_run: bool = False
    skip_existing: bool = True
    initial_password: Optional[str] = None  # auto-generated if None
    force_change_password: bool = False  # force password change on first login
    create_team_groups: bool = True
    create_admin_group: bool = True
    assign_admin_role: bool = True  # Global Reader
    # Phase A — metadata stamping (visible in Entra & search)
    hack_name: str = ""           # e.g. "NYC Esri GCC Hack — Apr 2026"
    created_by: str = ""          # free-form identifier of who ran provisioning
    concurrency: int = 6          # surfaced from API into orchestrator
    # GitHub EMU enablement (uses hardcoded broker creds in github_emu module)
    enable_github: bool = False
    enable_github_copilot: bool = False
    enable_github_ghas: bool = False
    enable_github_for_admins: bool = False
    github_use_legacy_groups: bool = False
    github_group_id_override: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EntraConfig":
        return cls(
            prefix=d.get("prefix", cls.prefix),
            domain=d.get("domain", ""),
            teams=int(d.get("teams", 0)),
            users_per_team=int(d.get("usersPerTeam", 10)),
            mode=d.get("mode", "team"),
            licenses=list(d.get("licenses", [])),
            assign_licenses_to_admins=bool(d.get("assignLicensesToAdmins", False)),
            admin_users=int(d.get("adminUsers", 0)),
            tap_lifetime=int(d.get("tapLifetime", 120)),
            dry_run=bool(d.get("dryRun", False)),
            skip_existing=bool(d.get("skipExisting", True)),
            initial_password=d.get("initialPassword"),
            force_change_password=bool(d.get("forceChangePassword", False)),
            create_team_groups=bool(d.get("createTeamGroups", True)),
            create_admin_group=bool(d.get("createAdminGroup", True)),
            assign_admin_role=bool(d.get("assignAdminRole", True)),
            hack_name=d.get("hackName", "") or "",
            created_by=d.get("createdBy", "") or "",
            concurrency=int(d.get("concurrency", 6)),
            enable_github=bool(d.get("enableGithub", False)),
            enable_github_copilot=bool(d.get("enableGithubCopilot", False)),
            enable_github_ghas=bool(d.get("enableGithubGhas", False)),
            enable_github_for_admins=bool(d.get("enableGithubForAdmins", False)),
            github_use_legacy_groups=bool(d.get("githubUseLegacyGroups", False)),
            github_group_id_override=d.get("githubGroupIdOverride") or None,
        )


@dataclass
class UserPlan:
    """A single user to provision."""
    upn: str
    display_name: str
    mail_nickname: str
    is_admin: bool = False
    team: Optional[str] = None  # e.g. "t04"


@dataclass
class UserProvisionResult:
    user_principal_name: str
    status: Status
    user_id: Optional[str] = None
    password: Optional[str] = None  # only set for newly-created users
    tap: Optional[str] = None
    tap_expires: Optional[str] = None
    licenses: List[str] = field(default_factory=list)
    groups: List[str] = field(default_factory=list)
    group_failures: List[str] = field(default_factory=list)  # groups that failed to add
    is_admin: bool = False
    message: Optional[str] = None
    github: Optional[Dict[str, Any]] = None  # GitHub EMU enablement result, when requested

    def to_dict(self) -> Dict[str, Any]:
        return {
            "userPrincipalName": self.user_principal_name,
            "status": self.status.value,
            "userId": self.user_id,
            "password": self.password,
            "tap": self.tap,
            "tapExpires": self.tap_expires,
            "licenses": self.licenses,
            "groups": self.groups,
            "groupFailures": self.group_failures,
            "isAdmin": self.is_admin,
            "message": self.message,
            "github": self.github,
        }


@dataclass
class ProvisioningReport:
    total_users: int
    created: int
    existing: int
    failed: int
    admins: int
    groups_created: int
    groups: List[str]
    users: List[UserProvisionResult]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "totalUsers": self.total_users,
            "created": self.created,
            "existing": self.existing,
            "failed": self.failed,
            "admins": self.admins,
            "groupsCreated": self.groups_created,
            "groups": self.groups,
            "users": [u.to_dict() for u in self.users],
        }
