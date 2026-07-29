from patchpilot.health.base import HealthCheck, HealthResult
from patchpilot.inventory.models import HealthCheckModel
from patchpilot.ssh.client import SSHSession


class HttpHealthCheck(HealthCheck):
    def __init__(self, config: HealthCheckModel) -> None:
        super().__init__(config)

    def type_name(self) -> str:
        return "http"

    async def check(self, session: SSHSession) -> HealthResult:
        url = self.config.url
        expected_status = self.config.expected_status

        result = await session.run(
            f"curl -s -o /dev/null -w '%{{http_code}}' "
            f"--connect-timeout {self.config.timeout} "
            f"{url}",
            timeout=self.config.timeout + 5,
        )

        status = result.stdout.strip()
        passed = status == str(expected_status)

        return HealthResult(
            passed=passed,
            check_type=self.type_name(),
            details=f"url={url} expected_status={expected_status} actual_status={status}",
        )
