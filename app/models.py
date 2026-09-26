"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Plan(Base):
    __tablename__ = "plans"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    iana_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    required_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("required_seconds >= 0", name="ck_plans_required_nonneg"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    # 来源溯源:local 为本校事件,exchange 为经互认映射的外校事件。
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="local")
    source_institution: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    source_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    exchange_batch_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    # 仅 source='exchange' 时有值:ACCEPTED/DISPUTED/UPHELD 入账,
    # REJECTED/SKIPPED 在回放时排除;DISPUTED 的签到按待定处理。
    exchange_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    exchange_suppressed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("event_id", "plan_version", name="uq_events_event_id_plan"),
        UniqueConstraint(
            "source_institution", "source_event_id", name="uq_events_source_ref"
        ),
        Index("ix_events_plan_student", "plan_version", "student_id"),
        CheckConstraint(
            "source in ('local', 'exchange')", name="ck_events_source"
        ),
        CheckConstraint(
            "exchange_status is null or exchange_status in "
            "('ACCEPTED', 'SKIPPED', 'DISPUTED', 'UPHELD', 'REJECTED')",
            name="ck_events_exchange_status",
        ),
    )


class Freeze(Base):
    __tablename__ = "freezes"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    freeze_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    event_cutoff_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class Institution(Base):
    """登记参与跨校联合实训的院校身份与校验凭据。"""

    __tablename__ = "institutions"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    iana_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_key: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class RecognitionRule(Base):
    """互认规则的不可变版本;批次在接收时绑定具体版本。"""

    __tablename__ = "recognition_rules"

    rule_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    local_plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    partner_institution_code: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    # None 表示适用于合作院校的任意培养方案。
    partner_plan_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 外校事件类型 -> 本校事件类型 的映射,缺省类型不互认。
    event_type_mapping: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # None 表示不限制活动类型;否则只有名单内活动自动互认,其余进入争议。
    accepted_activity_types: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "status in ('draft', 'active', 'retired')",
            name="ck_recognition_rules_status",
        ),
        Index(
            "ix_rules_lookup",
            "local_plan_version",
            "partner_institution_code",
            "status",
        ),
    )


class ExchangeBatch(Base):
    """外校交换批次:每个 (本校方案, 合作院校) 维护独立连续序号。"""

    __tablename__ = "exchange_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    local_plan_version: Mapped[str] = mapped_column(String(128), nullable=False)
    partner_institution_code: Mapped[str] = mapped_column(String(64), nullable=False)
    partner_plan_version: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_code: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # RECEIVED: 已落库但未处理(序号缺口或处理中崩溃);INGESTED: 已完成映射入账。
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RECEIVED")
    entries_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    disputed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    suppressed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signature: Mapped[str] = mapped_column(String(128), nullable=False)
    # 原始条目落库,使崩溃恢复无需对方重传即可重放入账。
    raw_entries: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "local_plan_version",
            "partner_institution_code",
            "seq",
            name="uq_exchange_batch_seq",
        ),
        CheckConstraint(
            "status in ('RECEIVED', 'INGESTED', 'REJECTED')",
            name="ck_exchange_batch_status",
        ),
    )


class ExchangeEvent(Base):
    """外校事件到本校事件的逐笔映射与裁决状态。"""

    __tablename__ = "exchange_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_event_id: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True
    )
    local_plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("exchange_batches.batch_id"), nullable=False
    )
    source_institution_code: Mapped[str] = mapped_column(String(64), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_student_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    mapped_event_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # ACCEPTED: 已映射入账;SKIPPED: 不互认/重复来源;
    # DISPUTED: 争议待定;UPHELD/REJECTED: 裁决结果。
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 非空表示该笔与既有记录指向同一活动同一时段,在回放中被抑制(不重复计时)。
    suppress_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    arbitration_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    arbitrated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    arbitrated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "source_institution_code",
            "source_event_id",
            name="uq_exchange_event_source",
        ),
        Index("ix_exchange_events_plan_status", "local_plan_version", "status"),
        CheckConstraint(
            "status in ('ACCEPTED', 'SKIPPED', 'DISPUTED', 'UPHELD', 'REJECTED')",
            name="ck_exchange_event_status",
        ),
    )
