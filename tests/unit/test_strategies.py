"""Tests for rollout strategies (batch grouping logic)."""
import pytest

from patchpilot.inventory.models import HostModel, InventoryModel, StrategyModel
from patchpilot.rollout.strategies import (
    CanaryStrategy,
    SingleStrategy,
    build_strategy,
)


def _make_inventory(hosts: list[dict], strategy: dict | None = None) -> InventoryModel:
    return InventoryModel(
        metadata={"name": "test"},
        hosts=[HostModel(**h) for h in hosts],
        strategy=StrategyModel.model_validate(strategy or {"type": "canary"}),
    )


class TestCanaryStrategy:
    def test_single_canary(self) -> None:
        inv = _make_inventory(
            hosts=[
                {"name": "c1", "address": "10.0.0.1", "tags": ["canary-eligible"]},
                {"name": "c2", "address": "10.0.0.2", "tags": ["canary-eligible"]},
                {"name": "n1", "address": "10.0.0.3"},
            ],
            strategy={
                "type": "canary",
                "canary": {"count": 1, "tag_filter": "canary-eligible"},
                "batch": {"size": 2},
            },
        )
        strategy = build_strategy(inv.strategy, inv)
        assert isinstance(strategy, CanaryStrategy)
        batches = strategy.group_hosts()

        assert len(batches) == 2
        assert len(batches[0]) == 1  # canary
        assert batches[0][0].name == "c1"
        assert len(batches[1]) == 2  # c2 + n1

    def test_canary_respects_tag(self) -> None:
        inv = _make_inventory(
            hosts=[
                {"name": "a", "address": "10.0.0.1", "tags": ["canary-eligible"]},
                {"name": "b", "address": "10.0.0.2", "tags": ["canary-eligible"]},
                {"name": "c", "address": "10.0.0.3"},
            ],
            strategy={
                "type": "canary",
                "canary": {"count": 1, "tag_filter": "canary-eligible"},
                "batch": {"size": 1},
            },
        )
        strategy = build_strategy(inv.strategy, inv)
        batches = strategy.group_hosts()
        batch_0_names = {h.name for h in batches[0]}
        assert "a" in batch_0_names or "b" in batch_0_names
        assert "c" not in batch_0_names

    def test_no_canary_candidates_raises(self) -> None:
        inv = _make_inventory(
            hosts=[
                {"name": "api", "address": "10.0.0.1", "role": "api"},
                {"name": "db", "address": "10.0.0.2", "role": "database"},
            ],
            strategy={
                "type": "canary",
                "canary": {"count": 1, "tag_filter": "canary-eligible"},
            },
        )
        strategy = build_strategy(inv.strategy, inv)
        with pytest.raises(ValueError, match="No hosts match the canary tag filter"):
            strategy.group_hosts()

    def test_database_last(self) -> None:
        inv = _make_inventory(
            hosts=[
                {"name": "db", "address": "10.0.0.2", "role": "database"},
                {"name": "api", "address": "10.0.0.1", "role": "api", "tags": ["canary-eligible"]},
            ],
            strategy={
                "type": "canary",
                "canary": {"count": 1, "tag_filter": "canary-eligible"},
                "batch": {"size": 5},
            },
        )
        strategy = build_strategy(inv.strategy, inv)
        batches = strategy.group_hosts()
        assert batches[0][0].name == "api"
        remaining = [h for batch in batches[1:] for h in batch]
        assert remaining[-1].role == "database"


class TestSingleStrategy:
    def test_each_host_separate_batch(self) -> None:
        inv = _make_inventory(
            hosts=[
                {"name": "a", "address": "10.0.0.1"},
                {"name": "b", "address": "10.0.0.2"},
                {"name": "c", "address": "10.0.0.3"},
            ],
            strategy={"type": "single"},
        )
        strategy = build_strategy(inv.strategy, inv)
        assert isinstance(strategy, SingleStrategy)
        batches = strategy.group_hosts()
        assert len(batches) == 3
        assert all(len(b) == 1 for b in batches)
