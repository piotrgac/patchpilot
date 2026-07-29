import hashlib
import json
from datetime import datetime

from sqlalchemy import select

from patchpilot.db.models import AuditEvent
from patchpilot.db.session import DatabaseManager


class AuditLogger:
    def __init__(self, db: DatabaseManager, rollout_id: str, use_hash_chain: bool = False) -> None:
        self._db = db
        self.rollout_id = rollout_id
        self.use_hash_chain = use_hash_chain
        self._last_hash: str | None = None

    async def log(self, event_type: str, payload: dict, actor: str | None = None) -> None:
        import os
        actor = actor or os.environ.get("USER", "unknown")

        payload_json = self._serialize(payload)

        async with self._db.session() as session:
            if self.use_hash_chain and self._last_hash is None:
                result = await session.execute(
                    select(AuditEvent.event_hash)
                    .where(AuditEvent.rollout_id == self.rollout_id)
                    .order_by(AuditEvent.id.desc())
                    .limit(1)
                )
                row = result.scalar_one_or_none()
                self._last_hash = row or "0" * 64

            previous = self._last_hash or "0" * 64
            event_hash = self._compute_hash(previous, payload_json)

            event = AuditEvent(
                rollout_id=self.rollout_id,
                event_type=event_type,
                actor=actor,
                timestamp=datetime.utcnow(),
                previous_hash=previous if self.use_hash_chain else None,
                event_hash=event_hash,
                payload_json=payload_json,
            )
            session.add(event)

            self._last_hash = event_hash

    def _serialize(self, payload: dict) -> dict:
        result: dict = {}
        for key, value in payload.items():
            if isinstance(value, (list, dict)):
                try:
                    json.dumps(value)
                    result[key] = value
                except (TypeError, ValueError):
                    result[key] = str(value)
            elif hasattr(value, "__dict__"):
                result[key] = str(value)
            else:
                result[key] = value
        return result

    def _compute_hash(self, previous: str, payload: dict) -> str:
        raw = previous + json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    async def verify_chain(db: DatabaseManager, rollout_id: str) -> list[dict]:
        async with db.session() as session:
            result = await session.execute(
                select(AuditEvent)
                .where(AuditEvent.rollout_id == rollout_id)
                .order_by(AuditEvent.id)
            )
            events = result.scalars().all()

        issues: list[dict] = []
        prev = "0" * 64

        for event in events:
            if event.previous_hash and event.previous_hash != prev:
                issues.append({
                    "id": event.id,
                    "event_type": event.event_type,
                    "expected_prev": prev,
                    "actual_prev": event.previous_hash,
                    "issue": "hash chain broken",
                })
            raw = prev + json.dumps(event.payload_json, sort_keys=True, default=str)
            expected = hashlib.sha256(raw.encode()).hexdigest()
            if expected != event.event_hash:
                issues.append({
                    "id": event.id,
                    "event_type": event.event_type,
                    "expected_hash": expected,
                    "actual_hash": event.event_hash,
                    "issue": "event hash mismatch",
                })
            prev = event.event_hash

        return issues
