"""Unit tests for audit logger functionality."""
from patchpilot.audit.logger import AuditLogger
from patchpilot.db.session import DatabaseManager


class TestAuditLogger:
    async def test_log_event(self, tmp_path) -> None:
        db_path = str(tmp_path / "test_audit.db")
        db = DatabaseManager(db_path)
        await db.initialize()

        logger = AuditLogger(db, rollout_id="test-123")
        await logger.log("rollout_started", {"env": "test", "hosts": ["h1"]})

        from sqlalchemy import select

        from patchpilot.db.models import AuditEvent

        async with db.session() as session:
            result = await session.execute(
                select(AuditEvent).where(AuditEvent.rollout_id == "test-123")
            )
            events = result.scalars().all()
            assert len(events) == 1
            assert events[0].event_type == "rollout_started"
            assert events[0].payload_json["env"] == "test"

        await db.close()

    async def test_hash_chain(self, tmp_path) -> None:
        db_path = str(tmp_path / "test_hash.db")
        db = DatabaseManager(db_path)
        await db.initialize()

        logger = AuditLogger(db, rollout_id="hash-test", use_hash_chain=True)
        await logger.log("event_a", {"seq": 1})
        await logger.log("event_b", {"seq": 2})
        await logger.log("event_c", {"seq": 3})

        issues = await AuditLogger.verify_chain(db, "hash-test")
        assert len(issues) == 0, f"Hash chain broken: {issues}"

        await db.close()
