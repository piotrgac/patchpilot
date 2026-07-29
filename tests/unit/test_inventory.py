"""Unit tests for PatchPilot core models."""
from pathlib import Path

import pytest
import yaml

from patchpilot.inventory.models import (
    HostModel,
    InventoryLoader,
    InventoryModel,
    StrategyModel,
)


class TestInventoryModels:
    def test_minimal_inventory(self) -> None:
        raw = {
            "metadata": {"name": "test"},
            "hosts": [
                {"name": "web-01", "address": "10.0.0.1", "role": "api"},
            ],
        }
        inv = InventoryModel.model_validate(raw)
        assert inv.metadata_.name == "test"
        assert len(inv.hosts) == 1
        assert inv.hosts[0].name == "web-01"

    def test_duplicate_host_names_raise(self) -> None:
        raw = {
            "metadata": {"name": "test"},
            "hosts": [
                {"name": "web-01", "address": "10.0.0.1"},
                {"name": "web-01", "address": "10.0.0.2"},
            ],
        }
        with pytest.raises(ValueError, match="Host names must be unique"):
            InventoryModel.model_validate(raw)

    def test_default_strategy(self) -> None:
        inv = InventoryModel(
            metadata={"name": "test"},
            hosts=[HostModel(name="h1", address="10.0.0.1")],
        )
        assert inv.strategy.type == "canary"

    def test_health_checks_by_role(self) -> None:
        inv = InventoryModel(
            metadata={"name": "test"},
            hosts=[HostModel(name="h1", address="10.0.0.1", role="api")],
            health_checks={
                "global": [{"type": "systemd", "service": "ssh.service"}],
                "per_role": {
                    "api": [
                        {"type": "http", "url": "http://localhost:8080/health"},
                    ],
                },
            },
        )
        checks = inv.health_checks.for_role("api")
        assert len(checks) == 2
        assert checks[0].type == "systemd"
        assert checks[1].type == "http"

    def test_hosts_by_tag(self) -> None:
        inv = InventoryModel(
            metadata={"name": "test"},
            hosts=[
                HostModel(name="h1", address="10.0.0.1", tags=["canary-eligible"]),
                HostModel(name="h2", address="10.0.0.2"),
            ],
        )
        canary_hosts = inv.hosts_by_tag("canary-eligible")
        assert len(canary_hosts) == 1
        assert canary_hosts[0].name == "h1"

    def test_hosts_by_role(self) -> None:
        inv = InventoryModel(
            metadata={"name": "test"},
            hosts=[
                HostModel(name="h1", address="10.0.0.1", role="api"),
                HostModel(name="h2", address="10.0.0.2", role="database"),
            ],
        )
        api_hosts = inv.hosts_by_role("api")
        db_hosts = inv.hosts_by_role("database")
        assert len(api_hosts) == 1
        assert len(db_hosts) == 1

    def test_inventory_from_yaml_string(self, tmp_path: Path) -> None:
        raw = yaml.safe_dump({
            "metadata": {"name": "lab", "owner": "dev"},
            "connection": {"ssh_user": "admin", "parallel_limit": 3},
            "hosts": [
                {"name": "node-1", "address": "192.168.1.10", "role": "worker"},
            ],
            "strategy": {"type": "batch", "batch": {"size": 5}},
        })
        f = tmp_path / "lab.yaml"
        f.write_text(raw)

        inv = InventoryLoader.from_yaml(str(f))
        assert inv.metadata_.name == "lab"
        assert inv.connection.ssh_user == "admin"
        assert inv.strategy.type == "batch"
        assert inv.strategy.batch is not None
        if inv.strategy.batch:
            assert inv.strategy.batch.size == 5


class TestStrategyModel:
    def test_canary_config(self) -> None:
        s = StrategyModel.model_validate({
            "type": "canary",
            "canary": {"count": 2, "tag_filter": "canary"},
            "batch": {"size": 3},
        })
        assert s.type == "canary"
        assert s.canary is not None
        if s.canary:
            assert s.canary.count == 2
            assert s.canary.tag_filter == "canary"

    def test_single_strategy(self) -> None:
        s = StrategyModel(type="single")
        assert s.type == "single"
