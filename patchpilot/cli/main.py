import asyncio
import json
import sys
from pathlib import Path

import click

from patchpilot.db.session import DatabaseManager
from patchpilot.inventory.models import InventoryLoader, InventoryModel
from patchpilot.rollout.executor import RolloutExecutor
from patchpilot.rollout.planner import RolloutPlanner
from patchpilot.ssh.client import SSHConnectionPool


def _load_inventory(inventory_path: str | None, env: str) -> InventoryModel:
    if inventory_path:
        return InventoryLoader.from_yaml(inventory_path)
    default = InventoryLoader.default_path(env)
    if default.exists():
        return InventoryLoader.from_yaml(str(default))
    possible = Path(f"{env}.yaml")
    if possible.exists():
        return InventoryLoader.from_yaml(str(possible))
    raise click.UsageError(
        f"Inventory file not found. Create one at ~/.config/patchpilot/{env}.yaml "
        f"or pass --inventory."
    )


def _get_db(inventory: InventoryModel) -> DatabaseManager:
    db_path = (
        (inventory.audit.db_path or DatabaseManager.default_path())
        if inventory.audit.enabled
        else DatabaseManager.default_path()
    )
    return DatabaseManager(db_path)


@click.group()
@click.version_option(version="0.1.0")
def cli() -> None:
    pass


@cli.command()
@click.argument("environment")
@click.option("--inventory", "-i", help="Path to inventory YAML file")
@click.option("--output", type=click.Choice(["table", "json", "yaml"]), default="table")
def plan(environment: str, inventory: str | None, output: str) -> None:
    """Analyze hosts and show what would be updated."""
    try:
        inv = _load_inventory(inventory, environment)
        planner = RolloutPlanner(inv)
        result = asyncio.get_event_loop().run_until_complete(planner.plan())
        asyncio.get_event_loop().run_until_complete(planner.close())

        if output == "json":
            data = {
                "environment": result.environment,
                "strategy": result.strategy_name,
                "total_packages": result.total_packages,
                "total_security": result.total_security,
                "reboot_count": result.reboot_count,
                "hosts": [
                    {
                        "name": ph.host.name,
                        "role": ph.host.role,
                        "distro": ph.distro.distro_id,
                        "packages": len(ph.available_updates),
                        "security": ph.security_updates,
                        "reboot": ph.reboot_required,
                        "snapshot": ph.snapshot_technology,
                        "error": ph.connection_error,
                    }
                    for ph in result.hosts
                ],
                "batches": [
                    [h.name for h in batch] for batch in result.batches
                ],
            }
            click.echo(json.dumps(data, indent=2))
            return

        click.secho(f"Rollout Plan: {result.environment}", bold=True)
        click.echo(f"Strategy: {result.strategy_name}")
        click.echo(f"Hosts: {len(result.hosts)}")
        click.echo(f"Packages requiring updates: {result.total_packages} (security: {result.total_security})")
        click.echo(f"Reboot required: {result.reboot_count} host(s)")
        click.echo("")

        click.secho("Execution order:", bold=True)
        for batch_idx, batch in enumerate(result.batches):
            label = "CANARY" if batch_idx == 0 and result.strategy_name == "canary" else f"BATCH {batch_idx}"
            for host in batch:
                ph = next((p for p in result.hosts if p.host.name == host.name), None)
                if ph:
                    status = "✓" if not ph.connection_error else "✗"
                    snap = f" ({ph.snapshot_technology or 'no snapshot'})" if not ph.connection_error else ""
                    click.echo(f"  {batch_idx + 1}. {host.name} — {host.role} [{status}]{snap}")
                else:
                    click.echo(f"  {batch_idx + 1}. {host.name}")

        errors = [ph for ph in result.hosts if ph.connection_error]
        if errors:
            click.echo("")
            click.secho("Errors:", fg="red", bold=True)
            for ph in errors:
                click.echo(f"  {ph.host.name}: {ph.connection_error}")

    except click.ClickException:
        raise
    except Exception as e:
        click.secho(f"Error: {e}", fg="red", err=True)
        sys.exit(1)


@cli.command()
@click.argument("environment")
@click.option("--inventory", "-i", help="Path to inventory YAML file")
@click.option("--strategy", type=click.Choice(["canary", "batch", "single"]), default=None)
@click.option("--auto-approve", is_flag=True, help="Skip manual confirmation")
@click.option("--dry-run", is_flag=True, help="Show what would be done without making changes")
@click.option("--limit", help="Only update hosts matching this glob pattern (e.g. api*)")
@click.option("--skip-health-checks", is_flag=True, help="Skip health checks after update")
@click.option("--force", is_flag=True, help="Override maintenance window and snapshot checks")
@click.option("--resume", help="Resume an interrupted rollout by ID")
def deploy(
    environment: str,
    inventory: str | None,
    strategy: str | None,
    auto_approve: bool,
    dry_run: bool,
    limit: str | None,
    skip_health_checks: bool,
    force: bool,
    resume: str | None,
) -> None:
    """Execute a rollout deployment."""
    try:
        inv = _load_inventory(inventory, environment)

        if strategy:
            inv.strategy.type = strategy  # type: ignore[assignment]

        pool = SSHConnectionPool(parallel_limit=inv.connection.parallel_limit)

        planner = RolloutPlanner(inv, ssh_pool=pool)
        plan_result = asyncio.get_event_loop().run_until_complete(planner.plan())

        if dry_run:
            click.secho("=== DRY RUN ===", bold=True)
            click.echo(f"Environment: {environment}")
            click.echo(f"Strategy: {plan_result.strategy_name}")
            click.echo(f"Hosts to update: {len(plan_result.hosts)}")
            for batch_idx, batch in enumerate(plan_result.batches):
                click.echo(f"  Batch {batch_idx + 1}: {', '.join(h.name for h in batch)}")
            click.echo("")
            click.echo("No changes were made.")
            return

        click.secho(f"Rollout plan for '{environment}':", bold=True)
        click.echo(f"  Hosts: {len(plan_result.hosts)}")
        click.echo(f"  Packages: {plan_result.total_packages}")
        click.echo(f"  Strategy: {plan_result.strategy_name}")
        click.echo("")

        if resume:
            click.echo(f"Resuming rollout: {resume}")
        elif not auto_approve:
            click.confirm("Proceed with deployment?", abort=True)

        db = _get_db(inv)
        asyncio.get_event_loop().run_until_complete(db.initialize())

        executor = RolloutExecutor(
            inventory=inv,
            plan=plan_result,
            db=db,
            ssh_pool=pool,
            auto_approve=auto_approve,
            resume_rollout_id=resume,
        )
        rollout_id = asyncio.get_event_loop().run_until_complete(executor.execute())
        asyncio.get_event_loop().run_until_complete(executor.close())

        click.secho(f"Rollout {'resumed' if resume else 'completed'}: {rollout_id}", bold=True, fg="green")

    except click.ClickException:
        raise
    except Exception as e:
        click.secho(f"Deploy failed: {e}", fg="red", err=True)
        sys.exit(1)


@cli.command()
@click.argument("rollout_id")
@click.option("--watch", is_flag=True, help="Continuously watch status")
@click.option("--format", "output_format", type=click.Choice(["table", "json"]), default="table")
def status(rollout_id: str, watch: bool, output_format: str) -> None:
    """Show the current status of a rollout."""
    import time
    from sqlalchemy import select

    from patchpilot.db.models import HealthCheckResult, Rollout, RolloutHost

    db = DatabaseManager(DatabaseManager.default_path())

    def _render() -> None:
        async def _fetch():
            async with db.session() as session:
                stmt = select(Rollout).where(Rollout.id == rollout_id)
                ro = (await session.execute(stmt)).scalar_one_or_none()
                if not ro:
                    click.echo(f"Rollout not found: {rollout_id}")
                    return

                stmt = (
                    select(RolloutHost)
                    .where(RolloutHost.rollout_id == rollout_id)
                    .order_by(RolloutHost.host_name)
                )
                hosts = (await session.execute(stmt)).scalars().all()

                if output_format == "json":
                    data = {
                        "id": ro.id,
                        "environment": ro.env_name,
                        "status": ro.status,
                        "strategy": ro.strategy_type,
                        "created_by": ro.created_by,
                        "started_at": str(ro.started_at) if ro.started_at else None,
                        "finished_at": str(ro.finished_at) if ro.finished_at else None,
                        "hosts": [
                            {
                                "name": h.host_name,
                                "role": h.host_role,
                                "status": h.status,
                                "snapshot": h.snapshot_type,
                                "error": h.error_log,
                            }
                            for h in hosts
                        ],
                    }
                    click.echo(json.dumps(data, indent=2))
                else:
                    click.secho(f"Rollout: {ro.id}", bold=True)
                    click.echo(f"  Environment: {ro.env_name}")
                    click.echo(f"  Status: {ro.status}")
                    click.echo(f"  Strategy: {ro.strategy_type}")
                    click.echo(f"  Started by: {ro.created_by}")
                    click.echo("")
                    click.secho("Hosts:", bold=True)
                    for h in hosts:
                        color = {
                            "healthy": "green",
                            "failed": "red",
                            "rolled_back": "yellow",
                            "in_progress": "blue",
                        }.get(h.status, "white")
                        click.secho(
                            f"  {h.host_name:20s} [{h.status:15s}] role={h.host_role}",
                            fg=color,
                        )
                        if h.error_log:
                            click.echo(f"    Error: {h.error_log[:100]}")

        asyncio.get_event_loop().run_until_complete(_fetch())

    _render()

    if watch:
        try:
            while True:
                time.sleep(5)
                click.clear()
                _render()
        except KeyboardInterrupt:
            pass


@cli.command()
@click.argument("rollout_id")
@click.option("--host", help="Only rollback this specific host")
@click.option("--all", "all_hosts", is_flag=True, help="Rollback all hosts")
@click.option("--ssh-user", default="root", help="SSH user for rollback connection")
@click.option("--ssh-key-path", type=click.Path(exists=True), help="SSH private key path")
def rollback(rollout_id: str, host: str | None, all_hosts: bool,
             ssh_user: str, ssh_key_path: str | None) -> None:
    """Rollback a rollout (restore snapshots)."""
    from patchpilot.rollback import RollbackService

    db = DatabaseManager(DatabaseManager.default_path())

    async def _do_rollback():
        await db.initialize()
        service = RollbackService(db)
        try:
            if host:
                results = [await service.rollback_host(
                    rollout_id, host, ssh_user=ssh_user, ssh_key_path=ssh_key_path,
                )]
            elif all_hosts or click.confirm(
                f"Rollback entire rollout '{rollout_id}'? "
                f"This will restore snapshots for all hosts.",
                abort=True,
            ):
                results = await service.rollback_all(
                    rollout_id, ssh_user=ssh_user, ssh_key_path=ssh_key_path,
                )
            else:
                return

            click.echo("")
            click.secho("Rollback results:", bold=True)
            for r in results:
                if r.success:
                    click.secho(f"  {r.host_name} ✓ {r.message}", fg="green")
                else:
                    click.secho(f"  {r.host_name} ✗ {r.message}", fg="red")

        finally:
            await service.close()

    asyncio.get_event_loop().run_until_complete(_do_rollback())


@cli.command()
@click.option("--environment", "-e", help="Filter by environment name")
@click.option("--limit", type=int, default=20, help="Number of recent rollouts")
def history(environment: str | None, limit: int) -> None:
    """Show recent rollout history."""
    from sqlalchemy import select

    from patchpilot.db.models import Rollout

    db = DatabaseManager(DatabaseManager.default_path())

    async def _fetch():
        async with db.session() as session:
            stmt = select(Rollout).order_by(Rollout.started_at.desc()).limit(limit)
            if environment:
                stmt = stmt.where(Rollout.env_name == environment)
            result = await session.execute(stmt)
            rollouts = result.scalars().all()

            if not rollouts:
                click.echo("No rollouts found.")
                return

            click.secho(f"{'ID':40s} {'Environment':15s} {'Status':15s} {'Date':25s} {'By':15s}", bold=True)
            click.echo("-" * 110)
            for r in rollouts:
                started = r.started_at.strftime("%Y-%m-%d %H:%M:%S") if r.started_at else "N/A"
                click.echo(
                    f"{r.id:40s} {r.env_name:15s} {r.status:15s} {started:25s} {r.created_by:15s}"
                )

    asyncio.get_event_loop().run_until_complete(_fetch())


@cli.command()
@click.argument("rollout_id")
@click.option("--format", "output_format", type=click.Choice(["json", "csv"]), default="json")
def audit(rollout_id: str, output_format: str) -> None:
    """Show detailed audit log for a rollout."""
    from sqlalchemy import select

    from patchpilot.db.models import AuditEvent

    db = DatabaseManager(DatabaseManager.default_path())

    async def _fetch():
        async with db.session() as session:
            stmt = (
                select(AuditEvent)
                .where(AuditEvent.rollout_id == rollout_id)
                .order_by(AuditEvent.id)
            )
            result = await session.execute(stmt)
            events = result.scalars().all()

            if not events:
                click.echo("No audit events found.")
                return

            if output_format == "json":
                data = [
                    {
                        "id": e.id,
                        "event_type": e.event_type,
                        "actor": e.actor,
                        "timestamp": str(e.timestamp),
                        "payload": e.payload_json,
                    }
                    for e in events
                ]
                click.echo(json.dumps(data, indent=2))
            else:
                click.echo("id,event_type,actor,timestamp,payload")
                for e in events:
                    payload = json.dumps(e.payload_json).replace('"', '""')
                    click.echo(f'{e.id},{e.event_type},{e.actor},{e.timestamp},"{payload}"')

    asyncio.get_event_loop().run_until_complete(_fetch())


@cli.command()
@click.argument("inventory_path", required=False, default=None)
def validate(inventory_path: str | None) -> None:
    """Validate an inventory YAML file syntax and semantics."""
    path = Path(inventory_path) if inventory_path else None
    if not path:
        click.echo("Searching for inventory files...")
        config_dir = Path.home() / ".config" / "patchpilot"
        if config_dir.exists():
            for f in config_dir.glob("*.yaml"):
                _validate_file(f)
        else:
            click.echo("No inventory file specified and no config directory found.")
        return

    _validate_file(path)


def _validate_file(path: Path) -> None:
    click.echo(f"Validating: {path}")
    try:
        inv = InventoryLoader.from_yaml(str(path))
        click.secho(f"  ✓ Valid: {inv.metadata_.name}", fg="green")
        click.echo(f"    Hosts: {len(inv.hosts)}")
        click.echo(f"    Strategy: {inv.strategy.type}")
    except Exception as e:
        click.secho(f"  ✗ {e}", fg="red", err=True)
        sys.exit(1)


@cli.group()
def config() -> None:
    """Manage PatchPilot configuration."""


@config.command()
@click.argument("environment")
def init(environment: str) -> None:
    """Create a default inventory YAML file for an environment."""
    config_dir = Path.home() / ".config" / "patchpilot"
    config_dir.mkdir(parents=True, exist_ok=True)
    dest = config_dir / f"{environment}.yaml"

    if dest.exists():
        click.confirm(f"Overwrite existing {dest}?", abort=True)

    template = f"""---
metadata:
  name: {environment}
  description: ""
  owner: ""

connection:
  ssh_user: deploy
  ssh_key_path: ~/.ssh/patchpilot_ed25519
  parallel_limit: 5

hosts:
  - name: host-01
    address: 10.0.0.1
    role: api
    tags: [canary-eligible]
  - name: host-02
    address: 10.0.0.2
    role: api
  - name: host-03
    address: 10.0.0.3
    role: database

strategy:
  type: canary
  canary:
    count: 1
    tag_filter: canary-eligible
  batch:
    size: 2

health_checks:
  global:
    - type: systemd
      service: ssh.service
  per_role:
    api:
      - type: http
        url: http://localhost:8080/health
        expected_status: 200
    database:
      - type: tcp
        host: localhost
        port: 5432

maintenance:
  timezone: UTC
  windows:
    - start: "23:00"
      end: "02:00"
      days: [saturday, sunday]

snapshot:
  preferred: auto
  on_unavailable: warn
"""
    dest.write_text(template.lstrip())
    click.secho(f"Created {dest}", fg="green")
    click.echo("Edit this file to match your infrastructure.")


@config.command()
@click.argument("environment")
def show(environment: str) -> None:
    """Show the current configuration for an environment."""
    try:
        inv = _load_inventory(None, environment)
        click.echo(inv.model_dump_json(indent=2))
    except Exception as e:
        click.secho(f"Error: {e}", fg="red", err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()
