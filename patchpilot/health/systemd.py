from patchpilot.health.base import HealthCheck, HealthResult
from patchpilot.inventory.models import HealthCheckModel
from patchpilot.ssh.client import SSHSession


class SystemdHealthCheck(HealthCheck):
    def __init__(self, config: HealthCheckModel) -> None:
        super().__init__(config)

    def type_name(self) -> str:
        return "systemd"

    async def check(self, session: SSHSession) -> HealthResult:
        service = self.config.service
        expected = self.config.state or "active"

        result = await session.run(
            f"systemctl is-active {service}",
            sudo=True,
            timeout=self.config.timeout,
        )

        actual = result.stdout.strip()
        passed = actual == expected

        return HealthResult(
            passed=passed,
            check_type=self.type_name(),
            details=f"service={service} expected={expected} actual={actual}",
        )
