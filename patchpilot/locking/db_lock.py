import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def acquire_lock(
    session: AsyncSession,
    environment: str,
    rollout_id: str,
) -> bool:
    """Try to acquire a lock for the given environment. Returns True on success."""
    from datetime import datetime, timedelta

    from sqlalchemy.exc import IntegrityError

    from patchpilot.db.models import RolloutLock

    # Check for stale locks
    stmt = select(RolloutLock).where(RolloutLock.environment == environment)
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing:
        age = datetime.utcnow() - existing.acquired_at.replace(tzinfo=None)
        if age > timedelta(hours=24):
            logger.warning(
                "Removing stale lock for environment '%s' (acquired %s, age %s)",
                environment, existing.acquired_at, age,
            )
            await session.delete(existing)
            await session.flush()
        else:
            logger.warning(
                "Lock already held for environment '%s' by rollout '%s' (since %s)",
                environment, existing.rollout_id, existing.acquired_at,
            )
            return False

    try:
        lock = RolloutLock(
            environment=environment,
            rollout_id=rollout_id,
        )
        session.add(lock)
        await session.flush()
        return True
    except IntegrityError:
        await session.rollback()
        return False


async def release_lock(session: AsyncSession, environment: str) -> None:
    from patchpilot.db.models import RolloutLock

    stmt = select(RolloutLock).where(RolloutLock.environment == environment)
    result = await session.execute(stmt)
    lock = result.scalar_one_or_none()
    if lock:
        await session.delete(lock)
        await session.flush()
