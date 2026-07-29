from patchpilot.health.base import HealthCheck, HealthResult
from patchpilot.inventory.models import HealthCheckModel
from patchpilot.ssh.client import SSHSession


class CommandHealthCheck(HealthCheck):
    def __init__(self, config: HealthCheckModel) -> None:
        super().__init__(config)

    def type_name(self) -> str:
        return "command"

    async def check(self, session: SSHSession) -> HealthResult:
        command = self.config.command or ""
        expected = self.config.expected_exit_code

        result = await session.run(
            command,
            timeout=self.config.timeout,
        )

        passed = result.exit_code == expected

        return HealthResult(
            passed=passed,
            check_type=self.type_name(),
            details=(
                f"command='{command[:80]}' "
                f"expected_exit_code={expected} "
                f"actual_exit_code={result.exit_code}"
            ),
        )
