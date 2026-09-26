"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..models import (
    Event as EventModel,
    ExchangeBatch,
    ExchangeEvent,
    Institution,
    RecognitionRule,
)


# ---------------------------------------------------------------------------
# 机构身份
# ---------------------------------------------------------------------------

def get_institution(db: Session, code: str) -> Institution | None:
    return db.get(Institution, code)


def upsert_institution(
    db: Session,
    *,
    code: str,
    display_name: str,
    iana_timezone: str,
    verification_key: str,
    is_active: bool = True,
) -> Institution:
    stmt = sqlite_insert(Institution).values(
        code=code,
        display_name=display_name,
        iana_timezone=iana_timezone,
        verification_key=verification_key,
        is_active=is_active,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["code"],
        set_={
            "display_name": display_name,
            "iana_timezone": iana_timezone,
            "verification_key": verification_key,
            "is_active": is_active,
        },
    )
    db.execute(stmt)
    db.commit()
    row = db.get(Institution, code)
    assert row is not None
    return row


def list_institutions(db: Session) -> list[Institution]:
    return list(db.execute(select(Institution).order_by(Institution.code)).scalars())


# ---------------------------------------------------------------------------
# 互认规则(版本化,不可变)
# ---------------------------------------------------------------------------

def create_rule(
    db: Session,
    *,
    rule_code: str,
    local_plan_version: str,
    partner_institution_code: str,
    event_type_mapping: dict[str, str],
    partner_plan_version: str | None = None,
    accepted_activity_types: list[str] | None = None,
) -> RecognitionRule:
    """在同一 rule_code 上追加一个新的草稿版本。"""
    latest = db.execute(
        select(RecognitionRule.version)
        .where(RecognitionRule.rule_code == rule_code)
        .order_by(RecognitionRule.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    next_version = (latest or 0) + 1
    row = RecognitionRule(
        rule_code=rule_code,
        version=next_version,
        local_plan_version=local_plan_version,
        partner_institution_code=partner_institution_code,
        partner_plan_version=partner_plan_version,
        event_type_mapping=dict(event_type_mapping),
        accepted_activity_types=accepted_activity_types,
        status="draft",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_rule(db: Session, rule_code: str, version: int) -> RecognitionRule | None:
    return db.get(RecognitionRule, (rule_code, version))


def publish_rule(
    db: Session, rule_code: str, version: int
) -> RecognitionRule | None:
    row = get_rule(db, rule_code, version)
    if row is None:
        return None
    if row.status == "draft":
        row.status = "active"
        row.published_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(row)
    return row


def retire_rule(
    db: Session, rule_code: str, version: int
) -> RecognitionRule | None:
    row = get_rule(db, rule_code, version)
    if row is None:
        return None
    if row.status == "active":
        row.status = "retired"
        db.commit()
        db.refresh(row)
    return row


def list_rules(db: Session, rule_code: str | None = None) -> list[RecognitionRule]:
    stmt = select(RecognitionRule).order_by(
        RecognitionRule.rule_code, RecognitionRule.version
    )
    if rule_code is not None:
        stmt = stmt.where(RecognitionRule.rule_code == rule_code)
    return list(db.execute(stmt).scalars())


# ---------------------------------------------------------------------------
# 交换批次
# ---------------------------------------------------------------------------

def insert_received_batch(db: Session, values: dict[str, Any]) -> str:
    """落库一个已通过签名校验的批次信封。

    返回 'inserted' / 'duplicate_id'(batch_id 重复) /
    'duplicate_seq'(同序号重传但 batch_id 不同)。
    """
    stmt = sqlite_insert(ExchangeBatch).values(**values)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["batch_id"]
    ).returning(ExchangeBatch.id)
    inserted = db.execute(stmt).scalar_one_or_none()
    if inserted is not None:
        db.commit()
        return "inserted"
    db.rollback()

    existing_id = db.execute(
        select(ExchangeBatch.batch_id).where(
            ExchangeBatch.batch_id == values["batch_id"]
        )
    ).scalar_one_or_none()
    if existing_id is not None:
        return "duplicate_id"
    return "duplicate_seq"


def get_batch(db: Session, batch_id: str) -> ExchangeBatch | None:
    stmt = select(ExchangeBatch).where(ExchangeBatch.batch_id == batch_id)
    return db.execute(stmt).scalar_one_or_none()


def list_batches(
    db: Session,
    *,
    local_plan_version: str | None = None,
    partner_institution_code: str | None = None,
    status: str | None = None,
) -> list[ExchangeBatch]:
    stmt = select(ExchangeBatch).order_by(
        ExchangeBatch.local_plan_version,
        ExchangeBatch.partner_institution_code,
        ExchangeBatch.seq,
    )
    if local_plan_version is not None:
        stmt = stmt.where(ExchangeBatch.local_plan_version == local_plan_version)
    if partner_institution_code is not None:
        stmt = stmt.where(
            ExchangeBatch.partner_institution_code == partner_institution_code
        )
    if status is not None:
        stmt = stmt.where(ExchangeBatch.status == status)
    return list(db.execute(stmt).scalars())


def list_pending_batches(db: Session) -> list[ExchangeBatch]:
    """所有已接收但未入账的批次,按通道与序号排序(供乱序追平和重启恢复)。"""
    stmt = (
        select(ExchangeBatch)
        .where(ExchangeBatch.status == "RECEIVED")
        .order_by(
            ExchangeBatch.local_plan_version,
            ExchangeBatch.partner_institution_code,
            ExchangeBatch.seq,
        )
    )
    return list(db.execute(stmt).scalars())


def ingested_seq_for(db: Session, batch: ExchangeBatch) -> int:
    """某通道已连续入账到的最大序号(0 表示尚未开始)。"""
    row = db.execute(
        select(ExchangeBatch.seq)
        .where(ExchangeBatch.local_plan_version == batch.local_plan_version)
        .where(
            ExchangeBatch.partner_institution_code
            == batch.partner_institution_code
        )
        .where(ExchangeBatch.status == "INGESTED")
        .order_by(ExchangeBatch.seq.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row or 0


def mark_batch_ingested(
    db: Session, batch_id: str, counters: dict[str, int]
) -> None:
    batch = get_batch(db, batch_id)
    assert batch is not None
    batch.status = "INGESTED"
    batch.error_code = None
    batch.processed_at = datetime.now(timezone.utc)
    for key, value in counters.items():
        setattr(batch, key, value)
    db.commit()


def mark_batch_received(db: Session, batch_id: str, error_code: str) -> None:
    batch = get_batch(db, batch_id)
    assert batch is not None
    batch.status = "RECEIVED"
    batch.error_code = error_code
    db.commit()


# ---------------------------------------------------------------------------
# 外校事件映射
# ---------------------------------------------------------------------------

def get_exchange_event(
    db: Session, source_institution_code: str, source_event_id: str
) -> ExchangeEvent | None:
    stmt = (
        select(ExchangeEvent)
        .where(
            ExchangeEvent.source_institution_code == source_institution_code
        )
        .where(ExchangeEvent.source_event_id == source_event_id)
    )
    return db.execute(stmt).scalar_one_or_none()


def insert_mapped_event(
    db: Session,
    *,
    event_row: dict[str, Any],
    exchange_row: dict[str, Any],
) -> tuple[ExchangeEvent | None, EventModel | None]:
    """原子写入映射台账与本校事件流。

    任一唯一约束(来源引用/本校 event_id)冲突则整体回滚,返回 (None, None)。
    """
    try:
        event = EventModel(**event_row)
        db.add(event)
        db.flush()
        mapping = ExchangeEvent(**exchange_row)
        db.add(mapping)
        db.flush()
        db.commit()
    except Exception:
        db.rollback()
        return None, None
    db.refresh(mapping)
    return mapping, event


def insert_ledger_only(db: Session, *, exchange_row: dict[str, Any]) -> ExchangeEvent:
    """仅登记台账(不入账事件流),用于不互认/无效负载等跳过情形。"""
    mapping = ExchangeEvent(**exchange_row)
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return mapping


def list_exchange_events(
    db: Session,
    *,
    local_plan_version: str | None = None,
    batch_id: str | None = None,
    student_id: str | None = None,
    status: str | None = None,
) -> list[ExchangeEvent]:
    stmt = select(ExchangeEvent).order_by(ExchangeEvent.id)
    if local_plan_version is not None:
        stmt = stmt.where(
            ExchangeEvent.local_plan_version == local_plan_version
        )
    if batch_id is not None:
        stmt = stmt.where(ExchangeEvent.batch_id == batch_id)
    if student_id is not None:
        stmt = stmt.where(ExchangeEvent.source_student_id == student_id)
    if status is not None:
        stmt = stmt.where(ExchangeEvent.status == status)
    return list(db.execute(stmt).scalars())


def get_exchange_event_by_local_id(
    db: Session, local_event_id: str
) -> ExchangeEvent | None:
    stmt = select(ExchangeEvent).where(
        ExchangeEvent.local_event_id == local_event_id
    )
    return db.execute(stmt).scalar_one_or_none()


def get_local_event(db: Session, event_id: str) -> EventModel | None:
    stmt = select(EventModel).where(EventModel.event_id == event_id)
    return db.execute(stmt).scalar_one_or_none()


def set_arbitration(
    db: Session,
    mapping: ExchangeEvent,
    *,
    verdict: str,
    reason: str,
    actor: str,
    suppressed: bool,
) -> None:
    """裁决:更新台账与事件流的互认状态,裁决后重新回放即可对账。"""
    event = get_local_event(db, mapping.local_event_id)
    mapping.status = verdict
    mapping.arbitration_reason = reason
    mapping.arbitrated_by = actor
    mapping.arbitrated_at = datetime.now(timezone.utc)
    mapping.suppress_reason = "arbitration_duplicate" if suppressed else None
    if event is not None:
        event.exchange_status = verdict
        event.exchange_suppressed = suppressed
    db.commit()
