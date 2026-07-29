import re

from patchpilot.snapshots.base import SnapshotProvider
from patchpilot.ssh.client import SSHSession


class SnapshotDetector:
    def __init__(self, session: SSHSession) -> None:
        self._session = session

    async def detect(self) -> SnapshotProvider | None:
        providers = [
            ("btrfs", self._check_btrfs),
            ("zfs", self._check_zfs),
            ("lvm", self._check_lvm),
        ]

        for name, check in providers:
            try:
                if await check():
                    return self._resolve_provider(name)
            except Exception:
                continue

        return None

    async def _check_btrfs(self) -> bool:
        result = await self._session.run(
            "findmnt -n -o FSTYPE / 2>/dev/null",
            timeout=5,
        )
        return result.stdout.strip() == "btrfs"

    async def _check_zfs(self) -> bool:
        result = await self._session.run(
            "zfs list -H 2>/dev/null | head -1",
            timeout=10,
        )
        return bool(result.stdout.strip())

    async def _check_lvm(self) -> bool:
        result = await self._session.run(
            "lvs --noheadings -o lv_name 2>/dev/null | head -1",
            timeout=10,
        )
        return bool(result.stdout.strip())

    def _resolve_provider(self, tech: str) -> SnapshotProvider | None:
        if tech == "btrfs":
            from patchpilot.snapshots.btrfs import BtrfsSnapshotProvider
            return BtrfsSnapshotProvider(self._session)
        elif tech == "zfs":
            from patchpilot.snapshots.zfs import ZfsSnapshotProvider
            return ZfsSnapshotProvider(self._session)
        elif tech == "lvm":
            from patchpilot.snapshots.lvm import LvmSnapshotProvider
            return LvmSnapshotProvider(self._session)
        return None

    @classmethod
    async def available_types(cls, session: SSHSession) -> list[str]:
        detector = cls(session)
        results: list[str] = []
        checks = [
            ("btrfs", detector._check_btrfs),
            ("zfs", detector._check_zfs),
            ("lvm", detector._check_lvm),
        ]
        for name, check in checks:
            try:
                if await check():
                    results.append(name)
            except Exception:
                continue
        return results
