"""CLI for onedrive-provisioner."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import List, Optional

import click

from .config import load_config
from .logging_setup import configure_logging, get_logger
from .orchestrator import Orchestrator
from .reporting import write_reports

logger = get_logger(__name__)


def _read_users_file(path: str) -> List[str]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    out: list[str] = []
    if p.suffix.lower() == ".csv":
        import csv
        for row in csv.reader(text.splitlines()):
            if row and row[0].strip() and not row[0].lstrip().startswith("#"):
                out.append(row[0].strip())
    else:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def _load(config_path: Optional[str]):
    cfg = load_config(config_path)
    configure_logging(cfg.log_level)
    return cfg


@click.group()
@click.option("--config", "-c", "config_path", type=click.Path(dir_okay=False),
              help="Path to YAML config (env vars still applied).")
@click.pass_context
def main(ctx: click.Context, config_path: Optional[str]) -> None:
    """Automated OneDrive provisioning + uploads via Microsoft Graph."""
    ctx.ensure_object(dict)
    ctx.obj["cfg"] = _load(config_path)


@main.command("provision")
@click.argument("user")
@click.pass_context
def cmd_provision(ctx: click.Context, user: str) -> None:
    """Ensure OneDrive is provisioned for USER (UPN or object ID)."""
    cfg = ctx.obj["cfg"]
    orch = Orchestrator(cfg)
    res = asyncio.run(orch.provision_user(user))
    click.echo(json.dumps(res.to_dict(), indent=2))
    sys.exit(0 if res.status.value == "success" else 1)


@main.command("upload")
@click.argument("user")
@click.option("--source", help="Local folder OR azure-blob URL (overrides config).")
@click.option("--destination", help="OneDrive root-relative destination (overrides config).")
@click.pass_context
def cmd_upload(ctx: click.Context, user: str, source: Optional[str], destination: Optional[str]) -> None:
    """Upload folder to USER's OneDrive."""
    cfg = ctx.obj["cfg"]
    orch = Orchestrator(cfg)
    res = asyncio.run(orch.upload_for_user(user, source, destination))
    click.echo(json.dumps(res.to_dict(), indent=2))
    sys.exit(0 if res.status.value in ("success", "dry_run", "skipped") else 1)


@main.command("bulk")
@click.option("--users-file", type=click.Path(exists=True, dir_okay=False),
              help="File with one UPN/objectId per line (.txt or .csv).")
@click.option("--all-users", is_flag=True, help="Run against all enabled member users in tenant.")
@click.option("--source", help="Local folder OR azure-blob URL (overrides config).")
@click.option("--destination", help="OneDrive root-relative destination (overrides config).")
@click.option("--dry-run", is_flag=True, help="Plan only, do not upload.")
@click.option("--concurrency", type=int, help="Override parallel worker count.")
@click.option("--report/--no-report", default=True, help="Write report files.")
@click.pass_context
def cmd_bulk(
    ctx: click.Context,
    users_file: Optional[str],
    all_users: bool,
    source: Optional[str],
    destination: Optional[str],
    dry_run: bool,
    concurrency: Optional[int],
    report: bool,
) -> None:
    """Run provisioning + upload for many users in parallel."""
    cfg = ctx.obj["cfg"]
    if dry_run:
        cfg.execution.dry_run = True
    if concurrency:
        cfg.execution.concurrency = concurrency
    if all_users:
        cfg.users.all_users = True

    explicit: Optional[List[str]] = None
    if users_file:
        explicit = _read_users_file(users_file)

    orch = Orchestrator(cfg)
    rep = asyncio.run(orch.bulk_setup(explicit, source, destination))
    click.echo(json.dumps(rep.to_dict(), indent=2))

    if report:
        paths = write_reports(rep, cfg.reporting.output_dir, cfg.reporting.formats)
        for p in paths:
            click.echo(f"report: {p}", err=True)

    sys.exit(0 if rep.failed == 0 else 2)


@main.command("list-users")
@click.option("--limit", type=int, default=20)
@click.pass_context
def cmd_list_users(ctx: click.Context, limit: int) -> None:
    """List enabled member users (smoke-test for credentials)."""
    from .auth import MsalTokenProvider
    from .graph import GraphClient
    from .onedrive import UserResolver

    cfg = ctx.obj["cfg"]

    async def _run():
        tp = MsalTokenProvider(cfg.azure)
        async with GraphClient(tp, max_retries=cfg.execution.max_retries) as g:
            users = []
            async for u in UserResolver(g).list_all_members():
                users.append({"upn": u.get("userPrincipalName"), "id": u.get("id")})
                if len(users) >= limit:
                    break
            return users

    out = asyncio.run(_run())
    click.echo(json.dumps(out, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main(obj={})
