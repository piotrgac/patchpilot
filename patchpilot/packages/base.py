from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from patchpilot.ssh.client import SSHSession


@dataclass
class PackageUpdate:
    name: str
    current_version: str
    new_version: str
    source: str = ""
    is_security: bool = False
    size_bytes: int | None = None


@dataclass
class UpdateResult:
    success: bool
    updated_packages: list[PackageUpdate] = field(default_factory=list)
    failed_packages: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    reboot_required: bool = False
    duration_seconds: float = 0.0


class PackageManager(ABC):
    def __init__(self, session: SSHSession) -> None:
        self._session = session

    @abstractmethod
    async def check_updates(self) -> list[PackageUpdate]:
        pass

    @abstractmethod
    async def apply_updates(self, dry_run: bool = False) -> UpdateResult:
        pass

    @abstractmethod
    async def requires_reboot(self) -> bool:
        pass

    @classmethod
    @abstractmethod
    def detect(cls, distro_id: str, version: str) -> bool:
        pass
