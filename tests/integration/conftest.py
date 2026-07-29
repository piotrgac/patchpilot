import logging
import subprocess
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
import yaml

from patchpilot.db.session import DatabaseManager
from patchpilot.inventory.models import InventoryLoader, InventoryModel
from patchpilot.ssh.client import SSHConnectionPool

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOCKER_NETWORK = "patchpilot_lab"


def _check_docker() -> None:
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=15)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pytest.skip("Docker is not available")


def _docker_run_cmd(name: str, ip: str, image: str) -> list[str]:
    return [
        "docker", "run", "-d",
        "--name", name,
        "--hostname", name,
        "--privileged",
        "--cgroupns=host",
        "--network", DOCKER_NETWORK,
        "--ip", ip,
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
        "--tmpfs", "/run/lock",
        "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw",
        "-e", "container=docker",
        "--stop-signal", "SIGRTMIN+3",
        image,
    ]


@pytest.fixture(scope="session")
def ssh_key(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate an ED25519 SSH key pair for test use."""
    key_dir = tmp_path_factory.mktemp("ssh_keys")
    key_path = key_dir / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-q"],
        check=True, capture_output=True, timeout=30,
    )
    key_path.chmod(0o600)
    return key_path


@pytest.fixture(scope="session")
def docker_hosts(ssh_key: Path) -> dict[str, str]:
    """Start Docker containers, copy SSH keys, return {container_name: ip_address}."""
    _check_docker()

    # Build images
    subprocess.run(
        ["docker", "build", "-t", "patchpilot-ubuntu", "-f", "docker/ubuntu/Dockerfile", "."],
        cwd=REPO_ROOT, check=True, timeout=300,
    )

    # Ensure network exists
    subprocess.run(
        ["docker", "network", "inspect", DOCKER_NETWORK],
        capture_output=True,
    )
    if subprocess.run(
        ["docker", "network", "ls", "--filter", f"name={DOCKER_NETWORK}", "--format", "{{.Name}}"],
        capture_output=True, text=True,
    ).stdout.strip() != DOCKER_NETWORK:
        subprocess.run(
            ["docker", "network", "create", "--driver", "bridge",
             "--subnet", "10.10.0.0/24", DOCKER_NETWORK],
            check=True, timeout=30,
        )

    # Define containers
    containers = [
        ("ubuntu-01", "10.10.0.11"),
        ("ubuntu-broken", "10.10.0.12"),
    ]

    hosts: dict[str, str] = {}
    for name, ip in containers:
        subprocess.run(_docker_run_cmd(name, ip, "patchpilot-ubuntu"), check=True, timeout=60)
        hosts[name] = ip

    # Wait for SSH and install public key
    pub_key = ssh_key.with_suffix(".pub").read_text().strip()

    for name in hosts:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            pg = subprocess.run(
                ["docker", "exec", name, "pgrep", "sshd"],
                capture_output=True, timeout=5,
            )
            if pg.returncode == 0:
                break
            time.sleep(3)
        else:
            _teardown_containers(list(hosts.keys()))
            pytest.fail(f"SSH did not start on {name} within 90s")

        # Install public key for deploy user
        subprocess.run(
            ["docker", "exec", "-i", name, "sh", "-c",
             "mkdir -p /home/deploy/.ssh && cat >> /home/deploy/.ssh/authorized_keys"],
            input=pub_key.encode(),
            check=True, timeout=10,
        )
        subprocess.run(
            ["docker", "exec", name, "sh", "-c",
             "chown -R deploy:deploy /home/deploy/.ssh && "
             "chmod 700 /home/deploy/.ssh && chmod 600 /home/deploy/.ssh/authorized_keys"],
            check=True, timeout=10,
        )
        logger.info("Host %s (%s) ready", name, hosts[name])

    yield hosts

    # Teardown
    _teardown_containers(list(hosts.keys()))


def _teardown_containers(names: list[str]) -> None:
    logger.info("Stopping containers...")
    for name in names:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


@pytest.fixture(scope="session")
def inventory_file(
    tmp_path_factory: pytest.TempPathFactory,
    ssh_key: Path,
    docker_hosts: dict[str, str],
) -> Path:
    """Generate a temporary inventory YAML pointing at the test containers."""
    hosts_config = []
    for name, ip in docker_hosts.items():
        tags = ["canary-eligible"] if "broken" not in name else []
        hosts_config.append({
            "name": name,
            "address": ip,
            "role": "api",
            "tags": tags,
        })

    inventory = {
        "metadata": {"name": "lab", "owner": "integration-test"},
        "connection": {
            "ssh_user": "deploy",
            "ssh_key_path": str(ssh_key),
            "ssh_timeout": 15,
            "parallel_limit": 3,
            "retry": {"max_attempts": 2, "backoff_seconds": 3},
        },
        "hosts": hosts_config,
        "strategy": {
            "type": "canary",
            "canary": {"count": 1, "tag_filter": "canary-eligible"},
            "batch": {"size": 2},
        },
        "health_checks": {
            "global": [{"type": "systemd", "service": "ssh.service"}],
            "per_role": {
                "api": [{"type": "http", "url": "http://localhost:80/", "expected_status": 200}],
            },
        },
        "snapshot": {"preferred": "auto", "on_unavailable": "warn"},
        "maintenance": {
            "timezone": "UTC",
            "windows": [
                {"start": "00:00", "end": "23:59",
                 "days": ["monday", "tuesday", "wednesday", "thursday",
                          "friday", "saturday", "sunday"]},
            ],
        },
        "audit": {"enabled": True},
        "metrics": {"enabled": False},
    }

    inv_dir = tmp_path_factory.mktemp("inventory")
    inv_path = inv_dir / "lab.yaml"
    with open(inv_path, "w") as f:
        yaml.dump(inventory, f, default_flow_style=False)

    return inv_path


@pytest_asyncio.fixture
async def inventory(inventory_file: Path) -> InventoryModel:
    return InventoryLoader.from_yaml(str(inventory_file))


@pytest_asyncio.fixture
async def ssh_pool() -> AsyncGenerator[SSHConnectionPool, None]:
    pool = SSHConnectionPool(parallel_limit=3)
    yield pool
    await pool.close_all()


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncGenerator[DatabaseManager, None]:
    db_path = tmp_path / "test_rollouts.db"
    db_mgr = DatabaseManager(str(db_path))
    await db_mgr.initialize()
    yield db_mgr
    await db_mgr.close()
