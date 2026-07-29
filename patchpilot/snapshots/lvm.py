import re

from patchpilot.snapshots.base import SnapshotInfo, SnapshotProvider
from patchpilot.ssh.client import SSHSession


class LvmSnapshotProvider(SnapshotProvider):
    def __init__(self, session: SSHSession) -> None:
        super().__init__(session)
        self._root_lv: str | None = None
        self._root_vg: str | None = None

    @classmethod
    def technology_name(cls) -> str:
        return "lvm"

    async def _discover_root_lv(self) -> tuple[str, str]:
        if self._root_lv and self._root_vg:
            return self._root_lv, self._root_vg

        result = await self._session.run(
            "findmnt -n -o SOURCE / 2>/dev/null",
            timeout=10,
        )
        dev = result.stdout.strip()

        m = re.match(r"/dev/mapper/([\w\-]+)", dev)
        if not m:
            m = re.match(r"/dev/(\w+)/(\w+)", dev)

        if not m:
            raise RuntimeError(f"Cannot determine LVM root device from: {dev}")

        if "/dev/mapper/" in dev:
            parts = m.group(1).rsplit("-", 1)
            if len(parts) == 2:
                self._root_vg, self._root_lv = parts
            else:
                raise RuntimeError(f"Cannot parse dm name: {m.group(1)}")
        else:
            self._root_vg = m.group(1)
            self._root_lv = m.group(2)

        return self._root_lv, self._root_vg

    async def create(self, label: str, timeout: int = 120) -> SnapshotInfo:
        lv, vg = await self._discover_root_lv()
        snapshot_name = f"patchpilot_{label}"

        await self._session.run(
            f"lvcreate -L 5G -s -n {snapshot_name} /dev/{vg}/{lv}",
            sudo=True,
            timeout=timeout,
        )

        return SnapshotInfo(
            name=snapshot_name,
            technology="lvm",
            path=f"/dev/{vg}/{snapshot_name}",
            metadata={"vg": vg, "lv": lv},
        )

    async def restore(self, snapshot: SnapshotInfo, timeout: int = 300) -> bool:
        vg = snapshot.metadata.get("vg", "") if snapshot.metadata else ""
        if not vg:
            # Try to discover
            lv, vg = await self._discover_root_lv()

        await self._session.run(
            f"lvconvert --merge /dev/{vg}/{snapshot.name}",
            sudo=True,
            timeout=timeout,
        )

        # Merge happens on next activation (reboot)
        await self._session.run(
            "shutdown -r now",
            sudo=True,
            timeout=10,
        )

        return True

    async def delete(self, snapshot: SnapshotInfo) -> bool:
        vg = snapshot.metadata.get("vg", "") if snapshot.metadata else ""
        if not vg:
            lv, vg = await self._discover_root_lv()

        await self._session.run(
            f"lvremove -f /dev/{vg}/{snapshot.name}",
            sudo=True,
            timeout=60,
        )
        return True
