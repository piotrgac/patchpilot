from abc import ABC, abstractmethod
from dataclasses import dataclass

from patchpilot.ssh.client import SSHSession


@dataclass
class SnapshotInfo:
    name: str
    technology: str  # "btrfs", "lvm", "zfs"
    path: str = ""
    size_bytes: int | None = None
    metadata: dict | None = None


class SnapshotProvider(ABC):
    def __init__(self, session: SSHSession) -> None:
        self._session = session

    @abstractmethod
    async def create(self, label: str, timeout: int = 120) -> SnapshotInfo:
        pass

    @abstractmethod
    async def restore(self, snapshot: SnapshotInfo, timeout: int = 300) -> bool:
        pass

    @abstractmethod
    async def delete(self, snapshot: SnapshotInfo) -> bool:
        pass

    @classmethod
    @abstractmethod
    def technology_name(cls) -> str:
        pass
