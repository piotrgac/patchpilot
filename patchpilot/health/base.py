from abc import ABC, abstractmethod
from dataclasses import dataclass

from patchpilot.inventory.models import HealthCheckModel
from patchpilot.ssh.client import SSHSession


@dataclass
class HealthResult:
    passed: bool
    check_type: str
    details: str = ""
    duration_ms: float = 0.0
    retry_number: int = 0


class HealthCheck(ABC):
    def __init__(self, config: HealthCheckModel) -> None:
        self.config = config

    @abstractmethod
    async def check(self, session: SSHSession) -> HealthResult:
        pass

    @abstractmethod
    def type_name(self) -> str:
        pass


class HealthCheckSuite:
    def __init__(self, checks: list[HealthCheck]) -> None:
        self._checks = checks
        self._last_results: list[HealthResult] = []

    async def run_all(self, session: SSHSession) -> list[HealthResult]:
        results: list[HealthResult] = []
        for check in self._checks:
            import asyncio
            import time

            for attempt in range(max(1, check.config.retries + 1)):
                start = time.monotonic()
                try:
                    result = await asyncio.wait_for(
                        check.check(session),
                        timeout=check.config.timeout,
                    )
                    result.duration_ms = (time.monotonic() - start) * 1000
                    result.retry_number = attempt
                except TimeoutError:
                    result = HealthResult(
                        passed=False,
                        check_type=check.type_name(),
                        details=f"Timed out after {check.config.timeout}s",
                        duration_ms=(time.monotonic() - start) * 1000,
                        retry_number=attempt,
                    )

                results.append(result)
                if result.passed:
                    break

                if attempt < check.config.retries:
                    await asyncio.sleep(5)

            if not results[-1].passed:
                self._last_results = results
                return results

        self._last_results = results
        return results

    @property
    def all_passed(self) -> bool:
        return len(self._last_results) > 0 and all(r.passed for r in self._last_results)


def build_suite(configs: list[HealthCheckModel]) -> HealthCheckSuite:
    from patchpilot.health.command import CommandHealthCheck
    from patchpilot.health.http import HttpHealthCheck
    from patchpilot.health.journal import JournalHealthCheck
    from patchpilot.health.systemd import SystemdHealthCheck
    from patchpilot.health.tcp import TcpHealthCheck

    type_map = {
        "systemd": SystemdHealthCheck,
        "http": HttpHealthCheck,
        "tcp": TcpHealthCheck,
        "command": CommandHealthCheck,
        "journal": JournalHealthCheck,
    }

    checks: list[HealthCheck] = []
    for cfg in configs:
        cls = type_map.get(cfg.type)
        if cls is None:
            raise ValueError(f"Unknown health check type: {cfg.type}")
        checks.append(cls(cfg))  # type: ignore[abstract]

    return HealthCheckSuite(checks)
