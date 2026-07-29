from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Rollout(Base):
    __tablename__ = "rollouts"

    id: Mapped[str] = mapped_column(
        Text, primary_key=True, default=lambda: uuid4().hex
    )
    env_name: Mapped[str] = mapped_column(Text, nullable=False)
    strategy_type: Mapped[str] = mapped_column(
        Text,
        CheckConstraint("strategy_type IN ('canary', 'batch', 'single')"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        Text,
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'paused', 'completed', 'failed', 'aborted')"
        ),
        default="pending",
        nullable=False,
    )
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    maintenance_window_ok: Mapped[bool | None] = mapped_column(Boolean, default=None)
    plan_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    final_report_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    metrics_written: Mapped[bool] = mapped_column(Boolean, default=False)
    aborted_reason: Mapped[str | None] = mapped_column(Text, default=None)

    hosts: Mapped[list["RolloutHost"]] = relationship(
        back_populates="rollout", cascade="all, delete-orphan"
    )
    audit_events: Mapped[list["AuditEvent"]] = relationship(
        back_populates="rollout", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_rollouts_env_status", "env_name", "status"),
    )


class RolloutHost(Base):
    __tablename__ = "rollout_hosts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rollout_id: Mapped[str] = mapped_column(
        Text, ForeignKey("rollouts.id", ondelete="CASCADE"), nullable=False
    )
    host_name: Mapped[str] = mapped_column(Text, nullable=False)
    host_role: Mapped[str] = mapped_column(Text, nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text,
        CheckConstraint(
            "status IN ("
            "'pending', 'snapshotting', 'updating', 'rebooting', "
            "'verifying', 'healthy', 'failed', 'rolling_back', "
            "'rolled_back', 'skipped'"
            ")"
        ),
        default="pending",
        nullable=False,
    )
    snapshot_type: Mapped[str | None] = mapped_column(
        Text,
        CheckConstraint("snapshot_type IN ('btrfs', 'lvm', 'zfs', 'timeshift', 'vm', 'none')"),
        default=None,
    )
    snapshot_name: Mapped[str | None] = mapped_column(Text, default=None)
    snapshot_created_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    reboot_required: Mapped[bool | None] = mapped_column(Boolean, default=None)
    error_log: Mapped[str | None] = mapped_column(Text, default=None)

    rollout: Mapped["Rollout"] = relationship(back_populates="hosts")
    steps: Mapped[list["RolloutStep"]] = relationship(
        back_populates="host", cascade="all, delete-orphan"
    )
    health_results: Mapped[list["HealthCheckResult"]] = relationship(
        back_populates="host", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("rollout_id", "host_name"),
        Index("idx_rollout_hosts_rollout", "rollout_id"),
        Index("idx_rollout_hosts_status", "status"),
    )


class RolloutStep(Base):
    __tablename__ = "rollout_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rollout_host_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("rollout_hosts.id", ondelete="CASCADE"), nullable=False
    )
    step_type: Mapped[str] = mapped_column(
        Text,
        CheckConstraint(
            "step_type IN ('snapshot', 'update', 'reboot', 'verify', 'rollback', 'begin')"
        ),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        Text,
        CheckConstraint("status IN ('pending', 'in_progress', 'completed', 'failed', 'skipped')"),
        default="pending",
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    log_output: Mapped[str | None] = mapped_column(Text, default=None)
    exit_code: Mapped[int | None] = mapped_column(Integer, default=None)
    packages_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    snapshot_info_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    host: Mapped["RolloutHost"] = relationship(back_populates="steps")

    __table_args__ = (
        UniqueConstraint("rollout_host_id", "step_type"),
        Index("idx_rollout_steps_host", "rollout_host_id"),
    )


class HealthCheckResult(Base):
    __tablename__ = "health_check_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rollout_host_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("rollout_hosts.id", ondelete="CASCADE"), nullable=False
    )
    check_type: Mapped[str] = mapped_column(Text, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    details: Mapped[str | None] = mapped_column(Text, default=None)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    retry_number: Mapped[int] = mapped_column(Integer, default=0)

    host: Mapped["RolloutHost"] = relationship(back_populates="health_results")

    __table_args__ = (
        Index("idx_health_host", "rollout_host_id"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rollout_id: Mapped[str] = mapped_column(
        Text, ForeignKey("rollouts.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    previous_hash: Mapped[str | None] = mapped_column(Text, default=None)
    event_hash: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)

    rollout: Mapped["Rollout"] = relationship(back_populates="audit_events")

    __table_args__ = (
        Index("idx_audit_rollout", "rollout_id"),
        Index("idx_audit_timestamp", "timestamp"),
    )


class RolloutLock(Base):
    __tablename__ = "rollout_locks"

    environment: Mapped[str] = mapped_column(Text, primary_key=True)
    rollout_id: Mapped[str] = mapped_column(Text, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
