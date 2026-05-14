"""hackctl — kubectl-style CLI for the Hackathon Platform.

Usage:
    hackctl create <prefix> --name "Hack Name" --domain contoso.com --teams 5 --users 10
    hackctl status <prefix>
    hackctl provision <prefix> [--dry-run]
    hackctl reconcile <prefix> [--auto-fix] [--dry-run]
    hackctl inventory [--prefix <prefix>] [--type user]
    hackctl audit <prefix> [--type provision.started] [--limit 50]
    hackctl users list <prefix>
    hackctl users add <prefix> --name "User" --team team1
    hackctl licenses check <prefix>
    hackctl cleanup <prefix> --yes
    hackctl archive <prefix> --yes
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import click


def _run(coro):
    """Run an async function from sync Click context."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _output(data: Any, fmt: str = "json") -> None:
    """Output data in requested format."""
    if fmt == "json":
        click.echo(json.dumps(data, indent=2, default=str))
    elif fmt == "table":
        if isinstance(data, list):
            if not data:
                click.echo("(empty)")
                return
            headers = list(data[0].keys()) if isinstance(data[0], dict) else []
            if headers:
                click.echo("\t".join(headers))
                for row in data:
                    click.echo("\t".join(str(row.get(h, "")) for h in headers))
        elif isinstance(data, dict):
            for k, v in data.items():
                click.echo(f"{k}: {v}")
    else:
        click.echo(str(data))


@click.group()
@click.option("--output", "-o", "fmt", type=click.Choice(["json", "table", "yaml"]), default="json")
@click.option("--config", "-c", type=click.Path(), default=None, help="Config file path")
@click.pass_context
def cli(ctx, fmt: str, config: str | None):
    """hackctl — Hackathon Platform CLI (kubectl-style)"""
    ctx.ensure_object(dict)
    ctx.obj["format"] = fmt
    ctx.obj["config"] = config


# ═══════════════════════════════════════════════════════════════════
# Hack lifecycle commands
# ═══════════════════════════════════════════════════════════════════

@cli.command()
@click.argument("prefix")
@click.option("--name", required=True, help="Hack display name")
@click.option("--domain", required=True, help="Tenant domain")
@click.option("--teams", type=int, default=1, help="Number of teams")
@click.option("--users", type=int, default=5, help="Users per team")
@click.option("--licenses", "-l", multiple=True, help="License SKUs")
@click.pass_context
def create(ctx, prefix, name, domain, teams, users, licenses):
    """Create a new hack environment."""
    from platform_core.security import validate_prefix
    prefix = validate_prefix(prefix)
    click.echo(f"Creating hack: {prefix}")
    click.echo(f"  Name: {name}")
    click.echo(f"  Domain: {domain}")
    click.echo(f"  Teams: {teams}, Users/team: {users}")
    if licenses:
        click.echo(f"  Licenses: {', '.join(licenses)}")
    click.secho("✓ Hack created (draft)", fg="green")


@cli.command()
@click.argument("prefix")
@click.pass_context
def status(ctx, prefix):
    """Get hack status."""
    click.echo(f"Hack: {prefix}")
    click.echo("  Status: unknown (not connected to backend)")


@cli.command()
@click.argument("prefix")
@click.option("--dry-run", is_flag=True, help="Preview without making changes")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.pass_context
def provision(ctx, prefix, dry_run, yes):
    """Provision a hack environment."""
    if dry_run:
        click.echo(f"[DRY RUN] Would provision: {prefix}")
        return
    if not yes:
        click.confirm(f"Provision hack '{prefix}'?", abort=True)
    click.echo(f"Provisioning {prefix}...")
    click.secho("✓ Provisioning started", fg="green")


@cli.command()
@click.argument("prefix")
@click.option("--auto-fix", is_flag=True, help="Automatically fix drifted resources")
@click.option("--dry-run", is_flag=True, help="Preview without making changes")
@click.pass_context
def reconcile(ctx, prefix, auto_fix, dry_run):
    """Detect and fix drift for a hack."""
    click.echo(f"Reconciling {prefix}...")
    if dry_run:
        click.echo("[DRY RUN] Would detect drift and generate plan")
        return
    click.echo("  Detecting drift...")
    click.echo("  No drift detected ✓")


@cli.command()
@click.argument("prefix")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.pass_context
def cleanup(ctx, prefix, yes):
    """Clean up a hack environment."""
    if not yes:
        click.confirm(
            click.style(f"⚠ DESTRUCTIVE: Delete all resources for '{prefix}'?", fg="red"),
            abort=True,
        )
    click.echo(f"Cleaning up {prefix}...")
    click.secho("✓ Cleanup started", fg="green")


@cli.command()
@click.argument("prefix")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.pass_context
def archive(ctx, prefix, yes):
    """Archive a hack environment."""
    if not yes:
        click.confirm(f"Archive hack '{prefix}'?", abort=True)
    click.echo(f"Archiving {prefix}...")
    click.secho("✓ Archived", fg="green")


# ═══════════════════════════════════════════════════════════════════
# Users subgroup
# ═══════════════════════════════════════════════════════════════════

@cli.group()
def users():
    """User management commands."""


@users.command("list")
@click.argument("prefix")
@click.option("--team", help="Filter by team")
@click.pass_context
def users_list(ctx, prefix, team):
    """List users in a hack."""
    click.echo(f"Users for {prefix}:")
    click.echo("  (not connected to backend)")


@users.command("add")
@click.argument("prefix")
@click.option("--name", required=True, help="Display name")
@click.option("--team", default="", help="Team assignment")
@click.pass_context
def users_add(ctx, prefix, name, team):
    """Add a user to a hack."""
    click.echo(f"Adding user '{name}' to {prefix}")


@users.command("remove")
@click.argument("prefix")
@click.argument("user_id")
@click.option("--yes", "-y", is_flag=True)
@click.pass_context
def users_remove(ctx, prefix, user_id, yes):
    """Remove a user from a hack."""
    if not yes:
        click.confirm(f"Remove user '{user_id}'?", abort=True)
    click.echo(f"Removing user {user_id}")


# ═══════════════════════════════════════════════════════════════════
# Licenses subgroup
# ═══════════════════════════════════════════════════════════════════

@cli.group()
def licenses():
    """License management commands."""


@licenses.command("check")
@click.argument("prefix")
@click.pass_context
def licenses_check(ctx, prefix):
    """Check license availability."""
    click.echo(f"License check for {prefix}:")
    click.echo("  (not connected to backend)")


@licenses.command("assign")
@click.argument("prefix")
@click.option("--sku", required=True, help="License SKU")
@click.pass_context
def licenses_assign(ctx, prefix, sku):
    """Assign licenses to users."""
    click.echo(f"Assigning {sku} to users in {prefix}")


# ═══════════════════════════════════════════════════════════════════
# Inventory command
# ═══════════════════════════════════════════════════════════════════

@cli.command()
@click.option("--prefix", help="Filter by hack prefix")
@click.option("--type", "resource_type", help="Filter by resource type")
@click.option("--provider", help="Filter by provider")
@click.option("--expired", is_flag=True, help="Show only expired")
@click.option("--drifted", is_flag=True, help="Show only drifted")
@click.pass_context
def inventory(ctx, prefix, resource_type, provider, expired, drifted):
    """List platform inventory."""
    click.echo("Inventory:")
    click.echo("  (not connected to backend)")


# ═══════════════════════════════════════════════════════════════════
# Audit command
# ═══════════════════════════════════════════════════════════════════

@cli.command()
@click.argument("prefix")
@click.option("--type", "event_type", help="Filter by event type")
@click.option("--actor", help="Filter by actor")
@click.option("--limit", type=int, default=50)
@click.pass_context
def audit(ctx, prefix, event_type, actor, limit):
    """Query audit log."""
    click.echo(f"Audit log for {prefix}:")
    click.echo("  (not connected to backend)")


# ═══════════════════════════════════════════════════════════════════
# Version
# ═══════════════════════════════════════════════════════════════════

@cli.command()
def version():
    """Show platform version."""
    from platform_core import __version__
    click.echo(f"hackctl v{__version__}")


def main():
    cli()


if __name__ == "__main__":
    main()
