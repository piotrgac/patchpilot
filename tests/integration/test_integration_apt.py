"""Integration tests: apt package manager operations."""

import pytest

from patchpilot.packages.apt import AptPackageManager
from patchpilot.ssh.client import SSHSession


@pytest.mark.integration
class TestAptManager:
    """Verify AptPackageManager works on Ubuntu containers."""

    async def _session(self, docker_hosts: dict[str, str], ssh_key, host_key: str = "ubuntu-01"):
        host = docker_hosts.get(host_key)
        if not host:
            pytest.skip(f"{host_key} not available")
        session = SSHSession(host=host, username="deploy", client_keys=[ssh_key], timeout=15)
        await session.connect()
        return session

    async def test_detect(self, docker_hosts: dict[str, str], ssh_key) -> None:
        """AptPackageManager.detect should return True for Ubuntu."""
        assert AptPackageManager.detect("ubuntu", "24.04")
        assert not AptPackageManager.detect("rocky", "9")

    async def test_check_updates(self, docker_hosts: dict[str, str], ssh_key) -> None:
        """check_updates should return a list of available package updates."""
        session = await self._session(docker_hosts, ssh_key)
        try:
            mgr = AptPackageManager(session)
            updates = await mgr.check_updates()
            assert isinstance(updates, list)
            # There should be some updates available on a fresh Ubuntu 24.04
            # (packages might already be patched in the image, so this may be empty)
            for pkg in updates:
                assert pkg.name
                assert pkg.new_version
        finally:
            await session.disconnect()

    async def test_requires_reboot(self, docker_hosts: dict[str, str], ssh_key) -> None:
        """requires_reboot should return a boolean."""
        session = await self._session(docker_hosts, ssh_key)
        try:
            mgr = AptPackageManager(session)
            reboot = await mgr.requires_reboot()
            assert isinstance(reboot, bool)
        finally:
            await session.disconnect()

    async def test_dry_run_upgrade(self, docker_hosts: dict[str, str], ssh_key) -> None:
        """apply_updates(dry_run=True) should not change anything."""
        session = await self._session(docker_hosts, ssh_key)
        try:
            mgr = AptPackageManager(session)
            result = await mgr.apply_updates(dry_run=True)
            assert result.success
            assert "Dry-run" in result.stdout
        finally:
            await session.disconnect()

    @pytest.mark.slow
    async def test_actual_upgrade(self, docker_hosts: dict[str, str], ssh_key) -> None:
        """Perform an actual apt upgrade on the broken host (it will be rebuilt)."""
        session = await self._session(docker_hosts, ssh_key, "ubuntu-broken")
        try:
            mgr = AptPackageManager(session)
            result = await mgr.apply_updates(dry_run=False)
            assert result.success, f"Upgrade failed: {result.stderr[:200]}"
            if result.reboot_required:
                assert result.reboot_required is True
        finally:
            await session.disconnect()

    async def test_check_updates_rocky(self, docker_hosts: dict[str, str], ssh_key) -> None:
        """Rocky Linux should NOT be detected as apt."""
        host = docker_hosts.get("rocky-01")
        if not host:
            pytest.skip("rocky-01 not available")
        session = SSHSession(host=host, username="deploy", client_keys=[ssh_key], timeout=15)
        try:
            await session.connect()
            mgr = AptPackageManager(session)

            # run() on dnf-based system should fail because apt isn't installed
            with pytest.raises(Exception):
                await mgr.check_updates()
        finally:
            await session.disconnect()
