from abc import ABC, abstractmethod

from patchpilot.inventory.models import HostModel, InventoryModel, StrategyModel


class RolloutStrategy(ABC):
    def __init__(self, config: StrategyModel, inventory: InventoryModel) -> None:
        self.config = config
        self.inventory = inventory

    @abstractmethod
    def group_hosts(self) -> list[list[HostModel]]:
        pass

    def __str__(self) -> str:
        return self.__class__.__name__.replace("Strategy", "").lower()


class CanaryStrategy(RolloutStrategy):
    def group_hosts(self) -> list[list[HostModel]]:
        hosts = list(self.inventory.hosts)
        canary_cfg = self.config.canary

        if canary_cfg and canary_cfg.tag_filter:
            tag_filter = canary_cfg.tag_filter
            canary_pool = [h for h in hosts if tag_filter in h.tags]
            non_canary = [h for h in hosts if tag_filter not in h.tags]
        else:
            canary_pool = list(hosts)
            non_canary = []

        if not canary_pool:
            raise ValueError("No hosts match the canary tag filter")

        canary_count = min(canary_cfg.count if canary_cfg else 1, len(canary_pool))
        canary_hosts = canary_pool[:canary_count]
        remaining = canary_pool[canary_count:] + non_canary

        # Sort remaining: databases last
        def sort_key(h: HostModel) -> tuple[int, str]:
            db_penalty = 1 if h.role == "database" else 0
            return (db_penalty, h.name)

        remaining.sort(key=sort_key)

        batch_size = self.config.batch.size if self.config.batch else 2
        batches = [canary_hosts]
        for i in range(0, len(remaining), batch_size):
            batches.append(remaining[i : i + batch_size])

        return batches


class BatchStrategy(RolloutStrategy):
    def group_hosts(self) -> list[list[HostModel]]:
        hosts = list(self.inventory.hosts)

        def sort_key(h: HostModel) -> tuple[int, str]:
            db_penalty = 1 if h.role == "database" else 0
            return (db_penalty, h.name)

        hosts.sort(key=sort_key)

        batch_size = self.config.batch.size if self.config.batch else 2
        batches: list[list[HostModel]] = []
        for i in range(0, len(hosts), batch_size):
            batches.append(hosts[i : i + batch_size])

        return batches


class SingleStrategy(RolloutStrategy):
    def group_hosts(self) -> list[list[HostModel]]:
        return [[h] for h in self.inventory.hosts]


def build_strategy(config: StrategyModel, inventory: InventoryModel) -> RolloutStrategy:
    strategies = {
        "canary": CanaryStrategy,
        "batch": BatchStrategy,
        "single": SingleStrategy,
    }
    cls = strategies.get(config.type)
    if cls is None:
        raise ValueError(f"Unknown strategy type: {config.type}")
    return cls(config, inventory)
