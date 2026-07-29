"""Integration tests: SSH connectivity and basic command execution."""

import pytest
import pytest_asyncio

from patchpilot.ssh.client import SSHSession


@pytest.mark.integration
class TestSSHConnection:
    """Verify SSH connections to test containers."""

    async def test_connect_to_ubuntu(self, docker_hosts: dict[str, str], ssh_key) -> None:
        """SSH to the Ubuntu container and run a basic command."""
        host = docker_hosts.get("ubuntu-01")
        if not host:
            pytest.skip("ubuntu-01 container not available")

        session = SSHSession(
            host=host,
            username="deploy",
            client_keys=[ssh_key],
            timeout=15,
        )
        try:
            await session.connect()
            assert session.connected

            result = await session.run("echo hello")
            assert result.ok
            assert result.stdout.strip() == "hello"

            result = await session.run("cat /etc/os-release")
            assert "Ubuntu" in result.stdout

            result = await session.run("id")
            assert "deploy" in result.stdout
        finally:
            await session.disconnect()

    async def test_sudo_access(self, docker_hosts: dict[str, str], ssh_key) -> None:
        """Verify passwordless sudo works."""
        host = docker_hosts.get("ubuntu-01")
        if not host:
            pytest.skip("ubuntu-01 not available")

        session = SSHSession(host=host, username="deploy", client_keys=[ssh_key], timeout=15)
        try:
            await session.connect()
            result = await session.run("whoami", sudo=True)
            assert result.ok
            assert result.stdout.strip() == "root"
        finally:
            await session.disconnect()

    async def test_connect_all_hosts(self, docker_hosts: dict[str, str], ssh_key) -> None:
        """Connect to every container and verify basic system info."""
        for name, ip in docker_hosts.items():
            session = SSHSession(host=ip, username="deploy", client_keys=[ssh_key], timeout=15)
            try:
                await session.connect()
                result = await session.run("hostname")
                assert result.ok
                assert result.stdout.strip() == name, (
                    f"Hostname mismatch for {name}: got {result.stdout.strip()}"
                )
            finally:
                await session.disconnect()

    async def test_connection_timeout(self) -> None:
        """Connecting to a non-existent host should raise an error."""
        session = SSHSession(
            host="10.255.255.254",
            username="deploy",
            timeout=3,
        )
        with pytest.raises(Exception):
            await session.connect()
        await session.disconnect()
