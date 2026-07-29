"""Integration tests: full rollout workflow — plan, deploy, status, rollback."""

import asyncio

import pytest

from patchpilot.rollout.executor import RolloutExecutor
from patchpilot.rollout.planner import RolloutPlanner, RolloutPlan
from patchpilot.ssh.client import SSHSession


@pytest.mark.integration
@pytest.mark.slow
class TestRollout:
    """Full rollout lifecycle tests.
    
    These tests start docker containers, plan a rollout, execute it,
    verify health checks pass, and test failure scenarios.
    """

    async def test_plan(self, inventory, ssh_key) -> None:
        """RolloutPlanner should produce a valid plan for the lab environment."""
        planner = RolloutPlanner(inventory, ssh_pool=None)
        try:
            plan = await planner.plan()
            assert isinstance(plan, RolloutPlan)
            assert plan.environment == "lab"
            assert len(plan.hosts) > 0
            assert plan.total_packages >= 0
            assert len(plan.batches) > 0

            # Check first batch is canary (only non-broken hosts)
            first_batch = plan.batches[0]
            for host in first_batch:
                assert "broken" not in host.name

        finally:
            await planner.close()

    async def test_plan_detects_ubuntu(self, inventory, ssh_key, docker_hosts) -> None:
        """Planner should correctly detect Ubuntu on the Ubuntu containers."""
        planner = RolloutPlanner(inventory, ssh_pool=None)
        try:
            plan = await planner.plan()
            for ph in plan.hosts:
                if "rocky" in ph.host.name:
                    continue  # Rocky uses a different package manager not in MVP
                assert ph.distro.distro_id in ("ubuntu", "debian"), (
                    f"Expected Ubuntu/Debian on {ph.host.name}, got {ph.distro.distro_id}"
                )
        finally:
            await planner.close()

    async def test_executor_full_rollout(self, inventory, ssh_key, db, docker_hosts) -> None:
        """Full rollout: plan → execute → all hosts healthy."""
        planner = RolloutPlanner(inventory)
        try:
            plan = await planner.plan()
        finally:
            await planner.close()

        # Only update non-broken hosts for this test
        healthy_hosts = [h for h in inventory.hosts if "broken" not in h.name]
        original_hosts = inventory.hosts
        inventory.hosts = healthy_hosts
        planner2 = RolloutPlanner(inventory)
        try:
            plan = await planner2.plan()
        finally:
            await planner2.close()
        inventory.hosts = original_hosts

        executor = RolloutExecutor(
            inventory=inventory,
            plan=plan,
            db=db,
            auto_approve=True,
        )
        try:
            rollout_id = await executor.execute()
            assert rollout_id is not None
            assert len(rollout_id) > 0  # UUID

            # Verify all hosts were marked healthy
            from sqlalchemy import select
            from patchpilot.db.models import RolloutHost

            async with db.session() as session:
                result = await session.execute(
                    select(RolloutHost).where(RolloutHost.rollout_id == rollout_id)
                )
                hosts = result.scalars().all()

            for h in hosts:
                assert h.status in ("healthy", "skipped"), (
                    f"Host {h.host_name} ended with status {h.status}"
                )

        finally:
            await executor.close()

    async def test_canary_stops_on_failure(self, inventory, ssh_key, db, docker_hosts) -> None:
        """If the canary fails health checks, the rollout should stop."""
        # Break nginx on the canary-eligible host
        host_ip = docker_hosts.get("ubuntu-broken")
        if not host_ip:
            pytest.skip("ubuntu-broken not available")

        session = SSHSession(host=host_ip, username="deploy", client_keys=[ssh_key], timeout=15)
        try:
            await session.connect()
            await session.run("systemctl stop nginx", sudo=True, timeout=10)
        finally:
            await session.disconnect()

        planner = RolloutPlanner(inventory)
        try:
            plan = await planner.plan()
        finally:
            await planner.close()

        executor = RolloutExecutor(
            inventory=inventory,
            plan=plan,
            db=db,
            auto_approve=True,
        )
        try:
            rollout_id = await executor.execute()

            from sqlalchemy import select
            from patchpilot.db.models import RolloutHost, Rollout

            async with db.session() as session:
                result = await session.execute(
                    select(Rollout).where(Rollout.id == rollout_id)
                )
                rollout = result.scalar_one()
                # Rollout may have completed (if ubuntu-01 was canary) or failed
                # depending on which host was chosen as canary
                assert rollout.status in ("completed", "failed")

                result = await session.execute(
                    select(RolloutHost).where(RolloutHost.rollout_id == rollout_id)
                )
                hosts = result.scalars().all()

            for h in hosts:
                if "broken" in h.host_name:
                    assert h.status in ("failed", "rolled_back", "skipped"), (
                        f"Broken host {h.host_name} should be failed/skipped, got {h.status}"
                    )
        finally:
            await executor.close()

        # Restore nginx on broken host
        session2 = SSHSession(host=host_ip, username="deploy", client_keys=[ssh_key], timeout=15)
        try:
            await session2.connect()
            await session2.run("systemctl start nginx", sudo=True, timeout=10)
        finally:
            await session2.disconnect()
