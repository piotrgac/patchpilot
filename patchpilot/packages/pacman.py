from typing import ClassVar

from patchpilot.packages.base import PackageManager, PackageUpdate, UpdateResult


class PacmanPackageManager(PackageManager):
    DISTROS: ClassVar[list[str]] = ["arch"]

    @classmethod
    def detect(cls, distro_id: str, version: str) -> bool:
        return distro_id.lower() in cls.DISTROS

    async def check_updates(self) -> list[PackageUpdate]:
        result = await self._session.run(
            "pacman -Qu 2>/dev/null; true",
            sudo=True,
            timeout=60,
        )

        packages: list[PackageUpdate] = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                name = parts[0]
                versions = parts[1].split(" -> ")
                old_ver = versions[0] if len(versions) == 2 else ""
                new_ver = versions[-1]
                packages.append(
                    PackageUpdate(
                        name=name,
                        current_version=old_ver,
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
            "pacman -Syu --noconfirm 2>&1",
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
        # Check if kernel was updated but running kernel is different
        result = await self._session.run(
            "uname -r",
            timeout=5,
        )
        running = result.stdout.strip()

        result = await self._session.run(
            "pacman -Q linux 2>/dev/null | awk '{print $2}' || echo ''",
            sudo=True,
            timeout=10,
        )
        installed = result.stdout.strip()

        if not installed:
            return False

        # Arch kernel version format may differ from `uname -r`
        running_short = running.split("-")[0]
        return running_short not in installed
