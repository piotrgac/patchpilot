import asyncio
import logging
import shlex
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import asyncssh

logger = logging.getLogger(__name__)


class SSHError(Exception):
    pass


class SSHConnectionError(SSHError):
    pass


class SSHCommandError(SSHError):
    def __init__(self, command: str, result: "SSHResult") -> None:
        self.command = command
        self.result = result
        super().__init__(
            f"Command failed (exit={result.exit_code}): {command[:100]}"
        )


class SSHAuthenticationError(SSHError):
    pass


@dataclass
class SSHResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def raise_for_status(self) -> None:
        if not self.ok:
            raise SSHCommandError("", self)


class SSHSession:
    def __init__(
        self,
        host: str,
        port: int = 22,
        username: str | None = None,
        password: str | None = None,
        client_keys: list[Path] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._client_keys = client_keys or []
        self._timeout = timeout
        self._conn: asyncssh.SSHClientConnection | None = None

    async def connect(self) -> None:
        if self._conn is not None:
            return
        try:
            self._conn = await asyncssh.connect(
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                client_keys=[str(k.expanduser()) for k in self._client_keys],
                connect_timeout=self._timeout,
                known_hosts=None,
            )
        except asyncssh.misc.PermissionDenied as e:
            raise SSHAuthenticationError(
                f"Permission denied for {self._username}@{self._host}: {e}"
            ) from e
        except (OSError, asyncssh.Error) as e:
            raise SSHConnectionError(
                f"Cannot connect to {self._host}:{self._port}: {e}"
            ) from e

    async def disconnect(self) -> None:
        if self._conn is not None:
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.is_closed()

    async def run(
        self,
        command: str,
        sudo: bool = False,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> SSHResult:
        if not self.connected:
            raise SSHConnectionError(f"Not connected to {self._host}")

        effective_cmd = command
        if sudo:
            safe_cmd = shlex.quote(command)
            effective_cmd = f"sudo --non-interactive bash -c {safe_cmd}"

        effective_timeout = timeout or self._timeout
        start = asyncio.get_event_loop().time()

        try:
            assert self._conn is not None
            result = await asyncio.wait_for(
                self._conn.run(
                    effective_cmd,
                    check=False,
                    env=env,
                ),
                timeout=effective_timeout,
            )
        except TimeoutError as e:
            raise SSHConnectionError(
                f"Command timed out after {effective_timeout}s on {self._host}: {command[:100]}"
            ) from e
        except (asyncssh.ChannelOpenError, OSError) as e:
            raise SSHCommandError(
                command,
                SSHResult(exit_code=-1, stdout="", stderr=str(e), duration_ms=0),
            ) from e

        duration_ms = (asyncio.get_event_loop().time() - start) * 1000
        stdout_str = result.stdout if isinstance(result.stdout, str) else (result.stdout or b"").decode("utf-8", errors="replace")
        stderr_str = result.stderr if isinstance(result.stderr, str) else (result.stderr or b"").decode("utf-8", errors="replace")
        return SSHResult(
            exit_code=result.returncode or 0,
            stdout=stdout_str,
            stderr=stderr_str,
            duration_ms=duration_ms,
        )

    async def run_stream(
        self,
        command: str,
        sudo: bool = False,
        timeout: float | None = None,
    ) -> AsyncIterator[str]:
        if not self.connected:
            raise SSHConnectionError(f"Not connected to {self._host}")

        effective_cmd = command
        if sudo:
            safe_cmd = shlex.quote(command)
            effective_cmd = f"sudo --non-interactive bash -c {safe_cmd}"

        effective_timeout = timeout or self._timeout

        try:
            assert self._conn is not None
            async with asyncio.timeout(effective_timeout):
                async with self._conn.create_process(effective_cmd) as process:
                    async for line in process.stdout:
                        yield line.rstrip("\n")
        except TimeoutError:
            raise SSHConnectionError(
                f"Command stream timed out after {effective_timeout}s on {self._host}"
            ) from None

    async def put_file(self, local_path: Path, remote_path: str) -> None:
        if not self.connected or self._conn is None:
            raise SSHConnectionError(f"Not connected to {self._host}")
        async with self._conn.start_sftp_client() as sftp:
            await sftp.put(str(local_path), remote_path)

    async def get_file(self, remote_path: str, local_path: Path) -> None:
        if not self.connected or self._conn is None:
            raise SSHConnectionError(f"Not connected to {self._host}")
        async with self._conn.start_sftp_client() as sftp:
            await sftp.get(remote_path, str(local_path))


class SSHConnectionPool:
    def __init__(self, parallel_limit: int = 5) -> None:
        self._parallel_limit = parallel_limit
        self._semaphore = asyncio.Semaphore(parallel_limit)
        self._sessions: dict[str, SSHSession] = {}

    async def acquire(
        self,
        host: str,
        port: int = 22,
        username: str | None = None,
        password: str | None = None,
        client_keys: list[Path] | None = None,
        timeout: float = 30.0,
    ) -> SSHSession:
        async with self._semaphore:
            key = f"{username}@{host}:{port}"
            if key in self._sessions and self._sessions[key].connected:
                return self._sessions[key]

            session = SSHSession(
                host=host,
                port=port,
                username=username,
                password=password,
                client_keys=client_keys or [],
                timeout=timeout,
            )
            await session.connect()
            self._sessions[key] = session
            return session

    async def close_all(self) -> None:
        tasks = [s.disconnect() for s in self._sessions.values()]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._sessions.clear()

    @property
    def connected_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.connected)


class RetryableSSHClient:
    def __init__(
        self,
        pool: SSHConnectionPool,
        max_attempts: int = 3,
        backoff: float = 5.0,
    ) -> None:
        self._pool = pool
        self._max_attempts = max_attempts
        self._backoff = backoff

    async def run(
        self,
        host: str,
        command: str,
        sudo: bool = False,
        timeout: float = 30.0,
        port: int = 22,
        username: str | None = None,
        client_keys: list[Path] | None = None,
    ) -> SSHResult:
        last_error: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                session = await self._pool.acquire(
                    host=host,
                    port=port,
                    username=username,
                    client_keys=client_keys,
                    timeout=timeout,
                )
                return await session.run(command, sudo=sudo, timeout=timeout)
            except SSHAuthenticationError:
                raise
            except (SSHConnectionError, SSHCommandError) as e:
                last_error = e
                if attempt < self._max_attempts:
                    wait = self._backoff * attempt
                    logger.warning(
                        "SSH attempt %d/%d failed for %s, retrying in %.1fs: %s",
                        attempt,
                        self._max_attempts,
                        host,
                        wait,
                        e,
                    )
                    await asyncio.sleep(wait)
                continue

        raise SSHConnectionError(
            f"All {self._max_attempts} SSH attempts failed for {host}: {last_error}"
        ) from last_error

    async def close_all(self) -> None:
        await self._pool.close_all()
