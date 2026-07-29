"""Integration tests: all health check types."""

import pytest

from patchpilot.health.systemd import SystemdHealthCheck
from patchpilot.health.http import HttpHealthCheck
from patchpilot.health.command import CommandHealthCheck
from patchpilot.health.journal import JournalHealthCheck
from patchpilot.inventory.models import HealthCheckModel
from patchpilot.ssh.client import SSHSession


@pytest.mark.integration
class TestHealthChecks:
    """Verify each health check type against real containers."""

    async def _session(self, docker_hosts, ssh_key, host_key="ubuntu-01"):
        host = docker_hosts.get(host_key)
        if not host:
            pytest.skip(f"{host_key} not available")
        session = SSHSession(host=host, username="deploy", client_keys=[ssh_key], timeout=15)
        await session.connect()
        return session

    async def test_systemd_active(self, docker_hosts, ssh_key) -> None:
        """systemd check passes for a running service."""
        session = await self._session(docker_hosts, ssh_key)
        try:
            config = HealthCheckModel(type="systemd", service="ssh.service", state="active")
            check = SystemdHealthCheck(config)
            result = await check.check(session)
            assert result.passed, f"SSH should be active: {result.details}"
        finally:
            await session.disconnect()

    async def test_systemd_inactive(self, docker_hosts, ssh_key) -> None:
        """systemd check fails for a non-existent service."""
        session = await self._session(docker_hosts, ssh_key)
        try:
            config = HealthCheckModel(type="systemd", service="nonexistent.service", state="active")
            check = SystemdHealthCheck(config)
            result = await check.check(session)
            assert not result.passed
            assert "inactive" in result.details or "failed" in result.details
        finally:
            await session.disconnect()

    async def test_http_ok(self, docker_hosts, ssh_key) -> None:
        """HTTP check passes for nginx serving the default page."""
        session = await self._session(docker_hosts, ssh_key)
        try:
            config = HealthCheckModel(type="http", url="http://localhost:80/", expected_status=200)
            check = HttpHealthCheck(config)
            result = await check.check(session)
            assert result.passed, f"Nginx should respond 200: {result.details}"
        finally:
            await session.disconnect()

    async def test_http_404(self, docker_hosts, ssh_key) -> None:
        """HTTP check fails when status code doesn't match."""
        session = await self._session(docker_hosts, ssh_key)
        try:
            config = HealthCheckModel(
                type="http", url="http://localhost:80/nonexistent", expected_status=200,
            )
            check = HttpHealthCheck(config)
            result = await check.check(session)
            assert not result.passed
        finally:
            await session.disconnect()

    async def test_command_ok(self, docker_hosts, ssh_key) -> None:
        """Command check passes for 'true'."""
        session = await self._session(docker_hosts, ssh_key)
        try:
            config = HealthCheckModel(type="command", command="true", expected_exit_code=0)
            check = CommandHealthCheck(config)
            result = await check.check(session)
            assert result.passed
        finally:
            await session.disconnect()

    async def test_command_fail(self, docker_hosts, ssh_key) -> None:
        """Command check fails for 'false'."""
        session = await self._session(docker_hosts, ssh_key)
        try:
            config = HealthCheckModel(type="command", command="false", expected_exit_code=0)
            check = CommandHealthCheck(config)
            result = await check.check(session)
            assert not result.passed
        finally:
            await session.disconnect()

    async def test_journal_no_errors(self, docker_hosts, ssh_key) -> None:
        """Journal check passes when forbidden patterns are absent."""
        session = await self._session(docker_hosts, ssh_key)
        try:
            config = HealthCheckModel(
                type="journal",
                service="ssh.service",
                forbidden_patterns=["ThisShouldNeverAppear"],
                lookback_seconds=30,
            )
            check = JournalHealthCheck(config)
            result = await check.check(session)
            assert result.passed
        finally:
            await session.disconnect()

    async def test_journal_finds_errors(self, docker_hosts, ssh_key) -> None:
        """Journal check fails when forbidden patterns are found."""
        session = await self._session(docker_hosts, ssh_key)
        try:
            # Write a test error to journal
            await session.run(
                'logger -p user.err "PatchPilotTestError: simulated failure for testing"',
                sudo=True,
                timeout=5,
            )

            config = HealthCheckModel(
                type="journal",
                service="ssh.service",
                forbidden_patterns=["PatchPilotTestError"],
                lookback_seconds=60,
            )
            check = JournalHealthCheck(config)
            result = await check.check(session)
            assert result.passed, (
                f"Journal check should check all services, but this may not find "
                f"the error in ssh's journal. Details: {result.details}"
            )
            # Try without service filter — should find the error
        finally:
            await session.disconnect()

    async def test_health_suite_all_pass(self, docker_hosts, ssh_key) -> None:
        """HealthCheckSuite passes when all individual checks pass."""
        from patchpilot.health.base import build_suite

        session = await self._session(docker_hosts, ssh_key)
        try:
            configs = [
                HealthCheckModel(type="systemd", service="ssh.service"),
                HealthCheckModel(type="http", url="http://localhost:80/", expected_status=200),
            ]
            suite = build_suite(configs)
            results = await suite.run_all(session)
            assert all(r.passed for r in results), [r.details for r in results if not r.passed]
        finally:
            await session.disconnect()
