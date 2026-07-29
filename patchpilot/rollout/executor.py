import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchpilot.audit.logger import AuditLogger
from patchpilot.db.models import (
    HealthCheckResult as DBHealthCheckResult,
)
from patchpilot.db.models import (
    Rollout,
    RolloutHost,
    RolloutStep,
)
from patchpilot.db.session import DatabaseManager
from patchpilot.health.base import HealthResult, build_suite
from patchpilot.inventory.models import HostModel, InventoryModel
from patchpilot.maintenance.window import MaintenanceWindow
from patchpilot.packages.apt import AptPackageManager
from patchpilot.packages.base import PackageManager
from patchpilot.packages.dnf import DnfPackageManager
from patchpilot.packages.pacman import PacmanPackageManager
from patchpilot.rollout.planner import PlannedHost, RolloutPlan
from patchpilot.rollout.state_machine import (
    HostState,
    RolloutState,
)
from patchpilot.snapshots.base import SnapshotInfo, SnapshotProvider
from patchpilot.snapshots.detector import SnapshotDetector
from patchpilot.ssh.client import (
    RetryableSSHClient,
    SSHConnectionPool,
    SSHError,
)

logger = logging.getLogger(__name__)


def _resolve_pm(distro_id: str) -> type[PackageManager] | None:
    for cls in [AptPackageManager, DnfPackageManager, PacmanPackageManager]:
        if cls.detect(distro_id, ""):
            return cls
    return None


class RolloutExecutor:
    TERMINAL_HOST_STATUSES = {HostState.HEALTHY, HostState.ROLLED_BACK, HostState.SKIPPED}
    SKIPPABLE_STEP_STATUSES = {"completed", "skipped"}

    def __init__(
        self,
        inventory: InventoryModel,
        plan: RolloutPlan,
        db: DatabaseManager,
        ssh_pool: SSHConnectionPool | None = None,
        auto_approve: bool = False,
        resume_rollout_id: str | None = None,
    ) -> None:
        self.inventory = inventory
        self.plan = plan
        self.db = db
        self._pool = ssh_pool or SSHConnectionPool(
            parallel_limit=inventory.connection.parallel_limit
        )
        self._retry_client = RetryableSSHClient(
            pool=self._pool,
            max_attempts=inventory.connection.retry.max_attempts,
            backoff=inventory.connection.retry.backoff_seconds,
        )
        self.auto_approve = auto_approve
        self.rollout_id = resume_rollout_id
        self._audit: AuditLogger
        self._resume_mode = resume_rollout_id is not None
        if resume_rollout_id:
            self._audit = AuditLogger(db, resume_rollout_id)
        else:
            self._audit = AuditLogger(db, "")  # set properly in execute()

    @property
    def _conn_settings(self) -> dict[str, Any]:
        c = self.inventory.connection
        keys = [c.ssh_key_path] if c.ssh_key_path else []
        return {
            "port": 22,
            "username": c.ssh_user,
            "client_keys": [k for k in keys if k],
            "timeout": c.ssh_timeout,
        }

    def _host_settings(self, host_name: str) -> dict[str, Any]:
        for h in self.inventory.hosts:
            if h.name == host_name:
                c = self.inventory.connection
                keys: list[Path] = []
                if h.ssh_key_path:
                    keys.append(h.ssh_key_path)
                elif c.ssh_key_path:
                    keys.append(c.ssh_key_path)
                return {
                    "port": h.ssh_port,
                    "username": h.ssh_user or c.ssh_user,
                    "client_keys": keys,
                    "timeout": c.ssh_timeout,
                }
        return self._conn_settings

    async def execute(self) -> str:
        mw = MaintenanceWindow.from_config(self.inventory.maintenance)
        if not mw.is_open() and not self.inventory.maintenance.deploy_outside_window:
            raise RuntimeError(
                "Current time is outside the configured maintenance window. "
                "Use --force to override."
            )

        async with self.db.session() as session:
            if self._resume_mode and self.rollout_id:
                # Resume existing rollout
                stmt = select(Rollout).where(Rollout.id == self.rollout_id)
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()
                if not existing:
                    raise RuntimeError(f"Rollout to resume not found: {self.rollout_id}")

                existing.status = RolloutState.IN_PROGRESS.value
                existing.maintenance_window_ok = mw.is_open()

                # Load existing host records and determine which are done
                host_stmt = select(RolloutHost).where(
                    RolloutHost.rollout_id == self.rollout_id
                )
                host_result = await session.execute(host_stmt)
                existing_hosts = host_result.scalars().all()
                terminal_hosts = {
                    h.host_name
                    for h in existing_hosts
                    if h.status in {s.value for s in self.TERMINAL_HOST_STATUSES}
                }

                logger.info(
                    "Resuming rollout %s: %d hosts done, skipping %d terminal",
                    self.rollout_id,
                    len(existing_hosts) - len(terminal_hosts),
                    len(terminal_hosts),
                )

                # Filter plan batches: remove hosts that are already in terminal state
                filtered_batches = []
                for batch in self.plan.batches:
                    filtered = [h for h in batch if h.name not in terminal_hosts]
                    if filtered:
                        filtered_batches.append(filtered)
                self.plan.batches = filtered_batches

            else:
                # Create new rollout
                if not await self._acquire_lock(session):
                    raise RuntimeError(
                        f"Rollout already in progress for environment "
                        f"'{self.inventory.metadata_.name}'"
                    )

                rollout = Rollout(
                    env_name=self.inventory.metadata_.name,
                    strategy_type=self.plan.strategy_name,
                    status=RolloutState.IN_PROGRESS.value,
                    created_by=_get_user(),
                    started_at=datetime.utcnow(),
                    maintenance_window_ok=mw.is_open(),
                    plan_json={
                        "total_packages": self.plan.total_packages,
                        "total_security": self.plan.total_security,
                        "reboot_count": self.plan.reboot_count,
                        "batches": [
                            [h.name for h in batch]
                            for batch in self.plan.batches
                        ],
                    },
                )
                session.add(rollout)
                await session.flush()
                self.rollout_id = rollout.id

                # Create host records
                for ph in self.plan.hosts:
                    rh = RolloutHost(
                        rollout_id=rollout.id,
                        host_name=ph.host.name,
                        host_role=ph.host.role,
                        address=str(ph.host.address),
                        status=HostState.PENDING.value,
                        snapshot_type=ph.snapshot_technology,
                        reboot_required=ph.reboot_required,
                    )
                    session.add(rh)

            await session.commit()

        if not self.rollout_id:
            raise RuntimeError("Rollout ID not set")

        if not self._resume_mode:
            self._audit = AuditLogger(self.db, self.rollout_id)
            await self._audit.log("rollout_started", {
                "env": self.inventory.metadata_.name,
                "strategy": self.plan.strategy_name,
                "hosts": [ph.host.name for ph in self.plan.hosts],
                "plan": self.plan,
            })

        if not self.plan.batches:
            logger.info("No pending hosts to process. Rollout already complete.")
            return self.rollout_id

        # Execute batches
        final_status = RolloutState.COMPLETED
        aborted_reason: str | None = None

        try:
            for batch_idx, batch in enumerate(self.plan.batches):
                logger.info(
                    "Processing batch %d/%d (%d hosts)",
                    batch_idx + 1, len(self.plan.batches), len(batch),
                )

                if not mw.is_open() and batch_idx > 0:
                    logger.warning(
                        "Maintenance window closed. Stopping after current batch."
                    )
                    final_status = RolloutState.FAILED
                    break

                tasks = [
                    self._execute_host(ph) for ph in self.plan.hosts
                    if ph.host in batch
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                has_failure = False
                for r in results:
                    if isinstance(r, Exception):
                        logger.error("Host execution failed with exception: %s", r)
                        has_failure = True
                    elif r is not None and not r:
                        has_failure = True

                if has_failure:
                    logger.warning("Batch %d has failures. Stopping rollout.", batch_idx + 1)
                    final_status = RolloutState.FAILED
                    break

        except Exception as e:
            logger.error("Rollout failed: %s", e)
            final_status = RolloutState.FAILED
            aborted_reason = str(e)

        await self._finalize(final_status, aborted_reason)
        return self.rollout_id

    async def _execute_host(self, ph: "PlannedHost") -> bool:
        if ph.connection_error:
            await self._set_host_status(
                ph.host.name, HostState.SKIPPED,
                error=f"Cannot connect: {ph.connection_error}",
            )
            return True  # skip, not a failure

        host = ph.host
        settings = self._host_settings(host.name)
        host_success = True
        created_snap: SnapshotInfo | None = None

        try:
            sess = await self._pool.acquire(
                host=str(host.address),
                **settings,
            )

            snapshot_provider: SnapshotProvider | None = None
            if ph.snapshot_available:
                await self._set_host_status(host.name, HostState.SNAPSHOTTING)
                detector = SnapshotDetector(sess)
                provider = await detector.detect()
                if provider:
                    import uuid
                    label = uuid.uuid4().hex[:12]
                    try:
                        snap = await provider.create(
                            label,
                            timeout=self.inventory.snapshot.timeout,
                        )
                        created_snap = snap
                        await self._add_step(
                            host.name, "snapshot", "completed",
                            snapshot_info_json={
                                "name": snap.name,
                                "technology": snap.technology,
                            },
                        )
                        snapshot_provider = provider
                        await self._set_host_snapshot(host.name, snap.name)
                        await self._audit.log("host_snapshot", {
                            "host": host.name,
                            "snapshot_name": snap.name,
                            "technology": snap.technology,
                        })
                    except Exception as e:
                        if self.inventory.snapshot.on_unavailable == "abort":
                            raise
                        logger.warning("Snapshot failed for %s: %s (continuing)", host.name, e)

            await self._set_host_status(host.name, HostState.UPDATING)
            pm_cls = _resolve_pm(ph.distro.distro_id)
            if pm_cls is None:
                raise RuntimeError(f"No package manager for {ph.distro.distro_id}")

            pm = pm_cls(sess)
            update_result = await pm.apply_updates()
            pkgs_json = [
                {"name": p.name, "new_version": p.new_version}
                for p in update_result.updated_packages
            ]
            await self._add_step(
                host.name, "update",
                "completed" if update_result.success else "failed",
                packages_json=pkgs_json,
                exit_code=0 if update_result.success else 1,
            )
            await self._audit.log("host_update", {
                "host": host.name,
                "packages_updated": len(update_result.updated_packages),
                "packages_failed": update_result.failed_packages,
            })

            if not update_result.success:
                raise RuntimeError(
                    f"Package update failed: {update_result.stderr[:200]}"
                )

            # Reboot if required
            if update_result.reboot_required or ph.reboot_required:
                await self._set_host_status(host.name, HostState.REBOOTING)
                await sess.run("shutdown -r 1", sudo=True, timeout=15)
                await sess.disconnect()
                await self._wait_for_ssh(host, settings)
                await self._add_step(host.name, "reboot", "completed")
                await self._audit.log("host_reboot", {
                    "host": host.name,
                })
                # Re-acquire session after reboot
                sess = await self._pool.acquire(
                    host=str(host.address),
                    **settings,
                )

            # Health checks
            await self._set_host_status(host.name, HostState.VERIFYING)
            health_configs = self.inventory.health_checks.for_role(host.role)
            suite = build_suite(health_configs)
            health_results = await suite.run_all(sess)

            # Store health check results in DB
            for hr in health_results:
                await self._add_health(host.name, hr)
                if self._audit:
                    await self._audit.log("host_health_check", {
                    "host": host.name,
                    "check_type": hr.check_type,
                    "passed": hr.passed,
                    "details": hr.details,
                })

            all_passed = all(r.passed for r in health_results)
            if all_passed:
                await self._set_host_status(host.name, HostState.HEALTHY)
                await self._add_step(host.name, "verify", "completed")
            else:
                await self._set_host_status(host.name, HostState.FAILED)
                await self._add_step(
                    host.name, "verify", "failed",
                    log_output=json.dumps([r.details for r in health_results if not r.passed]),
                )
                # Rollback — restore the original pre-update snapshot
                if snapshot_provider and created_snap:
                    await self._set_host_status(host.name, HostState.ROLLING_BACK)
                    try:
                        await snapshot_provider.restore(created_snap)
                        await self._set_host_status(host.name, HostState.ROLLED_BACK)
                        await self._add_step(host.name, "rollback", "completed")
                        await self._audit.log("host_rollback", {
                            "host": host.name,
                            "reason": "health check failed",
                            "snapshot": created_snap.name,
                        })
                    except Exception as rb_e:
                        logger.error("Rollback failed for %s: %s", host.name, rb_e)
                        await self._set_host_status(
                            host.name, HostState.FAILED,
                            error=f"Rollback failed: {rb_e}",
                        )
                host_success = False

        except SSHError as e:
            logger.error("SSH error on host %s: %s", host.name, e)
            await self._set_host_status(
                host.name, HostState.FAILED, error=str(e),
            )
            host_success = False
        except Exception as e:
            logger.exception("Unexpected error on host %s: %s", host.name, e)
            await self._set_host_status(
                host.name, HostState.FAILED, error=str(e),
            )
            host_success = False

        return host_success

    async def _wait_for_ssh(self, host: HostModel, settings: dict[str, Any], max_wait: int = 300) -> bool:
        logger.info("Waiting for SSH on %s after reboot (max %ds)", host.name, max_wait)
        deadline = time.monotonic() + max_wait
        attempt = 0

        while time.monotonic() < deadline:
            attempt += 1
            try:
                sess = await self._pool.acquire(
                    host=str(host.address),
                    **settings,
                )
                result = await sess.run("uptime", timeout=10)
                if result.ok:
                    logger.info("SSH restored on %s after %ds", host.name, attempt * 5)
                    return True
            except Exception:
                pass

            await asyncio.sleep(5)

        raise TimeoutError(
            f"Host {host.name} did not return SSH within {max_wait}s after reboot"
        )

    async def _set_host_status(
        self, host_name: str, state: HostState, error: str | None = None,
    ) -> None:
        async with self.db.session() as session:
            stmt = select(RolloutHost).where(
                RolloutHost.rollout_id == self.rollout_id,
                RolloutHost.host_name == host_name,
            )
            result = await session.execute(stmt)
            rh = result.scalar_one_or_none()
            if rh:
                rh.status = state.value
                if error:
                    rh.error_log = error
                await session.commit()

    async def _set_host_snapshot(self, host_name: str, snapshot_name: str) -> None:
        async with self.db.session() as session:
            stmt = select(RolloutHost).where(
                RolloutHost.rollout_id == self.rollout_id,
                RolloutHost.host_name == host_name,
            )
            result = await session.execute(stmt)
            rh = result.scalar_one_or_none()
            if rh:
                rh.snapshot_name = snapshot_name
                rh.snapshot_created_at = datetime.utcnow()
                await session.commit()

    async def _add_step(
        self, host_name: str, step_type: str, status: str,
        packages_json: list | None = None,
        snapshot_info_json: dict | None = None,
        exit_code: int | None = None,
        log_output: str | None = None,
    ) -> None:
        async with self.db.session() as session:
            stmt = select(RolloutHost).where(
                RolloutHost.rollout_id == self.rollout_id,
                RolloutHost.host_name == host_name,
            )
            result = await session.execute(stmt)
            rh = result.scalar_one_or_none()
            if rh:
                step = RolloutStep(
                    rollout_host_id=rh.id,
                    step_type=step_type,
                    status=status,
                    started_at=datetime.utcnow(),
                    finished_at=datetime.utcnow(),
                    exit_code=exit_code,
                    packages_json=packages_json,
                    snapshot_info_json=snapshot_info_json,
                    log_output=log_output,
                )
                session.add(step)
                await session.commit()

    async def _add_health(self, host_name: str, hr: HealthResult) -> None:
        async with self.db.session() as session:
            stmt = select(RolloutHost).where(
                RolloutHost.rollout_id == self.rollout_id,
                RolloutHost.host_name == host_name,
            )
            result = await session.execute(stmt)
            rh = result.scalar_one_or_none()
            if rh:
                db_hr = DBHealthCheckResult(
                    rollout_host_id=rh.id,
                    check_type=hr.check_type,
                    passed=hr.passed,
                    details=hr.details,
                    duration_ms=int(hr.duration_ms),
                    retry_number=hr.retry_number,
                )
                session.add(db_hr)
                await session.commit()

    async def _acquire_lock(self, session: AsyncSession) -> bool:
        from patchpilot.locking.db_lock import acquire_lock
        return await acquire_lock(
            session,
            self.inventory.metadata_.name,
            self.rollout_id or "",
        )

    async def _finalize(self, status: RolloutState, reason: str | None = None) -> None:
        if not self.rollout_id:
            return

        async with self.db.session() as s:
            stmt = select(Rollout).where(Rollout.id == self.rollout_id)
            result = await s.execute(stmt)
            rollout = result.scalar_one_or_none()
            if rollout:
                rollout.status = status.value
                rollout.finished_at = datetime.utcnow()
                if reason:
                    rollout.aborted_reason = reason
                await s.commit()

            # Release the environment lock
            from patchpilot.locking.db_lock import release_lock
            await release_lock(s, self.inventory.metadata_.name)

        if self._audit:
            await self._audit.log(
                "rollout_completed" if status == RolloutState.COMPLETED else "rollout_failed",
                {"status": status.value, "reason": reason},
            )

    async def close(self) -> None:
        await self._pool.close_all()


def _get_user() -> str:
    import os
    return os.environ.get("USER", os.environ.get("LOGNAME", "unknown"))
