import re
from typing import ClassVar

from patchpilot.packages.base import PackageManager, PackageUpdate, UpdateResult


class DnfPackageManager(PackageManager):
    DISTROS: ClassVar[list[str]] = [
        "fedora", "rhel", "rocky", "almalinux", "centos",
    ]

    @classmethod
    def detect(cls, distro_id: str, version: str) -> bool:
        return distro_id.lower() in cls.DISTROS

    async def check_updates(self) -> list[PackageUpdate]:
        result = await self._session.run(
            "dnf check-update --quiet 2>/dev/null; true",
            sudo=True,
            timeout=120,
        )

        packages: list[PackageUpdate] = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and "." in parts[0]:
                name = parts[0]
                cur_ver = parts[1].rsplit("-", 1)[0] if len(parts) > 2 else ""
                new_ver = parts[1]
                packages.append(
                    PackageUpdate(
                        name=name,
                        current_version=cur_ver,
                        new_version=new_ver,
                    )
                )
        return packages

    async def apply_updates(self, dry_run: bool = False) -> UpdateResult:
        import time

        if dry_run:
            updates = await self.check_updates()
            return UpdateResult(
                success=True,
                updated_packages=updates,
                stdout=f"Dry-run: {len(updates)} packages would be updated",
            )

        start = time.monotonic()
        result = await self._session.run(
            "dnf -y upgrade 2>&1",
            sudo=True,
            timeout=600,
        )
        duration = time.monotonic() - start

        reboot = await self.requires_reboot()

        return UpdateResult(
            success=result.ok,
            stdout=result.stdout,
            stderr=result.stderr,
            reboot_required=reboot,
            duration_seconds=duration,
        )

    async def requires_reboot(self) -> bool:
        result = await self._session.run(
            "needs-restarting -r 2>/dev/null && echo YES || echo NO",
            sudo=True,
            timeout=15,
        )
        if "YES" in result.stdout:
            return True

        return False
