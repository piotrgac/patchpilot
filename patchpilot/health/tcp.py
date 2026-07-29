from patchpilot.health.base import HealthCheck, HealthResult
from patchpilot.inventory.models import HealthCheckModel
from patchpilot.ssh.client import SSHSession


class TcpHealthCheck(HealthCheck):
    def __init__(self, config: HealthCheckModel) -> None:
        super().__init__(config)

    def type_name(self) -> str:
        return "tcp"

    async def check(self, session: SSHSession) -> HealthResult:
        host = self.config.host or "localhost"
        port = self.config.port

        result = await session.run(
            f"timeout {self.config.timeout} bash -c "
            f"'cat < /dev/tcp/{host}/{port} 2>/dev/null' "
            f"&& echo OK || echo FAIL",
            timeout=self.config.timeout + 5,
        )

        passed = "OK" in result.stdout
        details = f"host={host} port={port} {'open' if passed else 'closed'}"

        return HealthResult(
            passed=passed,
            check_type=self.type_name(),
            details=details,
        )
