"""Generate UPNs/display names from naming convention config."""
from __future__ import annotations

from typing import List

from .models import EntraConfig, UserPlan


def generate_user_plans(cfg: EntraConfig) -> List[UserPlan]:
    """Build the full list of users (regular + admins) to provision."""
    if not cfg.domain:
        raise ValueError("EntraConfig.domain is required (e.g. WWPS319.onmicrosoft.com)")

    plans: List[UserPlan] = []
    prefix = cfg.prefix.rstrip("-") + "-"

    # ----- Regular users -----
    if cfg.mode == "team":
        if cfg.teams < 1:
            raise ValueError("team mode requires teams >= 1")
        for t in range(1, cfg.teams + 1):
            team_id = f"t{t:02d}"
            for u in range(1, cfg.users_per_team + 1):
                user_id = f"u{u:02d}"
                local = f"{prefix}{team_id}-{user_id}"
                display = f"{prefix.rstrip('-').upper()} {team_id.upper()} {user_id.upper()}"
                plans.append(UserPlan(
                    upn=f"{local}@{cfg.domain}",
                    display_name=display,
                    mail_nickname=local,
                    team=team_id,
                ))
    elif cfg.mode == "flat":
        if cfg.users_per_team < 1:
            raise ValueError("flat mode requires usersPerTeam >= 1")
        for u in range(1, cfg.users_per_team + 1):
            user_id = f"u{u:02d}"
            local = f"{prefix}{user_id}"
            display = f"{prefix.rstrip('-').upper()} {user_id.upper()}"
            plans.append(UserPlan(
                upn=f"{local}@{cfg.domain}",
                display_name=display,
                mail_nickname=local,
            ))
    else:
        raise ValueError(f"Unknown mode: {cfg.mode!r} (use 'team' or 'flat')")

    # ----- Admin users -----
    for a in range(1, cfg.admin_users + 1):
        admin_id = f"admin{a:02d}"
        local = f"{prefix}{admin_id}"
        display = f"{prefix.rstrip('-').upper()} ADMIN {a:02d}"
        plans.append(UserPlan(
            upn=f"{local}@{cfg.domain}",
            display_name=display,
            mail_nickname=local,
            is_admin=True,
        ))

    return plans


def team_group_name(cfg: EntraConfig, team_id: str) -> str:
    return f"{cfg.prefix.rstrip('-')}-{team_id}-group"


def admin_group_name(cfg: EntraConfig) -> str:
    return f"{cfg.prefix.rstrip('-')}-admins"
