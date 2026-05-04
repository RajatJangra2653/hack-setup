"""Constants and utility functions for GitHub EMU integration."""
from __future__ import annotations

from typing import Optional


# ── Hardcoded GitHub EMU tenant credentials ──────────────────────────────
# These belong to a separate "GitHub EMU broker" Entra app and are NOT
# the same as the credentials the rest of the app uses for hack provisioning.
GITHUB_TENANT_ID = "f871d17e-efcd-44c7-ba5a-0162efa2fded"
GITHUB_CLIENT_ID = "e6b585c6-079f-489c-ae6b-a57a274139ea"
GITHUB_CLIENT_SECRET = "w2R8Q~MooRgSA855CVxZitnxayzHDecQx4yFHahc"

# ── Group IDs ────────────────────────────────────────────────────────────
# Primary (used by default) — public-sector-hacks groups.
GROUP_PUBLIC_SECTOR_HACKS = "3c92ccf9-53a5-45e0-a5c3-cfcd98e135a9"          # without Copilot
GROUP_PUBLIC_SECTOR_HACKS_COPILOT = "8e075647-984e-42da-9ffd-8a9a8272f257"  # with Copilot

# Legacy / alternate groups — still wired in so callers can opt in.
GROUP_LEGACY_DEFAULT = "83311b9f-c349-4a10-bb7f-e9342e76ea10"
GROUP_LEGACY_COPILOT = "bb0215fb-69d3-4d16-be56-cd2da619de31"
GROUP_GHAS = "5bd562c2-02f4-463d-b9ef-86fd666f5fe7"

# Provisioning ruleId, SP ID, and synchronization job ID for GitHub EMU.
GITHUB_RULE_ID = "03f7d90d-bf71-41b1-bda6-aaf0ddbee5d8"
GITHUB_SP_ID = "da6c7f14-b7a5-4b1b-b357-3594173bea4a"
GITHUB_SYNC_JOB_ID = (
    "gitHubEnterpriseCloud.f871d17eefcd44c7ba5a0162efa2fded."
    "d2318294-74b6-4d39-b351-8f0ee74687c0"
)

# GitHub Enterprise Managed Users "short name" — appended (with an underscore)
# to the email local-part to form the EMU handle. Example:
#   california-t01-u01@publicsectorhacks.com  →  california-t01-u01_clabs
GITHUB_EMU_SHORT_NAME = "clabs"


def derive_github_username(email: str, *, short_name: str = GITHUB_EMU_SHORT_NAME) -> str:
    """Derive the GitHub EMU handle for a tenant user email.

    EMU handles are always ``<localpart>_<enterprise-short-name>``. The
    local-part is lowercased and stripped of characters GitHub does not allow
    in usernames; the suffix is added with a single underscore.
    """
    email = (email or "").strip()
    if "@" not in email:
        local = email
    else:
        local = email.split("@", 1)[0]
    local = local.strip().lower()
    # GitHub usernames allow alphanumerics and single hyphens only. EMU also
    # accepts underscores in the short-name suffix. Replace anything else
    # with a hyphen and collapse repeats.
    cleaned = []
    for ch in local:
        if ch.isalnum() or ch in ("-",):
            cleaned.append(ch)
        else:
            cleaned.append("-")
    local = "".join(cleaned).strip("-")
    while "--" in local:
        local = local.replace("--", "-")
    suffix = (short_name or "").strip().lower()
    return f"{local}_{suffix}" if suffix else local


def resolve_group_id(
    *,
    with_copilot: bool = False,
    with_ghas: bool = False,
    use_legacy: bool = False,
    override: Optional[str] = None,
) -> str:
    """Pick the right Entra group object ID for a user."""
    if override:
        return override
    if with_ghas:
        return GROUP_GHAS
    if use_legacy:
        return GROUP_LEGACY_COPILOT if with_copilot else GROUP_LEGACY_DEFAULT
    return GROUP_PUBLIC_SECTOR_HACKS_COPILOT if with_copilot else GROUP_PUBLIC_SECTOR_HACKS
