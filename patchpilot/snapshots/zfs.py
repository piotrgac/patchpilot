from patchpilot.snapshots.base import SnapshotInfo, SnapshotProvider
from patchpilot.ssh.client import SSHSession


class ZfsSnapshotProvider(SnapshotProvider):
    def __init__(self, session: SSHSession) -> None:
        super().__init__(session)
        self._pool_dataset: str | None = None

    @classmethod
    def technology_name(cls) -> str:
        return "zfs"

    async def _discover_root_dataset(self) -> str:
        if self._pool_dataset:
            return self._pool_dataset

        result = await self._session.run(
            "zfs list -H -o name,mountpoint 2>/dev/null | grep '\\s/$' | head -1",
            timeout=10,
        )
        if not result.stdout.strip():
            # Fallback: first pool
            result = await self._session.run(
                "zfs list -H -o name 2>/dev/null | head -1",
                timeout=10,
            )
        self._pool_dataset = result.stdout.strip()
        if not self._pool_dataset:
            raise RuntimeError("No ZFS dataset found mounted as root")
        return self._pool_dataset

    async def create(self, label: str, timeout: int = 120) -> SnapshotInfo:
        dataset = await self._discover_root_dataset()
        snapshot_name = f"patchpilot_{label}"

        await self._session.run(
            f"zfs snapshot {dataset}@{snapshot_name}",
            sudo=True,
            timeout=timeout,
        )

        return SnapshotInfo(
            name=snapshot_name,
            technology="zfs",
            path=f"{dataset}@{snapshot_name}",
        )

    async def restore(self, snapshot: SnapshotInfo, timeout: int = 300) -> bool:
        dataset = await self._discover_root_dataset()

        # For safety, stop critical services first
        await self._session.run(
            "systemctl stop nginx apache2 postgresql mysql 2>/dev/null; true",
            sudo=True,
            timeout=30,
        )

        await self._session.run(
            f"zfs rollback -r {dataset}@{snapshot.name}",
            sudo=True,
            timeout=timeout,
        )

        # Restart services
        await self._session.run(
            "systemctl restart nginx apache2 postgresql mysql 2>/dev/null; true",
            sudo=True,
            timeout=30,
        )

        return True

    async def delete(self, snapshot: SnapshotInfo) -> bool:
        dataset = await self._discover_root_dataset()
        await self._session.run(
            f"zfs destroy {dataset}@{snapshot.name}",
            sudo=True,
            timeout=60,
        )
        return True
