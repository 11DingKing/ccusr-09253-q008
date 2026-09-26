"""跨校交换的持久化操作（机构、规则版本、批次、外校事件登记）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import (
    Event as EventModel,
    ExchangeBatch,
    ExternalEvent,
    Institution,
    RecognitionRule,
)


# ---------------------------------------------------------------------------
# 机构身份
# ---------------------------------------------------------------------------


def get_institution(db: Session, institution_id: str) -> Institution | None:
    return db.get(Institution, institution_id)


def upsert_institution(
    db: Session,
    *,
    institution_id: str,
    name: str,
    iana_timezone: str,
    signing_secret: str,
    active: bool = True,
) -> Institution:
    stmt = sqlite_insert(Institution).values(
        institution_id=institution_id,
        name=name,
        iana_timezone=iana_timezone,
        signing_secret=signing_secret,
        active=active,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["institution_id"],
        set_={
            "name": name,
            "iana_timezone": iana_timezone,
            "signing_secret": signing_secret,
            "active": active,
        },
    )
    db.execute(stmt)
    db.commit()
    row = db.get(Institution, institution_id)
    assert row is not None
    return row


def list_institutions(db: Session) -> list[Institution]:
    return list(
        db.execute(select(Institution).order_by(Institution.institution_id)).scalars()
    )


# ---------------------------------------------------------------------------
# 互认规则（版本化，只追加）
# ---------------------------------------------------------------------------


def get_rule_version(
    db: Session, rule_id: str, version: int
) -> RecognitionRule | None:
    return db.get(RecognitionRule, (rule_id, version))


def latest_rule_version(db: Session, rule_id: str) -> int | None:
    stmt = (
        select(RecognitionRule.version)
        .where(RecognitionRule.rule_id == rule_id)
        .order_by(RecognitionRule.version.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_active_rule(
    db: Session, sender_id: str, receiver_id: str, plan_version: str
) -> RecognitionRule | None:
    stmt = (
        select(RecognitionRule)
        .where(RecognitionRule.sender_id == sender_id)
        .where(RecognitionRule.receiver_id == receiver_id)
        .where(RecognitionRule.plan_version == plan_version)
        .where(RecognitionRule.active.is_(True))
        .order_by(RecognitionRule.version.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def list_rule_versions(db: Session, rule_id: str) -> list[RecognitionRule]:
    stmt = (
        select(RecognitionRule)
        .where(RecognitionRule.rule_id == rule_id)
        .order_by(RecognitionRule.version)
    )
    return list(db.execute(stmt).scalars())


def insert_rule_version(
    db: Session,
    *,
    rule_id: str,
    sender_id: str,
    receiver_id: str,
    plan_version: str,
    activity_map: dict[str, str],
    cap_seconds: int | None,
    active: bool = True,
) -> RecognitionRule:
    """登记规则新版本；旧版本行保持不变，批次永久引用其接收时的版本。"""
    next_version = (latest_rule_version(db, rule_id) or 0) + 1
    row = RecognitionRule(
        rule_id=rule_id,
        version=next_version,
        sender_id=sender_id,
        receiver_id=receiver_id,
        plan_version=plan_version,
        activity_map=activity_map,
        cap_seconds=cap_seconds,
        active=active,
    )
    db.add(row)
    db.commit()
    return row


# ---------------------------------------------------------------------------
# 交换批次
# ---------------------------------------------------------------------------


def get_batch(db: Session, batch_id: str) -> ExchangeBatch | None:
    return db.get(ExchangeBatch, batch_id)


def insert_batch(
    db: Session,
    *,
    batch_id: str,
    sender_id: str,
    receiver_id: str,
    plan_version: str,
    rule_id: str,
    rule_version: int,
    signature: str,
    sent_at: str,
    payload_digest: str,
    envelope: dict[str, Any],
    expected_count: int,
) -> ExchangeBatch | None:
    """幂等插入批次；已存在时返回 None（由调用方比对摘要）。"""
    stmt = sqlite_insert(ExchangeBatch)
    stmt = stmt.values(
        batch_id=batch_id,
        sender_id=sender_id,
        receiver_id=receiver_id,
        plan_version=plan_version,
        rule_id=rule_id,
        rule_version=rule_version,
        signature=signature,
        sent_at=sent_at,
        status="validated",
        expected_count=expected_count,
        payload_digest=payload_digest,
        envelope=envelope,
    ).on_conflict_do_nothing(index_elements=["batch_id"]).returning(
        ExchangeBatch.batch_id
    )
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return None
    return db.get(ExchangeBatch, batch_id)


def mark_batch_status(
    db: Session,
    batch_id: str,
    *,
    status: str,
    accepted_count: int | None = None,
    duplicate_count: int | None = None,
    quarantined_count: int | None = None,
    last_error: str = "",
) -> None:
    row = db.get(ExchangeBatch, batch_id)
    assert row is not None
    row.status = status
    if accepted_count is not None:
        row.accepted_count = accepted_count
    if duplicate_count is not None:
        row.duplicate_count = duplicate_count
    if quarantined_count is not None:
        row.quarantined_count = quarantined_count
    row.last_error = last_error
    db.commit()


def list_batches(
    db: Session, *, plan_version: str | None = None
) -> list[ExchangeBatch]:
    stmt = select(ExchangeBatch).order_by(ExchangeBatch.created_at)
    if plan_version is not None:
        stmt = stmt.where(ExchangeBatch.plan_version == plan_version)
    return list(db.execute(stmt).scalars())


# ---------------------------------------------------------------------------
# 外校事件登记
# ---------------------------------------------------------------------------


def get_external_event(
    db: Session, batch_id: str, external_event_id: str
) -> ExternalEvent | None:
    stmt = select(ExternalEvent).where(
        ExternalEvent.batch_id == batch_id,
        ExternalEvent.external_event_id == external_event_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def list_external_events(
    db: Session,
    *,
    batch_id: str | None = None,
    plan_version: str | None = None,
    status: str | None = None,
) -> list[ExternalEvent]:
    stmt = select(ExternalEvent).order_by(
        ExternalEvent.sender_id, ExternalEvent.sender_seq
    )
    if batch_id is not None:
        stmt = stmt.where(ExternalEvent.batch_id == batch_id)
    if plan_version is not None:
        stmt = stmt.where(ExternalEvent.plan_version == plan_version)
    if status is not None:
        stmt = stmt.where(ExternalEvent.status == status)
    return list(db.execute(stmt).scalars())


def upsert_external_event(
    db: Session,
    *,
    batch_id: str,
    sender_id: str,
    receiver_id: str,
    plan_version: str,
    external_event_id: str,
    sender_seq: int,
    student_id: str,
    event_type: str,
    payload: dict[str, Any],
    status: str,
    local_event_id: str | None,
    rule_id: str,
    rule_version: int,
    reason: str,
    occurred_at_utc: datetime | None,
) -> None:
    """按 (sender_id, external_event_id) 幂等登记；重启重跑不产生重复行。"""
    stmt = sqlite_insert(ExternalEvent).values(
        batch_id=batch_id,
        sender_id=sender_id,
        receiver_id=receiver_id,
        plan_version=plan_version,
        external_event_id=external_event_id,
        sender_seq=sender_seq,
        student_id=student_id,
        event_type=event_type,
        payload=payload,
        status=status,
        local_event_id=local_event_id,
        rule_id=rule_id,
        rule_version=rule_version,
        reason=reason,
        occurred_at_utc=occurred_at_utc,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["sender_id", "external_event_id"],
        set_={
            "status": status,
            "local_event_id": local_event_id,
            "reason": reason,
            # 重复送达时批次归属以最新送达为准，其余来源字段保持不变。
            "batch_id": batch_id,
        },
    )
    db.execute(stmt)


def load_registered_checkins(
    db: Session, plan_version: str, student_ids: set[str]
) -> list[dict[str, Any]]:
    """读取学生已进入本地事件流的签到（含本校事件与已采信外校事件）。

    这些区间构成跨校判重的阻塞集：重发批次或重启恢复时，已采信的
    外校事件不会再次计时。
    """
    if not student_ids:
        return []
    stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    rows = list(db.execute(stmt).scalars())
    registered: list[dict[str, Any]] = []
    for row in rows:
        if row.student_id not in student_ids or row.event_type != "checkin":
            continue
        registered.append(
            {
                "student_id": row.student_id,
                "event_id": row.event_id,
                "local_event_id": row.event_id,
                "payload": dict(row.payload),
            }
        )
    return registered
