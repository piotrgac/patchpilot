import re

from patchpilot.health.base import HealthCheck, HealthResult
from patchpilot.inventory.models import HealthCheckModel
from patchpilot.ssh.client import SSHSession


class JournalHealthCheck(HealthCheck):
    def __init__(self, config: HealthCheckModel) -> None:
        super().__init__(config)

    def type_name(self) -> str:
        return "journal"

    async def check(self, session: SSHSession) -> HealthResult:
        service = self.config.service
        lookback = self.config.lookback_seconds
        patterns = self.config.forbidden_patterns

        result = await session.run(
            f"journalctl -u {service} "
            f"--since '{lookback} seconds ago' "
            f"--no-pager 2>&1 | tail -200",
            sudo=True,
            timeout=self.config.timeout,
        )

        matches: list[str] = []
        for pattern in patterns:
            if re.search(pattern, result.stdout, re.IGNORECASE):
                matches.append(pattern)

        passed = len(matches) == 0

        return HealthResult(
            passed=passed,
            check_type=self.type_name(),
            details=(
                f"service={service} lookback={lookback}s "
                f"forbidden_matches={matches}"
                if matches
                else f"service={service} lookback={lookback}s no forbidden patterns"
            ),
        )
