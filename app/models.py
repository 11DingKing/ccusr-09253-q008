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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("event_id", "plan_version", name="uq_events_event_id_plan"),
        Index("ix_events_plan_student", "plan_version", "student_id"),
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


# ---------------------------------------------------------------------------
# 跨校实训互认（institution identities / recognition rules / exchange batches）
# ---------------------------------------------------------------------------


class Institution(Base):
    """登记联合实训的牵头/合作院校身份与其签名公钥。"""

    __tablename__ = "institutions"

    institution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    iana_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    # 对称 HMAC 密钥（测试与轻量部署用）；生产可替换为非对称验签。
    signing_secret: Mapped[str] = mapped_column(String(256), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class RecognitionRule(Base):
    """院校间互认规则的版本化登记；新版本永不修改旧行。"""

    __tablename__ = "recognition_rules"

    rule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    sender_id: Mapped[str] = mapped_column(String(64), nullable=False)
    receiver_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False)
    # sender 的活动类型 -> 本校映射活动类型；未列出的类型不予互认。
    activity_map: Mapped[dict] = mapped_column(JSON, nullable=False)
    # 可互认活动类型的秒数上限（按映射后类型计），None 表示不限。
    cap_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("version > 0", name="ck_rules_version_positive"),
        CheckConstraint(
            "cap_seconds IS NULL OR cap_seconds >= 0",
            name="ck_rules_cap_nonneg",
        ),
        Index(
            "ix_rules_pair_active",
            "sender_id",
            "receiver_id",
            "plan_version",
            "active",
        ),
    )


class ExchangeBatch(Base):
    """一次跨校事件交换批次；规则版本在接收时固化，事后升级不回溯。"""

    __tablename__ = "exchange_batches"

    batch_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    sender_id: Mapped[str] = mapped_column(String(64), nullable=False)
    receiver_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 批次原始签名与发送方声称的发送时间（对账时重新验签）。
    signature: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    sent_at: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # received / validated / applied / rejected
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="received")
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quarantined_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_digest: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # 原始批次信封（含事件与发送顺序），重启后可重新规范化验签与对账。
    envelope: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_error: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index(
            "ix_batches_sender_plan", "sender_id", "receiver_id", "plan_version"
        ),
    )


class ExternalEvent(Base):
    """登记外校原始事件及其到本校培养方案的映射结果。"""

    __tablename__ = "external_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("exchange_batches.batch_id"), nullable=False, index=True
    )
    sender_id: Mapped[str] = mapped_column(String(64), nullable=False)
    receiver_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False)
    # 发送方事件编号（在 sender 命名空间内唯一）。
    external_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sender_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    # 映射后写入本地 events 表的命名空间编号；None 表示尚未映射/未采信。
    local_event_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # accepted / duplicate / quarantined / rejected
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="accepted")
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reason: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # 裁决审计：争议事件在裁决前 status=quarantined 且不计学时。
    resolution: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    adjudicator: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    adjudication_note: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    adjudicated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    occurred_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "sender_id",
            "external_event_id",
            name="uq_external_events_sender_event",
        ),
        Index(
            "ix_external_lookup",
            "batch_id",
            "sender_id",
            "receiver_id",
            "plan_version",
        ),
    )
