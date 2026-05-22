"""License domain models."""

from __future__ import annotations

from pydantic import BaseModel


class LicenseSku(BaseModel):
    """An available license SKU in the tenant."""

    sku_id: str
    sku_part_number: str
    display_name: str = ""
    total_units: int = 0
    consumed_units: int = 0

    @property
    def available(self) -> int:
        return max(0, self.total_units - self.consumed_units)


class LicenseAssignment(BaseModel):
    """A license assigned to a user."""

    user_id: str
    user_principal_name: str = ""
    sku_id: str
    sku_part_number: str = ""
    display_name: str = ""
    assigned_at: str = ""
    status: str = "active"


class LicenseCatalogEntry(BaseModel):
    """Maps friendly names to SKU part numbers."""

    friendly_name: str
    sku_part_number: str
    display_name: str = ""
    monthly_price_usd: float = 0.0


# ── Standard license catalog ────────────────────────────────────────

LICENSE_CATALOG: dict[str, str] = {
    "M365_E3": "SPE_E3",
    "M365_E5": "SPE_E5",
    "M365_F1": "SPE_F1",
    "M365_F3": "SPE_F3",
    "M365_BP": "O365_BUSINESS_PREMIUM",
    "M365_BS": "O365_BUSINESS_ESSENTIALS",
    "O365_E1": "STANDARDPACK",
    "O365_E3": "ENTERPRISEPACK",
    "O365_E5": "ENTERPRISEPREMIUM",
    "EMS_E3": "EMS",
    "EMS_E5": "EMSPREMIUM",
    "AAD_P1": "AAD_PREMIUM",
    "AAD_P2": "AAD_PREMIUM_P2",
    "TEAMS_ESSENTIALS": "TEAMS_ESSENTIALS_AAD",
    "POWER_BI_PRO": "POWER_BI_PRO",
    "PROJECT_P3": "PROJECTPREMIUM",
    "VISIO_P2": "VISIOCLIENT",
    "WIN_E3": "WIN10_PRO_ENT_SUB",
    "WIN_E5": "WIN10_VDA_E5",
    "DEFENDER_P2": "WIN_DEF_ATP",
    "INTUNE_P1": "INTUNE_A",
    "COPILOT_M365": "Microsoft_365_Copilot",
    "CPC_E2": "CPC_E_2C_8GB_128GB",
    "CPC_E4": "CPC_E_4C_16GB_128GB",
    "CPC_E8": "CPC_E_8C_32GB_128GB",
    "GITHUB_COPILOT": "INTUNE_A",  # Placeholder
}
