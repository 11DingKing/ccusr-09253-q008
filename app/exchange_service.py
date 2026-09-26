"""跨校互认应用服务：批次接收/校验/恢复、对账、裁决与来源解释。

批次生命周期::

    POST /batches (签名)
        validated ──映射并写入本地事件流──▶ applied
                     任意阶段重发同一批次 = 幂等恢复（resume）

规则版本在批次 validated 时固化进批次行与每条外校事件登记；规则升级
只产生新版本行，旧批次永远按旧版本重放。争议事件停留在 quarantined，
与本地事件流物理隔离，裁决前不计任何学时。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .exchange.mapping import RuleSnapshot, map_batch
from .exchange.signing import SignatureError, payload_digest, verify_signature
from .exchange_store import (
    get_active_rule,
    get_batch,
    get_external_event,
    get_institution,
    get_rule_version,
    insert_batch,
    insert_rule_version,
    list_batches,
    list_external_events,
    list_institutions,
    list_rule_versions,
    load_registered_checkins,
    mark_batch_status,
    upsert_external_event,
    upsert_institution,
)
from .models import Event as EventModel
from .models import ExchangeBatch, ExternalEvent
from .repository import get_plan
from .services import PlanNotFoundError


class InstitutionNotFoundError(Exception):
    pass


class InstitutionInactiveError(Exception):
    pass


class RuleNotFoundError(Exception):
    pass


class BatchConflictError(Exception):
    pass


class ExternalEventNotFoundError(Exception):
    pass


class DisputeAlreadyResolvedError(Exception):
    pass


class AdjudicationError(Exception):
    pass


ENVELOPE_REQUIRED = (
    "batch_id",
    "sender_id",
    "receiver_id",
    "plan_version",
    "rule_id",
    "sent_at",
)


# ---------------------------------------------------------------------------
# 机构与规则的登记
# ---------------------------------------------------------------------------


def register_institution(
    db: Session,
    *,
    institution_id: str,
    name: str,
    iana_timezone: str,
    signing_secret: str,
    active: bool = True,
) -> dict[str, Any]:
    from zoneinfo import ZoneInfoNotFoundError, ZoneInfo

    try:
        ZoneInfo(iana_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("iana_timezone 不是合法的 IANA 时区") from exc
    row = upsert_institution(
        db,
        institution_id=institution_id,
        name=name,
        iana_timezone=iana_timezone,
        signing_secret=signing_secret,
        active=active,
    )
    return {
        "institution_id": row.institution_id,
        "name": row.name,
        "iana_timezone": row.iana_timezone,
        "active": row.active,
    }


def list_institutions_plain(db: Session) -> list[dict[str, Any]]:
    return [
        {
            "institution_id": r.institution_id,
            "name": r.name,
            "iana_timezone": r.iana_timezone,
            "active": r.active,
        }
        for r in list_institutions(db)
    ]


def publish_rule_version(
    db: Session,
    *,
    rule_id: str,
    sender_id: str,
    receiver_id: str,
    plan_version: str,
    activity_map: dict[str, str],
    cap_seconds: int | None,
) -> dict[str, Any]:
    """登记互认规则新版本。

    规则升级只追加新版本行：已接收批次的 rule_version 已固化，重放仍走
    旧映射；只有新批次会解析到新版本。
    """
    if get_institution(db, sender_id) is None:
        raise InstitutionNotFoundError(f"发送院校 {sender_id} 未登记")
    if get_institution(db, receiver_id) is None:
        raise InstitutionNotFoundError(f"接收院校 {receiver_id} 未登记")
    if get_plan(db, plan_version) is None:
        raise PlanNotFoundError(f"培养方案 {plan_version} 未在本校登记")
    if not activity_map:
        raise ValueError("activity_map 不能为空")
    row = insert_rule_version(
        db,
        rule_id=rule_id,
        sender_id=sender_id,
        receiver_id=receiver_id,
        plan_version=plan_version,
        activity_map=activity_map,
        cap_seconds=cap_seconds,
    )
    return _rule_to_dict(row)


def list_rule_versions_plain(db: Session, rule_id: str) -> list[dict[str, Any]]:
    return [_rule_to_dict(r) for r in list_rule_versions(db, rule_id)]


def _rule_to_dict(row: Any) -> dict[str, Any]:
    return {
        "rule_id": row.rule_id,
        "version": row.version,
        "sender_id": row.sender_id,
        "receiver_id": row.receiver_id,
        "plan_version": row.plan_version,
        "activity_map": dict(row.activity_map),
        "cap_seconds": row.cap_seconds,
        "active": row.active,
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rule_snapshot(version_row: Any) -> RuleSnapshot:
    return RuleSnapshot(
        rule_id=version_row.rule_id,
        version=version_row.version,
        sender_id=version_row.sender_id,
        receiver_id=version_row.receiver_id,
        plan_version=version_row.plan_version,
        activity_map=dict(version_row.activity_map),
        cap_seconds=version_row.cap_seconds,
    )


def _validate_envelope_shape(envelope: dict[str, Any]) -> None:
    for key in ENVELOPE_REQUIRED:
        if not envelope.get(key):
            raise ValueError(f"批次缺少必填字段: {key}")
    if not isinstance(envelope.get("events"), list):
        raise ValueError("批次 events 必须是数组")


def _resolve_rule(db: Session, envelope: dict[str, Any]) -> Any:
    claimed = envelope.get("rule_version")
    if claimed:
        row = get_rule_version(db, envelope["rule_id"], int(claimed))
        if row is None:
            raise RuleNotFoundError(
                f"规则 {envelope['rule_id']} v{claimed} 不存在"
            )
        _assert_rule_matches(row, envelope)
        return row
    row = get_active_rule(
        db, envelope["sender_id"], envelope["receiver_id"], envelope["plan_version"]
    )
    if row is None:
        raise RuleNotFoundError(
            "院校间没有生效的互认规则，且批次未声明 rule_version"
        )
    return row


def _frozen_rule(db: Session, envelope: dict[str, Any], version: int) -> Any:
    """按批次固化的版本号取回规则（恢复/重发路径），不随规则升级漂移。"""
    row = get_rule_version(db, envelope["rule_id"], version)
    if row is None:
        raise RuleNotFoundError(f"规则 {envelope['rule_id']} v{version} 已丢失")
    _assert_rule_matches(row, envelope)
    return row


def _assert_rule_matches(row: Any, envelope: dict[str, Any]) -> None:
    if (
        row.sender_id != envelope["sender_id"]
        or row.receiver_id != envelope["receiver_id"]
        or row.plan_version != envelope["plan_version"]
    ):
        raise RuleNotFoundError("规则版本与批次的院校/培养方案不匹配")


def _stage_local_events(
    db: Session, plan_version: str, events: list[dict[str, Any]]
) -> int:
    """把映射后的事件幂等暂存到本地事件流（不提交，由调用方统一提交）。"""
    inserted = 0
    for e in events:
        stmt = sqlite_insert(EventModel).values(
            event_id=e["event_id"],
            plan_version=plan_version,
            student_id=e["student_id"],
            event_type=e["event_type"],
            payload=e["payload"],
        ).on_conflict_do_nothing(
            index_elements=["event_id", "plan_version"]
        ).returning(EventModel.id)
        if db.execute(stmt).scalar_one_or_none() is not None:
            inserted += 1
    return inserted


def _registered_blockers(
    db: Session,
    plan_version: str,
    student_ids: set[str],
) -> list[dict[str, Any]]:
    """接收/裁决路径的阻塞集：全部已进入本地事件流的签到。

    包含当前批次此前已采信的事件，乱序重发、重启恢复或裁决采信时都
    不会与已计秒区间重复计时。
    """
    return load_registered_checkins(db, plan_version, student_ids)


def _blockers_excluding_batch(
    db: Session,
    plan_version: str,
    student_ids: set[str],
    batch_id: str,
) -> list[dict[str, Any]]:
    """对账重映射用的阻塞集：排除本批全部事件。

    批次内的互判由 map_batch 的批内判重完成；排除后重建的环境与该批
    首次接收时一致，用于检测乱序/恢复造成的状态漂移。
    """
    registered = load_registered_checkins(db, plan_version, student_ids)
    own_local_ids = {
        r.local_event_id
        for r in list_external_events(db, batch_id=batch_id)
        if r.local_event_id
    }
    return [
        r for r in registered if r.get("local_event_id") not in own_local_ids
    ]


def _batch_to_dict(batch: ExchangeBatch, *, resumed: bool = False) -> dict[str, Any]:
    return {
        "batch_id": batch.batch_id,
        "sender_id": batch.sender_id,
        "receiver_id": batch.receiver_id,
        "plan_version": batch.plan_version,
        "rule_id": batch.rule_id,
        "rule_version": batch.rule_version,
        "status": batch.status,
        "sent_at": batch.sent_at,
        "expected_count": batch.expected_count,
        "accepted_count": batch.accepted_count,
        "duplicate_count": batch.duplicate_count,
        "quarantined_count": batch.quarantined_count,
        "payload_digest": batch.payload_digest,
        "resumed": resumed,
    }


# ---------------------------------------------------------------------------
# 接收 / 校验 / 应用 / 恢复
# ---------------------------------------------------------------------------


def receive_batch(
    db: Session, envelope: dict[str, Any], signature: str | None
) -> dict[str, Any]:
    """接收外校批次：验签 → 固化规则 → 映射 → 写入本地事件流。

    同一 batch_id 以相同载荷重发（或服务重启后补发）执行幂等恢复：
    已裁决争议保持裁决结论，其余登记重新对齐，本地事件流不产生重复。
    """
    _validate_envelope_shape(envelope)

    sender = get_institution(db, envelope["sender_id"])
    if sender is None:
        raise InstitutionNotFoundError(f"发送院校 {envelope['sender_id']} 未登记")
    if not sender.active:
        raise InstitutionInactiveError(f"发送院校 {sender.institution_id} 已停用")
    receiver = get_institution(db, envelope["receiver_id"])
    if receiver is None:
        raise InstitutionNotFoundError(f"接收院校 {envelope['receiver_id']} 未登记")
    if get_plan(db, envelope["plan_version"]) is None:
        raise PlanNotFoundError(
            f"培养方案 {envelope['plan_version']} 未在本校登记"
        )

    # 不可信载荷必须先过签名校验，才允许落库。
    verify_signature(envelope, sender.signing_secret, signature)
    digest = payload_digest(envelope)

    existing = get_batch(db, envelope["batch_id"])
    if existing is not None and existing.payload_digest != digest:
        raise BatchConflictError(
            f"批次 {envelope['batch_id']} 已存在但载荷摘要不一致"
        )

    rule_row = (
        _frozen_rule(db, envelope, existing.rule_version)
        if existing is not None
        else _resolve_rule(db, envelope)
    )
    rule = _rule_snapshot(rule_row)

    if existing is None:
        # 原样保存信封，保证重启后重验签的字节一致；规则版本固化在
        # 批次列上，之后规则升级不回溯本批。
        row = insert_batch(
            db,
            batch_id=envelope["batch_id"],
            sender_id=envelope["sender_id"],
            receiver_id=envelope["receiver_id"],
            plan_version=envelope["plan_version"],
            rule_id=rule.rule_id,
            rule_version=rule.version,
            signature=signature or "",
            sent_at=str(envelope["sent_at"]),
            payload_digest=digest,
            envelope=envelope,
            expected_count=len(envelope["events"]),
        )
        if row is None:
            # 并发首达竞争：重读后走恢复路径。
            db.rollback()
            existing = get_batch(db, envelope["batch_id"])
            assert existing is not None
            if existing.payload_digest != digest:
                raise BatchConflictError("批次并发送达且载荷不一致")

    batch = existing or get_batch(db, envelope["batch_id"])
    assert batch is not None
    resumed = existing is not None

    student_ids = {str(e.get("student_id", "")) for e in envelope["events"]} - {""}
    blockers = _registered_blockers(db, envelope["plan_version"], student_ids)

    mapping = map_batch(envelope["events"], rule, already_registered=blockers)

    # 已裁决争议在恢复时保持裁决结论，不被重新映射覆盖。
    previously = {
        (r.sender_id, r.external_event_id): r
        for r in list_external_events(db, batch_id=batch.batch_id)
    }

    local_to_insert: list[dict[str, Any]] = []
    for m in mapping.mapped:
        prior = previously.get((envelope["sender_id"], m.external_event_id))
        if prior is not None:
            # 首次映射结论（含争议待定）在同批次重发/重启时永久保持；
            # 争议的唯一出口是人工裁决，不随重放自动翻转。
            # 仅当本地事件行缺失（灾备恢复）时，重映射会重新产出
            # accepted 事件，此时幂等补齐；已在流中的会被判为 duplicate。
            if m.status == "accepted" and m.local_event is not None:
                local_to_insert.append(m.local_event)
            continue
        local_id = m.local_event_id if m.status == "accepted" else None
        original = _raw_event(
            envelope["events"], m.external_event_id, m.sender_seq
        )
        upsert_external_event(
            db,
            batch_id=batch.batch_id,
            sender_id=envelope["sender_id"],
            receiver_id=envelope["receiver_id"],
            plan_version=envelope["plan_version"],
            external_event_id=m.external_event_id,
            sender_seq=m.sender_seq,
            student_id=m.student_id,
            event_type=str(original.get("event_type", "checkin")),
            payload=_raw_payload(envelope["events"], m.external_event_id, m.sender_seq),
            status=m.status,
            local_event_id=local_id,
            rule_id=rule.rule_id,
            rule_version=rule.version,
            reason=m.reason,
            occurred_at_utc=m.occurred_at_utc,
        )
        if m.status == "accepted" and m.local_event is not None:
            local_to_insert.append(m.local_event)

    _stage_local_events(db, envelope["plan_version"], local_to_insert)

    rows = list_external_events(db, batch_id=batch.batch_id)
    accepted = sum(1 for r in rows if r.status == "accepted")
    duplicates = sum(1 for r in rows if r.status == "duplicate")
    quarantined = sum(1 for r in rows if r.status == "quarantined")
    db.commit()

    mark_batch_status(
        db,
        batch.batch_id,
        status="applied",
        accepted_count=accepted,
        duplicate_count=duplicates,
        quarantined_count=quarantined,
    )

    stored = get_batch(db, batch.batch_id)
    assert stored is not None
    return _batch_to_dict(stored, resumed=resumed)


def validate_batch(
    db: Session, envelope: dict[str, Any], signature: str | None
) -> dict[str, Any]:
    """无副作用校验：验签、机构/规则解析、试映射，但不落库。

    供发送方在正式提交前预检，或供运维核查“若接收会发生什么”。
    """
    _validate_envelope_shape(envelope)
    sender = get_institution(db, envelope["sender_id"])
    if sender is None:
        raise InstitutionNotFoundError(f"发送院校 {envelope['sender_id']} 未登记")
    if not sender.active:
        raise InstitutionInactiveError(f"发送院校 {sender.institution_id} 已停用")
    if get_institution(db, envelope["receiver_id"]) is None:
        raise InstitutionNotFoundError(f"接收院校 {envelope['receiver_id']} 未登记")
    if get_plan(db, envelope["plan_version"]) is None:
        raise PlanNotFoundError(
            f"培养方案 {envelope['plan_version']} 未在本校登记"
        )
    verify_signature(envelope, sender.signing_secret, signature)
    rule = _rule_snapshot(_resolve_rule(db, envelope))

    existing = get_batch(db, envelope["batch_id"])
    duplicate_batch = existing is not None
    if duplicate_batch and existing.payload_digest != payload_digest(envelope):
        raise BatchConflictError(
            f"批次 {envelope['batch_id']} 已存在但载荷摘要不一致"
        )

    student_ids = {str(e.get("student_id", "")) for e in envelope["events"]} - {""}
    blockers = _registered_blockers(db, envelope["plan_version"], student_ids)
    mapping = map_batch(envelope["events"], rule, already_registered=blockers)
    return {
        "batch_id": envelope["batch_id"],
        "signature_valid": True,
        "duplicate_batch": duplicate_batch,
        "rule_id": rule.rule_id,
        "rule_version": rule.version,
        "expected_count": len(envelope["events"]),
        "accepted_count": len(mapping.accepted),
        "duplicate_count": len(mapping.duplicates),
        "quarantined_count": len(mapping.quarantined),
        "quarantined": [
            {"external_event_id": m.external_event_id, "reason": m.reason}
            for m in mapping.quarantined
        ],
    }


def resume_batch(db: Session, batch_id: str) -> dict[str, Any]:
    """重启/补发恢复：用落库信封与固化规则版本重新对齐批次。"""
    batch = get_batch(db, batch_id)
    if batch is None:
        raise ExternalEventNotFoundError(f"批次 {batch_id} 不存在")
    return receive_batch(db, batch.envelope, batch.signature)


def _raw_event(
    events: list[dict[str, Any]], external_event_id: str, seq: int
) -> dict[str, Any]:
    for e in events:
        if str(e.get("event_id")) == external_event_id and int(
            e.get("sender_seq", 0)
        ) == seq:
            return e
    return {}


def _raw_payload(
    events: list[dict[str, Any]], external_event_id: str, seq: int
) -> dict[str, Any]:
    return dict(_raw_event(events, external_event_id, seq).get("payload") or {})


# ---------------------------------------------------------------------------
# 对账
# ---------------------------------------------------------------------------


def reconcile_batch(db: Session, batch_id: str) -> dict[str, Any]:
    """对单个批次做四重对账：签名、摘要、映射登记、本地事件流。"""
    batch = get_batch(db, batch_id)
    if batch is None:
        raise ExternalEventNotFoundError(f"批次 {batch_id} 不存在")

    discrepancies: list[str] = []
    sender = get_institution(db, batch.sender_id)
    signature_valid = False
    if sender is not None:
        try:
            verify_signature(batch.envelope, sender.signing_secret, batch.signature)
            signature_valid = True
        except SignatureError:
            signature_valid = False
    else:
        discrepancies.append("sender_not_registered")
    if not signature_valid:
        discrepancies.append("signature_invalid")

    digest_match = payload_digest(batch.envelope) == batch.payload_digest
    if not digest_match:
        discrepancies.append("payload_digest_mismatch")

    rule_row = get_rule_version(db, batch.rule_id, batch.rule_version)
    if rule_row is None:
        discrepancies.append("rule_version_missing")
        rule = None
    else:
        rule = _rule_snapshot(rule_row)

    rows = list_external_events(db, batch_id=batch_id)
    counts = {
        "accepted": sum(1 for r in rows if r.status == "accepted"),
        "duplicate": sum(1 for r in rows if r.status == "duplicate"),
        "quarantined": sum(1 for r in rows if r.status == "quarantined"),
        "rejected": sum(1 for r in rows if r.status == "rejected"),
    }
    stored_counts = {
        "accepted": batch.accepted_count,
        "duplicate": batch.duplicate_count,
        "quarantined": batch.quarantined_count,
        "rejected": sum(1 for r in rows if r.status == "rejected"),
    }
    if counts != stored_counts:
        discrepancies.append("batch_counts_stale")

    envelope_ids = {str(e.get("event_id", "")) for e in batch.envelope["events"]}
    registered_ids = {r.external_event_id for r in rows}
    if envelope_ids != registered_ids:
        discrepancies.append("event_registration_mismatch")

    # accepted 登记必须能在本地事件流找到；非 accepted 必须找不到。
    local_events = {
        row.event_id: row
        for row in db.execute(
            select(EventModel).where(EventModel.plan_version == batch.plan_version)
        ).scalars()
    }
    for r in rows:
        if r.status == "accepted":
            local = local_events.get(r.local_event_id or "") if r.local_event_id else None
            if local is None:
                discrepancies.append(f"accepted_event_missing_local:{r.external_event_id}")
        elif r.local_event_id and r.local_event_id in local_events and r.status in {
            "rejected",
            "quarantined",
        } and not r.resolution:
            discrepancies.append(f"disputed_event_leaked_into_stream:{r.external_event_id}")

    # 反向：本地 ext 事件必须有登记且属于某个批次。
    ext_rows = list_external_events(db, plan_version=batch.plan_version)
    ext_local_ids = {r.local_event_id for r in ext_rows if r.local_event_id}
    for eid in local_events:
        if eid.startswith("ext:") and eid not in ext_local_ids:
            discrepancies.append(f"local_event_without_registration:{eid}")

    # 用固化规则重新映射，结果应与现存未裁决登记一致（乱序/恢复收敛性）。
    remap_consistent = True
    if rule is not None and not any(
        d.startswith(("rule_version_missing", "payload_digest_mismatch"))
        for d in discrepancies
    ):
        student_ids = {
            str(e.get("student_id", "")) for e in batch.envelope["events"]
        } - {""}
        blockers = _blockers_excluding_batch(
            db, batch.plan_version, student_ids, batch.batch_id
        )
        remapped = map_batch(batch.envelope["events"], rule, already_registered=blockers)
        current = {
            (r.sender_id, r.external_event_id): r for r in rows if not r.resolution
        }
        for m in remapped.mapped:
            row = current.get((batch.sender_id, m.external_event_id))
            if row is not None and row.status != m.status:
                remap_consistent = False
                discrepancies.append(
                    f"remap_status_drift:{m.external_event_id}:{row.status}!={m.status}"
                )

    balanced = not discrepancies
    return {
        "batch_id": batch_id,
        "status": batch.status,
        "signature_valid": signature_valid,
        "payload_digest_match": digest_match,
        "remap_consistent": remap_consistent,
        "counts": counts,
        "stored_counts": stored_counts,
        "pending_disputes": counts["quarantined"],
        "balanced": balanced,
        "discrepancies": discrepancies,
    }


def reconcile_plan(db: Session, plan_version: str) -> dict[str, Any]:
    batches = list_batches(db, plan_version=plan_version)
    reports = [reconcile_batch(db, b.batch_id) for b in batches]
    return {
        "plan_version": plan_version,
        "batch_count": len(reports),
        "balanced": all(r["balanced"] for r in reports),
        "pending_disputes": sum(r["pending_disputes"] for r in reports),
        "batches": reports,
    }


# ---------------------------------------------------------------------------
# 裁决
# ---------------------------------------------------------------------------


def adjudicate(
    db: Session,
    batch_id: str,
    external_event_id: str,
    *,
    decision: str,
    adjudicator: str,
    note: str,
) -> dict[str, Any]:
    """对争议事件做人工裁决；裁决前事件保持 quarantined 且不计学时。"""
    if decision not in {"accept", "reject"}:
        raise AdjudicationError("decision 必须是 accept 或 reject")
    if not adjudicator.strip():
        raise AdjudicationError("裁决必须记录 adjudicator")
    row = get_external_event(db, batch_id, external_event_id)
    if row is None:
        raise ExternalEventNotFoundError(
            f"批次 {batch_id} 中没有外校事件 {external_event_id}"
        )
    if row.resolution:
        raise DisputeAlreadyResolvedError(
            f"事件已由 {row.adjudicator} 裁决为 {row.resolution}"
        )
    if row.status != "quarantined":
        raise AdjudicationError(f"事件状态为 {row.status}，无需裁决")

    rule_row = get_rule_version(db, row.rule_id, row.rule_version)
    if rule_row is None:
        raise AdjudicationError("固化的规则版本已丢失，无法裁决")

    batch = get_batch(db, batch_id)
    assert batch is not None

    if decision == "reject":
        row.resolution = "rejected"
        row.status = "rejected"
        row.adjudicator = adjudicator.strip()
        row.adjudication_note = note.strip()
        row.adjudicated_at = _now()
        db.commit()
    else:
        # 人工采信：以固化规则重新映射该单条（相对当前阻塞集）。
        # 规则未覆盖的活动类型可由裁决人人工覆盖为 regular，留痕；
        # 时间戳非法的事件即使人工也无法采信。
        blockers = _registered_blockers(
            db, row.plan_version, {row.student_id}
        )
        forced_rule = _rule_snapshot(rule_row)
        sender_type = str(row.payload.get("activity_type", "regular"))
        manual_override = sender_type not in forced_rule.activity_map
        if manual_override:
            forced_rule = RuleSnapshot(
                rule_id=forced_rule.rule_id,
                version=forced_rule.version,
                sender_id=forced_rule.sender_id,
                receiver_id=forced_rule.receiver_id,
                plan_version=forced_rule.plan_version,
                activity_map={**forced_rule.activity_map, sender_type: "regular"},
                cap_seconds=forced_rule.cap_seconds,
            )
        single = [
            {
                "event_id": row.external_event_id,
                "sender_seq": row.sender_seq,
                "event_type": row.event_type,
                "student_id": row.student_id,
                "payload": row.payload,
            }
        ]
        mapping = map_batch(single, forced_rule, already_registered=blockers)
        m = mapping.mapped[0] if mapping.mapped else None
        if m is None or m.status == "quarantined":
            reason = m.reason if m is not None else "映射失败"
            raise AdjudicationError(f"无法采信该事件: {reason}")

        local_id: str | None = None
        if m.status == "accepted" and m.local_event is not None:
            if manual_override:
                m.local_event["payload"]["source"]["manual_override"] = True
            _stage_local_events(db, row.plan_version, [m.local_event])
            local_id = m.local_event_id
        row.resolution = "accepted"
        row.status = m.status  # accepted 或 duplicate（与现存活动完全重叠）
        row.local_event_id = local_id
        row.adjudicator = adjudicator.strip()
        row.adjudication_note = note.strip()
        row.adjudicated_at = _now()
        if m.reason:
            row.reason = m.reason
        db.commit()

    rows = list_external_events(db, batch_id=batch_id)
    mark_batch_status(
        db,
        batch_id,
        status="applied",
        accepted_count=sum(1 for r in rows if r.status == "accepted"),
        duplicate_count=sum(1 for r in rows if r.status == "duplicate"),
        quarantined_count=sum(1 for r in rows if r.status == "quarantined"),
    )
    refreshed = get_external_event(db, batch_id, external_event_id)
    assert refreshed is not None
    return _external_event_to_dict(refreshed)


def get_batch_or_404(db: Session, batch_id: str) -> ExchangeBatch:
    batch = get_batch(db, batch_id)
    if batch is None:
        raise ExternalEventNotFoundError(f"批次 {batch_id} 不存在")
    return batch


def list_external_events_plain(
    db: Session, *, batch_id: str | None = None
) -> list[ExternalEvent]:
    return list_external_events(db, batch_id=batch_id)


def external_event_plain(row: ExternalEvent) -> dict[str, Any]:
    return _external_event_to_dict(row)


def _external_event_to_dict(row: ExternalEvent) -> dict[str, Any]:    return {
        "batch_id": row.batch_id,
        "sender_id": row.sender_id,
        "receiver_id": row.receiver_id,
        "plan_version": row.plan_version,
        "external_event_id": row.external_event_id,
        "sender_seq": row.sender_seq,
        "student_id": row.student_id,
        "event_type": row.event_type,
        "status": row.status,
        "local_event_id": row.local_event_id,
        "rule_id": row.rule_id,
        "rule_version": row.rule_version,
        "reason": row.reason,
        "resolution": row.resolution or None,
        "adjudicator": row.adjudicator or None,
        "adjudication_note": row.adjudication_note or None,
        "adjudicated_at": row.adjudicated_at.isoformat()
        if row.adjudicated_at is not None
        else None,
        "occurred_at_utc": row.occurred_at_utc.isoformat()
        if row.occurred_at_utc is not None
        else None,
    }


# ---------------------------------------------------------------------------
# 来源解释
# ---------------------------------------------------------------------------


def explain_provenance(
    db: Session, plan_version: str, student_id: str
) -> dict[str, Any]:
    """解释学生每个计秒事件的来源：本校采集或某外校某批次/规则版本。"""
    if get_plan(db, plan_version) is None:
        raise PlanNotFoundError(f"培养方案 {plan_version} 未在本校登记")

    rows = list(db.execute(
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.student_id == student_id)
        .order_by(EventModel.event_id)
    ).scalars())

    ext_index = {
        r.local_event_id: r
        for r in list_external_events(db, plan_version=plan_version)
        if r.local_event_id
    }

    sources: list[dict[str, Any]] = []
    seconds_by_origin: dict[str, int] = {"local": 0}
    seconds_by_sender: dict[str, int] = {}
    for row in rows:
        payload = dict(row.payload)
        source_meta = payload.get("source") if row.event_type == "checkin" else None
        seconds = 0
        start_utc = end_utc = None
        if row.event_type == "checkin":
            start_utc = payload.get("check_in_at")
            end_utc = payload.get("check_out_at")
            try:
                from datetime import datetime as _dt

                seconds = int(
                    (
                        _dt.fromisoformat(str(end_utc).replace("Z", "+00:00"))
                        - _dt.fromisoformat(str(start_utc).replace("Z", "+00:00"))
                    ).total_seconds()
                )
            except (TypeError, ValueError):
                seconds = 0

        if source_meta is None:
            origin = "local"
            seconds_by_origin["local"] += seconds
            sources.append(
                {
                    "local_event_id": row.event_id,
                    "origin": origin,
                    "event_type": row.event_type,
                    "status": "accepted",
                    "seconds": seconds,
                    "interval_utc": [start_utc, end_utc],
                }
            )
            continue

        ext = ext_index.get(row.event_id)
        sender_id = source_meta.get("sender_id", "")
        origin = "external"
        seconds_by_sender[sender_id] = seconds_by_sender.get(sender_id, 0) + seconds
        sources.append(
            {
                "local_event_id": row.event_id,
                "origin": origin,
                "event_type": row.event_type,
                "status": ext.status if ext is not None else "accepted",
                "resolution": ext.resolution if ext is not None and ext.resolution else None,
                "sender_id": sender_id,
                "external_event_id": source_meta.get("external_event_id"),
                "batch_id": ext.batch_id if ext is not None else None,
                "rule_id": source_meta.get("rule_id"),
                "rule_version": source_meta.get("rule_version"),
                "sender_timezone_hint": source_meta.get("sender_timezone_hint", ""),
                "overlap_trimmed_seconds": source_meta.get(
                    "overlap_trimmed_seconds", 0
                ),
                "cap_trimmed_seconds": source_meta.get("cap_trimmed_seconds", 0),
                "manual_override": source_meta.get("manual_override", False),
                "adjudicator": ext.adjudicator if ext is not None else "",
                "adjudication_note": ext.adjudication_note if ext is not None else "",
                "seconds": seconds,
                "interval_utc": [start_utc, end_utc],
            }
        )

    rules_seen: dict[tuple[str, int], dict[str, Any]] = {}
    for s in sources:
        if s["origin"] == "external":
            key = (s["rule_id"], s["rule_version"])
            if key not in rules_seen:
                vr = get_rule_version(db, s["rule_id"], s["rule_version"])
                rules_seen[key] = (
                    {
                        "rule_id": vr.rule_id,
                        "version": vr.version,
                        "activity_map": dict(vr.activity_map),
                        "cap_seconds": vr.cap_seconds,
                    }
                    if vr is not None
                    else {"rule_id": s["rule_id"], "version": s["rule_version"], "missing": True}
                )

    quarantined = [
        _external_event_to_dict(r)
        for r in list_external_events(
            db, plan_version=plan_version, status="quarantined"
        )
        if r.student_id == student_id
    ]

    return {
        "plan_version": plan_version,
        "student_id": student_id,
        "sources": sources,
        "seconds_by_origin": seconds_by_origin,
        "seconds_by_sender": seconds_by_sender,
        "rules_applied": list(rules_seen.values()),
        "pending_disputes": quarantined,
    }
