import re
from typing import ClassVar

from patchpilot.packages.base import PackageManager, PackageUpdate, UpdateResult

_INST_RE = re.compile(
    r"^Inst\s+(\S+)\s+\[([^\]]+)\]\s+\(([^ ]+)", re.MULTILINE
)
_SECURITY_RE = re.compile(r"(security|ubuntu-security)", re.IGNORECASE)
_BOOT_RE = re.compile(r"linux-image-|linux-zen|linux-lts", re.IGNORECASE)


class AptPackageManager(PackageManager):
    DISTROS: ClassVar[list[str]] = ["ubuntu", "debian"]

    @classmethod
    def detect(cls, distro_id: str, version: str) -> bool:
        return distro_id.lower() in cls.DISTROS

    async def check_updates(self) -> list[PackageUpdate]:
        result = await self._session.run(
            "apt-get update -qq 2>/dev/null && "
            "apt-get --just-print upgrade 2>/dev/null | grep '^Inst '",
            sudo=True,
            timeout=120,
        )

        packages: list[PackageUpdate] = []
        for match in _INST_RE.finditer(result.stdout):
            name = match.group(1)
            current = match.group(2)
            rest = match.group(3)
            new_version = rest.split()[0] if rest.split() else rest
            is_security = bool(_SECURITY_RE.search(rest))
            packages.append(
                PackageUpdate(
                    name=name,
                    current_version=current,
                    new_version=new_version,
                    source=rest,
                    is_security=is_security,
                )
            )
        return packages

    async def apply_updates(self, dry_run: bool = False) -> UpdateResult:
        import asyncio
        import time

        if dry_run:
            updates = await self.check_updates()
            return UpdateResult(
                success=True,
                updated_packages=updates,
                stdout=f"Dry-run: {len(updates)} packages would be updated",
            )

        start = time.monotonic()

        # Pre-check reboot status before update
        reboot_before = await self.requires_reboot()

        result = await self._session.run(
            "DEBIAN_FRONTEND=noninteractive "
            "apt-get -y -o Dpkg::Options::=--force-confold upgrade 2>&1",
            sudo=True,
            timeout=600,
        )

        duration = time.monotonic() - start

        updated = self._parse_upgraded(result.stdout)
        failed = self._parse_failed(result.stdout)

        reboot_after = await self.requires_reboot()
        # Reboot is "new" if it wasn't needed before but is needed now
        fresh_reboot = reboot_after and not reboot_before

        return UpdateResult(
            success=result.ok,
            updated_packages=updated,
            failed_packages=failed,
            stdout=result.stdout,
            stderr=result.stderr,
            reboot_required=fresh_reboot,
            duration_seconds=duration,
        )

    async def requires_reboot(self) -> bool:
        result = await self._session.run(
            "test -f /var/run/reboot-required && echo YES || echo NO",
            sudo=True,
            timeout=10,
        )
        if "YES" in result.stdout:
            return True

        # Fallback: check if a newer kernel is installed than running
        result = await self._session.run(
            "dpkg --list 2>/dev/null | grep -E '^ii.*linux-image-' | awk '{print $3}' "
            "| sort -V | tail -1",
            sudo=True,
            timeout=10,
        )
        installed_kernel = result.stdout.strip()
        if not installed_kernel:
            return False

        uname = await self._session.run("uname -r", timeout=5)
        running = uname.stdout.strip()
        if not running:
            return False

        return installed_kernel[: len(running)] != running

    def _parse_upgraded(self, output: str) -> list[PackageUpdate]:
        packages: list[PackageUpdate] = []
        for line in output.splitlines():
            m = re.search(
                r"^Unpacking\s+(\S+)\s+\((\S+)", line
            )
            if m:
                packages.append(
                    PackageUpdate(
                        name=m.group(1),
                        current_version="",
                        new_version=m.group(2),
                    )
                )
        if not packages:
            for line in output.splitlines():
                m = re.search(
                    r"^(Preparing to unpack|Setting up)\s+(\S+)",
                    line,
                )
                if m:
                    packages.append(
                        PackageUpdate(
                            name=m.group(2) if m.lastindex == 2 else m.group(1),
                            current_version="",
                            new_version="",
                        )
                    )
        return packages

    def _parse_failed(self, output: str) -> list[str]:
        failed: list[str] = []
        for line in output.splitlines():
            if re.search(r"^E:|dpkg: error processing", line):
                m = re.search(r"package\s+(\S+)", line)
                if m:
                    failed.append(m.group(1))
        return failed
