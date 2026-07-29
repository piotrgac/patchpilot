import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from patchpilot.db.models import Rollout, RolloutHost
from patchpilot.db.session import DatabaseManager
from patchpilot.snapshots.base import SnapshotInfo, SnapshotProvider
from patchpilot.ssh.client import SSHConnectionPool, SSHSession

logger = logging.getLogger(__name__)


class RollbackError(Exception):
    pass


class RollbackHostResult:
    def __init__(self, host_name: str, success: bool, message: str = "") -> None:
        self.host_name = host_name
        self.success = success
        self.message = message


class RollbackService:
    def __init__(self, db: DatabaseManager) -> None:
        self.db = db
        self._pool = SSHConnectionPool(parallel_limit=5)

    async def rollback_all(
        self,
        rollout_id: str,
        ssh_user: str = "root",
        ssh_key_path: str | None = None,
    ) -> list[RollbackHostResult]:
        async with self.db.session() as session:
            stmt = select(Rollout).where(Rollout.id == rollout_id)
            result = await session.execute(stmt)
            rollout = result.scalar_one_or_none()

            if not rollout:
                raise RollbackError(f"Rollout not found: {rollout_id}")

            stmt = (
                select(RolloutHost)
                .where(RolloutHost.rollout_id == rollout_id)
                .where(RolloutHost.snapshot_name.isnot(None))
            )
            result = await session.execute(stmt)
            hosts = result.scalars().all()

            if not hosts:
                raise RollbackError(
                    f"No hosts with snapshots found in rollout {rollout_id}"
                )

        results: list[RollbackHostResult] = []
        for host in hosts:
            try:
                await self._rollback_host(host, ssh_user, ssh_key_path)
                results.append(RollbackHostResult(host.host_name, True, "Rolled back"))
            except RollbackError as e:
                results.append(RollbackHostResult(host.host_name, False, str(e)))
            except Exception as e:
                logger.exception("Rollback failed for %s", host.host_name)
                results.append(RollbackHostResult(
                    host.host_name, False, f"Unexpected error: {e}",
                ))

        return results

    async def rollback_host(
        self,
        rollout_id: str,
        host_name: str,
        ssh_user: str = "root",
        ssh_key_path: str | None = None,
    ) -> RollbackHostResult:
        async with self.db.session() as session:
            stmt = select(RolloutHost).where(
                RolloutHost.rollout_id == rollout_id,
                RolloutHost.host_name == host_name,
            )
            result = await session.execute(stmt)
            host = result.scalar_one_or_none()

            if not host:
                return RollbackHostResult(
                    host_name, False,
                    f"Host {host_name} not found in rollout {rollout_id}",
                )
            if not host.snapshot_name:
                return RollbackHostResult(
                    host_name, False,
                    f"No snapshot available for {host_name}",
                )

        try:
            await self._rollback_host(host, ssh_user, ssh_key_path)
            return RollbackHostResult(host_name, True, "Rolled back")
        except RollbackError as e:
            return RollbackHostResult(host_name, False, str(e))

    async def _rollback_host(
        self,
        host: RolloutHost,
        ssh_user: str,
        ssh_key_path: str | None,
    ) -> None:
        snapshot_type = host.snapshot_type
        if not snapshot_type or snapshot_type == "none":
            raise RollbackError(f"No snapshot technology for {host.host_name}")

        session = SSHSession(
            host=host.address,
            username=ssh_user,
            client_keys=[Path(ssh_key_path)] if ssh_key_path else [],
            timeout=30,
        )
        try:
            await session.connect()
        except Exception as e:
            raise RollbackError(
                f"Cannot connect to {host.host_name} ({host.address}): {e}"
            )

        provider = self._resolve_provider(snapshot_type, session)
        if not provider:
            raise RollbackError(
                f"Unsupported snapshot technology: {snapshot_type}"
            )

        # Reconstruct SnapshotInfo from DB data
        snapshot_info = SnapshotInfo(
            name=host.snapshot_name or "",
            technology=snapshot_type,
        )

        logger.info(
            "Restoring snapshot %s (%s) on %s",
            host.snapshot_name, snapshot_type, host.host_name,
        )

        try:
            success = await provider.restore(snapshot_info)
            if not success:
                raise RollbackError(
                    f"Failed to restore snapshot {host.snapshot_name} on {host.host_name}"
                )
        except NotImplementedError:
            raise RollbackError(
                f"Rollback of {snapshot_type} snapshots is not implemented "
                f"for {host.host_name}. Manual intervention required."
            )
        except Exception as e:
            raise RollbackError(
                f"Failed to restore snapshot on {host.host_name}: {e}"
            )

        # Update DB
        async with self.db.session() as s:
            stmt = select(RolloutHost).where(RolloutHost.id == host.id)
            result = await s.execute(stmt)
            db_host = result.scalar_one()
            db_host.status = "rolled_back"
            db_host.error_log = (
                f"Rolled back via CLI at {datetime.utcnow().isoformat()}"
            )
            await s.commit()

    def _resolve_provider(self, tech: str, session: SSHSession) -> SnapshotProvider | None:
        if tech == "zfs":
            from patchpilot.snapshots.zfs import ZfsSnapshotProvider
            return ZfsSnapshotProvider(session)
        elif tech == "lvm":
            from patchpilot.snapshots.lvm import LvmSnapshotProvider
            return LvmSnapshotProvider(session)
        elif tech == "btrfs":
            from patchpilot.snapshots.btrfs import BtrfsSnapshotProvider
            return BtrfsSnapshotProvider(session)
        return None

    async def close(self) -> None:
        await self._pool.close_all()
