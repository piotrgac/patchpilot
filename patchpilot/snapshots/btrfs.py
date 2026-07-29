import time
from datetime import datetime

from patchpilot.snapshots.base import SnapshotInfo, SnapshotProvider
from patchpilot.ssh.client import SSHSession


class BtrfsSnapshotProvider(SnapshotProvider):
    def __init__(self, session: SSHSession) -> None:
        super().__init__(session)

    @classmethod
    def technology_name(cls) -> str:
        return "btrfs"

    async def create(self, label: str, timeout: int = 120) -> SnapshotInfo:
        snapshot_name = f"patchpilot-{label}"
        # Determine root subvolume path
        result = await self._session.run(
            "findmnt -n -o SOURCE / 2>/dev/null",
            timeout=10,
        )
        source = result.stdout.strip()
        pool_dir = "/.patchpilot_snapshots"

        # Ensure snapshot directory exists
        await self._session.run(
            f"mkdir -p {pool_dir}",
            sudo=True,
            timeout=10,
        )

        await self._session.run(
            f"btrfs subvolume snapshot / {pool_dir}/{snapshot_name}",
            sudo=True,
            timeout=timeout,
        )

        return SnapshotInfo(
            name=snapshot_name,
            technology="btrfs",
            path=f"{pool_dir}/{snapshot_name}",
        )

    async def restore(self, snapshot: SnapshotInfo, timeout: int = 300) -> bool:
        # Btrfs online restore is non-trivial — requires reboot to a snapshot
        # or booting from a recovery medium.
        # This implementation creates an inverse snapshot or logs guidance.
        raise NotImplementedError(
            "Btrfs online rollback is not supported. "
            "Use grub-btrfs or boot from a recovery medium to restore: "
            f"btrfs subvolume set-default {snapshot.path} /"
        )

    async def delete(self, snapshot: SnapshotInfo) -> bool:
        await self._session.run(
            f"btrfs subvolume delete {snapshot.path}",
            sudo=True,
            timeout=60,
        )
        return True
