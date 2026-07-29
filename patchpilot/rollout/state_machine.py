from enum import Enum, auto


class HostState(str, Enum):
    PENDING = "pending"
    SNAPSHOTTING = "snapshotting"
    UPDATING = "updating"
    REBOOTING = "rebooting"
    VERIFYING = "verifying"
    HEALTHY = "healthy"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    SKIPPED = "skipped"


class RolloutState(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


# Allowed transitions for a host's state machine
_HOST_TRANSITIONS: dict[HostState, set[HostState]] = {
    HostState.PENDING: {HostState.SNAPSHOTTING, HostState.SKIPPED, HostState.UPDATING},
    HostState.SNAPSHOTTING: {HostState.UPDATING, HostState.FAILED},
    HostState.UPDATING: {HostState.REBOOTING, HostState.VERIFYING, HostState.FAILED},
    HostState.REBOOTING: {HostState.VERIFYING, HostState.FAILED},
    HostState.VERIFYING: {HostState.HEALTHY, HostState.FAILED},
    HostState.HEALTHY: {HostState.ROLLING_BACK},  # manual rollback
    HostState.FAILED: {HostState.ROLLING_BACK, HostState.ROLLED_BACK},
    HostState.ROLLING_BACK: {HostState.ROLLED_BACK, HostState.FAILED},
    HostState.ROLLED_BACK: set(),
    HostState.SKIPPED: set(),
}


def validate_host_transition(current: HostState, next_state: HostState) -> None:
    """Raise ValueError if the transition is not allowed."""
    allowed = _HOST_TRANSITIONS.get(current, set())
    if next_state not in allowed:
        raise ValueError(
            f"Invalid host state transition: {current.value} → {next_state.value}. "
            f"Allowed: {[s.value for s in allowed]}"
        )
