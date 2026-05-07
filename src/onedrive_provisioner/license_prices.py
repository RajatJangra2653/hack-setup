"""NCE license price lookup – monthly ERP prices (Commercial, P1M term).

Source: Microsoft NCE License-Based Price List (May 2026 GA).
Prices are monthly per-user ERP in USD for Commercial segment.

The mapping uses friendly product names as keys, matching the values in
LICENSE_DISPLAY_NAMES from entra/models.py.  The `lookup_license_price`
helper resolves SKU part-number codes (e.g. ``SPE_E3``) to their friendly
name before looking up the price.
"""
from __future__ import annotations

from typing import Dict, Optional

from onedrive_provisioner.entra.models import LICENSE_DISPLAY_NAMES

# Friendly product name → monthly ERP price (USD, Commercial, P1M).
NCE_LICENSE_PRICES: Dict[str, float] = {
    "Microsoft 365 E7": 118.80,
    "Microsoft 365 E5": 68.40,
    "Office 365 E5": 45.60,
    "Microsoft 365 E3": 43.20,
    "Power BI Premium Per User": 28.80,
    "Office 365 E3": 27.60,
    "Microsoft 365 Business Premium": 26.40,
    "Microsoft 365 Copilot": 25.20,
    "Power Apps Premium": 24.00,
    "Microsoft Copilot Studio": 240.00,
    "Enterprise Mobility + Security E5": 19.68,
    "Power BI Pro": 16.80,
    "Microsoft 365 Business Standard": 15.00,
    "Microsoft 365 Apps for enterprise": 14.40,
    "Enterprise Mobility + Security E3": 12.72,
    "Microsoft Teams Premium": 12.00,
    "Office 365 E1": 12.00,
    "Microsoft 365 Apps for business": 9.96,
    "Microsoft 365 F3": 9.60,
    "Microsoft 365 Business Basic": 7.20,
    "Microsoft Teams Essentials": 4.80,
    "Office 365 F3": 4.80,
    "Microsoft Defender Vulnerability Management": 3.00,
    "Microsoft 365 F1": 2.76,
}

# Windows 365 Cloud PC SKU codes → monthly ERP price.
# Naming: CPC_{B|E|F}_{vCPU}C_{RAM}RAM_{Storage}
#   B = Business, E = Enterprise, F = Frontline
CPC_LICENSE_PRICES: Dict[str, float] = {
    "CPC_B_2C_4RAM_64GB": 25.60,
    "CPC_B_2C_4RAM_128GB": 28.00,
    "CPC_B_2C_4RAM_256GB": 35.20,
    "CPC_B_2C_8RAM_128GB": 36.00,
    "CPC_B_2C_8RAM_256GB": 43.20,
    "CPC_B_4C_16RAM_128GB": 56.00,
    "CPC_B_4C_16RAM_256GB": 63.20,
    "CPC_B_4C_16RAM_512GB": 84.00,
    "CPC_B_8C_32RAM_128GB": 101.60,
    "CPC_B_8C_32RAM_256GB": 108.80,
    "CPC_B_8C_32RAM_512GB": 129.60,
    "CPC_B_16C_64RAM_512GB": 224.80,
    "CPC_B_16C_64RAM_1TB": 255.20,
    "CPC_E_2C_4RAM_64GB": 28.00,
    "CPC_E_2C_4RAM_128GB": 31.00,
    "CPC_E_2C_4RAM_256GB": 40.00,
    "CPC_E_2C_8RAM_128GB": 41.00,
    "CPC_E_2C_8RAM_256GB": 50.00,
    "CPC_E_4C_16RAM_128GB": 66.00,
    "CPC_E_4C_16RAM_256GB": 75.00,
    "CPC_E_4C_16RAM_512GB": 101.00,
    "CPC_E_8C_32RAM_128GB": 123.00,
    "CPC_E_8C_32RAM_256GB": 132.00,
    "CPC_E_8C_32RAM_512GB": 158.00,
    "CPC_E_16C_64RAM_512GB": 277.00,
    "CPC_E_16C_64RAM_1TB": 315.00,
    "CPC_F_2C_4RAM_64GB": 42.00,
    "CPC_F_2C_4RAM_128GB": 47.00,
    "CPC_F_2C_4RAM_256GB": 60.00,
    "CPC_F_2C_8RAM_128GB": 62.00,
    "CPC_F_2C_8RAM_256GB": 75.00,
    "CPC_F_4C_16RAM_128GB": 99.00,
    "CPC_F_4C_16RAM_256GB": 113.00,
    "CPC_F_4C_16RAM_512GB": 152.00,
    "CPC_F_8C_32RAM_128GB": 185.00,
    "CPC_F_8C_32RAM_256GB": 198.00,
    "CPC_F_8C_32RAM_512GB": 237.00,
    "CPC_F_16C_64RAM_512GB": 416.00,
    "CPC_F_16C_64RAM_1TB": 473.00,
}

# Build a case-insensitive reverse index from friendly name → price.
_PRICE_BY_FRIENDLY_UPPER: Dict[str, float] = {
    k.upper(): v for k, v in NCE_LICENSE_PRICES.items()
}
# Also index CPC SKU codes directly (case-insensitive).
_PRICE_BY_SKU_UPPER: Dict[str, float] = {
    k.upper(): v for k, v in CPC_LICENSE_PRICES.items()
}


def lookup_license_price(sku_or_name: str) -> Optional[float]:
    """Return the monthly ERP price for a license SKU code or friendly name.

    Accepts either a SKU part-number (e.g. ``SPE_E3``) or a friendly display
    name (e.g. ``Microsoft 365 E3``).  Returns ``None`` when no match is found.
    """
    raw = (sku_or_name or "").strip()
    if not raw:
        return None

    # Direct match by friendly name (case-insensitive).
    upper = raw.upper()
    price = _PRICE_BY_FRIENDLY_UPPER.get(upper)
    if price is not None:
        return price

    # Direct match by CPC / Cloud PC SKU code.
    price = _PRICE_BY_SKU_UPPER.get(upper)
    if price is not None:
        return price

    # Resolve SKU code → friendly name via LICENSE_DISPLAY_NAMES, then look up.
    for key, friendly in LICENSE_DISPLAY_NAMES.items():
        if key.upper() == upper:
            price = _PRICE_BY_FRIENDLY_UPPER.get(friendly.upper())
            if price is not None:
                return price

    # Substring fallback for tenant-specific SKU suffixes.
    for key, friendly in LICENSE_DISPLAY_NAMES.items():
        if key.upper() in upper or upper in key.upper():
            price = _PRICE_BY_FRIENDLY_UPPER.get(friendly.upper())
            if price is not None:
                return price

    return None


def resolve_license_prices(sku_names: list[str]) -> Dict[str, float]:
    """Given a list of SKU codes/names, return ``{sku: monthly_price}`` for all matched."""
    result: Dict[str, float] = {}
    for name in sku_names:
        price = lookup_license_price(name)
        if price is not None:
            result[name] = price
    return result


# ── GitHub Enterprise seat costs (fixed monthly per-user) ──────────────
GITHUB_SEAT_COST_WITH_COPILOT: float = 40.00
GITHUB_SEAT_COST_WITHOUT_COPILOT: float = 21.00


def github_seat_cost(with_copilot: bool = True) -> float:
    """Return the monthly per-user GitHub seat cost."""
    return GITHUB_SEAT_COST_WITH_COPILOT if with_copilot else GITHUB_SEAT_COST_WITHOUT_COPILOT
