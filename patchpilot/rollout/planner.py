import asyncio
from dataclasses import dataclass, field

from patchpilot.inventory.models import HostModel, InventoryModel
from patchpilot.packages.base import PackageManager, PackageUpdate
from patchpilot.rollout.strategies import build_strategy
from patchpilot.snapshots.detector import SnapshotDetector
from patchpilot.ssh.client import SSHConnectionPool, SSHSession


@dataclass
class DistroInfo:
    distro_id: str
    version: str
    pretty_name: str = ""

    @classmethod
    def from_os_release(cls, content: str) -> "DistroInfo":
        data: dict[str, str] = {}
        for line in content.splitlines():
            if "=" in line:
                key, val = line.split("=", 1)
                data[key.strip()] = val.strip().strip('"').strip("'")
        return cls(
            distro_id=data.get("ID", "unknown"),
            version=data.get("VERSION_ID", "0"),
            pretty_name=data.get("PRETTY_NAME", data.get("ID", "unknown")),
        )


@dataclass
class PlannedHost:
    host: HostModel
    distro: DistroInfo
    package_manager_type: str
    available_updates: list[PackageUpdate] = field(default_factory=list)
    security_updates: int = 0
    reboot_required: bool = False
    snapshot_technology: str | None = None
    snapshot_available: bool = False
    execution_group: int = 0
    connection_error: str | None = None


@dataclass
class RolloutPlan:
    environment: str
    hosts: list[PlannedHost]
    batches: list[list[HostModel]]
    total_packages: int = 0
    total_security: int = 0
    reboot_count: int = 0
    strategy_name: str = ""


def _resolve_package_manager(distro_id: str, session: "SSHSession | None" = None) -> type[PackageManager] | None:  # noqa: ARG001
    from patchpilot.packages.apt import AptPackageManager
    from patchpilot.packages.dnf import DnfPackageManager
    from patchpilot.packages.pacman import PacmanPackageManager

    for mgr in [AptPackageManager, DnfPackageManager, PacmanPackageManager]:
        if mgr.detect(distro_id, ""):
            return mgr
    return None


class RolloutPlanner:
    def __init__(
        self,
        inventory: InventoryModel,
        ssh_pool: SSHConnectionPool | None = None,
    ) -> None:
        self.inventory = inventory
        self._pool = ssh_pool or SSHConnectionPool(
            parallel_limit=inventory.connection.parallel_limit
        )

    async def plan(self) -> RolloutPlan:

        hosts_to_plan = list(self.inventory.hosts)
        planned_hosts: list[PlannedHost] = []

        async def plan_host(host: HostModel) -> PlannedHost:
            try:
                session = await self._pool.acquire(
                    host=str(host.address),
                    port=host.ssh_port,
                    username=host.ssh_user or self.inventory.connection.ssh_user,
                    client_keys=(
                        [host.ssh_key_path] if host.ssh_key_path
                        else [self.inventory.connection.ssh_key_path]
                    ) if self.inventory.connection.ssh_key_path else None,
                    timeout=self.inventory.connection.ssh_timeout,
                )

                # Get distro info
                distro_result = await session.run(
                    "cat /etc/os-release 2>/dev/null || cat /usr/lib/os-release 2>/dev/null",
                    timeout=10,
                )
                distro = DistroInfo.from_os_release(distro_result.stdout)

                # Resolve package manager
                pm_cls = _resolve_package_manager(distro.distro_id, session)
                if pm_cls is None:
                    return PlannedHost(
                        host=host,
                        distro=distro,
                        package_manager_type="unknown",
                        connection_error=f"Unsupported distro: {distro.distro_id}",
                    )

                pm = pm_cls(session)
                updates = await pm.check_updates()
                reboot = await pm.requires_reboot()
                security_count = sum(1 for u in updates if u.is_security)

                # Detect snapshot
                snapshot_tech = await SnapshotDetector.available_types(session)

                return PlannedHost(
                    host=host,
                    distro=distro,
                    package_manager_type=pm_cls.__name__,
                    available_updates=updates,
                    security_updates=security_count,
                    reboot_required=reboot,
                    snapshot_technology=snapshot_tech[0] if snapshot_tech else None,
                    snapshot_available=len(snapshot_tech) > 0,
                )
            except Exception as e:
                return PlannedHost(
                    host=host,
                    distro=DistroInfo(distro_id="unknown", version=""),
                    package_manager_type="unknown",
                    connection_error=str(e),
                )

        tasks = [plan_host(h) for h in hosts_to_plan]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                continue
            planned_hosts.append(result)  # type: ignore[arg-type]

        # Build strategy and batches
        strategy = build_strategy(self.inventory.strategy, self.inventory)
        batches = strategy.group_hosts()

        # Assign execution groups based on batches
        host_to_group: dict[str, int] = {}
        for group_idx, batch in enumerate(batches):
            for h in batch:
                host_to_group[h.name] = group_idx

        for ph in planned_hosts:
            ph.execution_group = host_to_group.get(ph.host.name, 0)

        total_pkgs = sum(len(ph.available_updates) for ph in planned_hosts)
        total_sec = sum(ph.security_updates for ph in planned_hosts)
        reboot_count = sum(1 for ph in planned_hosts if ph.reboot_required)

        return RolloutPlan(
            environment=self.inventory.metadata_.name,
            hosts=planned_hosts,
            batches=batches,
            total_packages=total_pkgs,
            total_security=total_sec,
            reboot_count=reboot_count,
            strategy_name=str(strategy),
        )

    async def close(self) -> None:
        await self._pool.close_all()
