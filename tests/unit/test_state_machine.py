"""Tests for rollout state machine transitions."""
import pytest

from patchpilot.rollout.state_machine import (
    HostState,
    validate_host_transition,
)


class TestStateMachine:
    def test_valid_transitions(self) -> None:
        validate_host_transition(HostState.PENDING, HostState.SNAPSHOTTING)
        validate_host_transition(HostState.PENDING, HostState.UPDATING)
        validate_host_transition(HostState.SNAPSHOTTING, HostState.UPDATING)
        validate_host_transition(HostState.UPDATING, HostState.REBOOTING)
        validate_host_transition(HostState.UPDATING, HostState.VERIFYING)
        validate_host_transition(HostState.REBOOTING, HostState.VERIFYING)
        validate_host_transition(HostState.VERIFYING, HostState.HEALTHY)
        validate_host_transition(HostState.VERIFYING, HostState.FAILED)
        validate_host_transition(HostState.FAILED, HostState.ROLLING_BACK)
        validate_host_transition(HostState.ROLLING_BACK, HostState.ROLLED_BACK)
        validate_host_transition(HostState.PENDING, HostState.SKIPPED)

    def test_invalid_transition(self) -> None:
        with pytest.raises(ValueError, match="Invalid host state transition"):
            validate_host_transition(HostState.HEALTHY, HostState.UPDATING)

        with pytest.raises(ValueError, match="Invalid host state transition"):
            validate_host_transition(HostState.ROLLED_BACK, HostState.PENDING)

        with pytest.raises(ValueError, match="Invalid host state transition"):
            validate_host_transition(HostState.SKIPPED, HostState.UPDATING)

    def test_all_states_covered(self) -> None:
        from patchpilot.rollout.state_machine import _HOST_TRANSITIONS
        for state in HostState:
            assert state in _HOST_TRANSITIONS, f"Missing transition map for {state}"
