"""跨校互认领域服务。

流程:
1. 接收:校验机构身份与批次 HMAC 签名,原始批次落库(RECEIVED)。
2. 追平:每个 (本校方案, 合作院校) 通道按序号连续入账,乱序批次挂起等待缺口。
3. 入账:外校事件按批次绑定的规则版本映射到本校事件流,处理重复来源、
   同活动同时段抑制与部分重叠争议。
4. 裁决:争议记录裁决前保持待定,裁决后重放即生效。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ..core.clock import to_utc
from ..core.replay import EventType
from ..repository import get_plan, load_events
from . import repository as repo
from .signing import verify_signature

LOCAL_EVENT_PREFIX = "EXG"

# 入账后计入回放的状态。
ACTIVE_STATUSES = frozenset({"ACCEPTED", "UPHELD"})


class ExchangeError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 422):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass
class _Interval:
    start_utc: datetime
    end_utc: datetime
    activity_id: str


# ---------------------------------------------------------------------------
# 机构与规则登记
# ---------------------------------------------------------------------------

def register_institution(
    db: Session,
    *,
    code: str,
    display_name: str,
    iana_timezone: str,
    verification_key: str,
    is_active: bool = True,
) -> dict[str, Any]:
    row = repo.upsert_institution(
        db,
        code=code,
        display_name=display_name,
        iana_timezone=iana_timezone,
        verification_key=verification_key,
        is_active=is_active,
    )
    return _institution_dict(row)


def _institution_dict(row: Any) -> dict[str, Any]:
    return {
        "code": row.code,
        "display_name": row.display_name,
        "iana_timezone": row.iana_timezone,
        "is_active": bool(row.is_active),
    }


def list_institutions(db: Session) -> list[dict[str, Any]]:
    return [_institution_dict(r) for r in repo.list_institutions(db)]


def create_rule(
    db: Session,
    *,
    rule_code: str,
    local_plan_version: str,
    partner_institution_code: str,
    event_type_mapping: dict[str, str],
    partner_plan_version: str | None = None,
    accepted_activity_types: list[str] | None = None,
) -> dict[str, Any]:
    if get_plan(db, local_plan_version) is None:
        raise ExchangeError("PLAN_NOT_FOUND", "本校培养方案不存在", 404)
    if repo.get_institution(db, partner_institution_code) is None:
        raise ExchangeError("INSTITUTION_NOT_FOUND", "合作院校未登记", 404)
    allowed = {t.value for t in EventType}
    bad = set(event_type_mapping.values()) - allowed
    if bad:
        raise ExchangeError(
            "INVALID_MAPPING", f"映射的目标事件类型非法: {sorted(bad)}"
        )
    row = repo.create_rule(
        db,
        rule_code=rule_code,
        local_plan_version=local_plan_version,
        partner_institution_code=partner_institution_code,
        partner_plan_version=partner_plan_version,
        event_type_mapping=event_type_mapping,
        accepted_activity_types=accepted_activity_types,
    )
    return _rule_dict(row)


def publish_rule(db: Session, rule_code: str, version: int) -> dict[str, Any]:
    row = repo.publish_rule(db, rule_code, version)
    if row is None:
        raise ExchangeError("RULE_NOT_FOUND", "规则版本不存在", 404)
    return _rule_dict(row)


def _rule_dict(row: Any) -> dict[str, Any]:
    return {
        "rule_code": row.rule_code,
        "version": row.version,
        "local_plan_version": row.local_plan_version,
        "partner_institution_code": row.partner_institution_code,
        "partner_plan_version": row.partner_plan_version,
        "event_type_mapping": dict(row.event_type_mapping),
        "accepted_activity_types": row.accepted_activity_types,
        "status": row.status,
        "published_at": row.published_at.isoformat() if row.published_at else None,
    }


def list_rules(db: Session, rule_code: str | None = None) -> list[dict[str, Any]]:
    return [_rule_dict(r) for r in repo.list_rules(db, rule_code)]


# ---------------------------------------------------------------------------
# 批次接收
# ---------------------------------------------------------------------------

def receive_batch(db: Session, envelope: dict[str, Any]) -> dict[str, Any]:
    required = (
        "batch_id",
        "local_plan_version",
        "partner_institution_code",
        "partner_plan_version",
        "rule_code",
        "rule_version",
        "seq",
        "entries",
        "signature",
    )
    missing = [k for k in required if k not in envelope]
    if missing:
        raise ExchangeError(
            "MALFORMED_BATCH", f"批次缺少字段: {missing}", 400
        )

    partner = repo.get_institution(db, envelope["partner_institution_code"])
    if partner is None:
        raise ExchangeError("INSTITUTION_NOT_FOUND", "合作院校未登记", 404)
    if not partner.is_active:
        raise ExchangeError("INSTITUTION_INACTIVE", "合作院校已停用")

    if not verify_signature(envelope, partner.verification_key):
        raise ExchangeError("SIGNATURE_INVALID", "批次签名校验失败", 401)

    plan = get_plan(db, envelope["local_plan_version"])
    if plan is None:
        raise ExchangeError("PLAN_NOT_FOUND", "本校培养方案不存在", 404)

    rule = repo.get_rule(db, envelope["rule_code"], int(envelope["rule_version"]))
    if rule is None:
        raise ExchangeError("RULE_NOT_FOUND", "批次绑定的互认规则版本不存在", 422)
    if rule.status != "active":
        raise ExchangeError("RULE_NOT_ACTIVE", "互认规则版本尚未生效")
    if (
        rule.local_plan_version != envelope["local_plan_version"]
        or rule.partner_institution_code != envelope["partner_institution_code"]
        or (
            rule.partner_plan_version is not None
            and rule.partner_plan_version != envelope["partner_plan_version"]
        )
    ):
        raise ExchangeError("RULE_MISMATCH", "规则适用范围与批次不匹配")

    seq = int(envelope["seq"])
    if seq <= 0:
        raise ExchangeError("MALFORMED_BATCH", "批次序号必须为正整数", 400)

    values = {
        "batch_id": envelope["batch_id"],
        "local_plan_version": envelope["local_plan_version"],
        "partner_institution_code": envelope["partner_institution_code"],
        "partner_plan_version": envelope["partner_plan_version"],
        "rule_code": envelope["rule_code"],
        "rule_version": int(envelope["rule_version"]),
        "seq": seq,
        "status": "RECEIVED",
        "entries_count": len(envelope["entries"]),
        "signature": envelope["signature"],
        "raw_entries": _normalize_entries(envelope["entries"]),
    }
    outcome = repo.insert_received_batch(db, values)
    if outcome == "duplicate_id":
        existing = repo.get_batch(db, envelope["batch_id"])
        # 重传也触发追平,以防首传后进程崩溃。
        _drain_channel(db, existing.local_plan_version, existing.partner_institution_code)
        existing = repo.get_batch(db, envelope["batch_id"])
        result = _batch_dict(existing)
        result["duplicate_delivery"] = True
        return result
    if outcome == "duplicate_seq":
        raise ExchangeError(
            "SEQ_CONFLICT", "该通道已存在相同序号但批次号不同的批次", 409
        )

    batch = repo.get_batch(db, envelope["batch_id"])
    assert batch is not None
    _drain_channel(db, batch.local_plan_version, batch.partner_institution_code)
    batch = repo.get_batch(db, envelope["batch_id"])
    return _batch_dict(batch)


def _normalize_entries(entries: Any) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        raise ExchangeError("MALFORMED_BATCH", "entries 必须是列表", 400)
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ExchangeError("MALFORMED_BATCH", "批次条目必须是对象", 400)
        for key in ("event_id", "event_type", "student_id", "payload"):
            if key not in entry:
                raise ExchangeError(
                    "MALFORMED_BATCH", f"批次条目缺少字段 {key}", 400
                )
        normalized.append(
            {
                "event_id": str(entry["event_id"]),
                "event_type": str(entry["event_type"]),
                "student_id": str(entry["student_id"]),
                "payload": dict(entry["payload"]),
            }
        )
    return normalized


def recover_pending(db: Session) -> dict[str, Any]:
    """重启恢复:重新驱动所有停留在 RECEIVED 的通道。"""
    drained: list[str] = []
    for batch in repo.list_pending_batches(db):
        channel = (batch.local_plan_version, batch.partner_institution_code)
        if channel in drained:
            continue
        drained.append(channel)
        _drain_channel(db, *channel)
    return {
        "channels_recovered": len(drained),
        "batches": [
            _batch_dict(b)
            for b in repo.list_batches(db)
        ],
    }


def _drain_channel(db: Session, local_plan_version: str, partner_code: str) -> None:
    """按序号连续入账;遇到缺口或异常批次则挂起,等待后续重传。"""
    pending = {
        b.seq: b
        for b in repo.list_batches(
            db,
            local_plan_version=local_plan_version,
            partner_institution_code=partner_code,
            status="RECEIVED",
        )
    }
    if not pending:
        return
    ingested_seq = repo.ingested_seq_for(
        db,
        next(iter(pending.values())),
    )
    expected = ingested_seq + 1
    while expected in pending:
        batch = pending.pop(expected)
        try:
            counters = _process_batch(db, batch)
            repo.mark_batch_ingested(db, batch.batch_id, counters)
        except Exception:
            # 条目逐笔提交且重放幂等,保留在 RECEIVED 等待恢复/重传。
            db.rollback()
            repo.mark_batch_received(db, batch.batch_id, "PROCESSING_RETRY")
            return
        expected += 1


# ---------------------------------------------------------------------------
# 批次入账
# ---------------------------------------------------------------------------

def _local_event_id(partner_code: str, source_event_id: str) -> str:
    return f"{LOCAL_EVENT_PREFIX}:{partner_code}:{source_event_id}"


def _load_intervals(db: Session, plan_version: str) -> dict[str, list[_Interval]]:
    """读取方案下已生效(含争议待定)签到的 UTC 区间,用于重复/冲突识别。"""
    result: dict[str, list[_Interval]] = {}
    for event in load_events(db, plan_version):
        if event.event_type != EventType.CHECKIN:
            continue
        if event.source == "exchange" and event.exchange_status not in (
            ACTIVE_STATUSES | {"DISPUTED"}
        ):
            continue
        if event.suppressed:
            continue
        try:
            start = to_utc(
                datetime.fromisoformat(event.payload["check_in_at"])
            )
            end = to_utc(datetime.fromisoformat(event.payload["check_out_at"]))
        except (KeyError, TypeError, ValueError):
            continue
        result.setdefault(event.student_id, []).append(
            _Interval(
                start_utc=start,
                end_utc=end,
                activity_id=str(event.payload.get("activity_id", "")),
            )
        )
    for intervals in result.values():
        intervals.sort(key=lambda iv: iv.start_utc)
    return result


def _classify_overlap(
    intervals: list[_Interval],
    *,
    start: datetime,
    end: datetime,
    activity_id: str,
) -> str | None:
    """返回 'suppressed'(同活动被既有记录覆盖)/ 'disputed'(时段冲突)/ None。"""
    conflict = False
    for iv in intervals:
        if start < iv.end_utc and end > iv.start_utc:
            if (
                iv.activity_id == activity_id
                and iv.start_utc <= start
                and end <= iv.end_utc
            ):
                return "suppressed"
            conflict = True
    return "disputed" if conflict else None


def _process_batch(db: Session, batch: Any) -> dict[str, int]:
    rule = repo.get_rule(db, batch.rule_code, batch.rule_version)
    assert rule is not None
    mapping = dict(rule.event_type_mapping)
    accepted_types = rule.accepted_activity_types

    # 崩溃恢复:批次条目已部分或全部写入时,直接汇总既有结果,绝不重复入账。
    existing = {
        row.source_event_id: row
        for row in repo.list_exchange_events(db, batch_id=batch.batch_id)
    }

    entries = sorted(
        batch.raw_entries,
        key=lambda e: (e["event_type"] == "mentor_confirm", e["event_id"]),
    )

    # 本批次新引入的区间也参与后续条目的冲突识别。
    intervals = _load_intervals(db, batch.local_plan_version)

    counters = {
        "accepted_count": 0,
        "skipped_count": 0,
        "disputed_count": 0,
        "suppressed_count": 0,
    }
    for status in existing.values():
        counters[_counter_for(status.status, status.suppress_reason)] += 1

    for entry in entries:
        source_eid = entry["event_id"]
        if source_eid in existing:
            continue  # 恢复时幂等跳过
        counters.update(
            _map_and_insert(
                db,
                batch=batch,
                rule_mapping=mapping,
                accepted_types=accepted_types,
                entry=entry,
                intervals=intervals,
            )
        )
    return counters


def _counter_for(status: str, suppress_reason: str | None) -> str:
    if status == "DISPUTED":
        return "disputed_count"
    if status == "SKIPPED":
        return "skipped_count"
    if suppress_reason:
        return "suppressed_count"
    return "accepted_count"


def _map_and_insert(
    db: Session,
    *,
    batch: Any,
    rule_mapping: dict[str, str],
    accepted_types: list[str] | None,
    entry: dict[str, Any],
    intervals: dict[str, list[_Interval]],
) -> dict[str, int]:
    partner_code = batch.partner_institution_code
    source_eid = entry["event_id"]
    student_id = entry["student_id"]
    source_type = entry["event_type"]
    payload = dict(entry["payload"])
    local_eid = _local_event_id(partner_code, source_eid)

    # 1) 重复来源:同一合作院校的同一事件在后续批次再次出现,不重复计时。
    #    既有台账即权威记录,重复投递只计入批次统计。
    if repo.get_exchange_event(db, partner_code, source_eid) is not None:
        return {"skipped_count": 1}

    mapped_type = rule_mapping.get(source_type)
    if mapped_type is None:
        status, reason, suppressed = "SKIPPED", "unmapped_event_type", False
        payload_out: dict[str, Any] = {}
    elif mapped_type == EventType.CHECKIN:
        status, reason, suppressed, payload_out = _classify_checkin(
            payload=payload,
            accepted_types=accepted_types,
            student_intervals=intervals.setdefault(student_id, []),
        )
    elif mapped_type == EventType.MENTOR_CONFIRM:
        status, reason, suppressed, payload_out = _classify_confirm(
            db, partner_code=partner_code, payload=payload
        )
    else:  # leave_correction
        status, reason, suppressed, payload_out = "ACCEPTED", None, False, payload

    if status == "SKIPPED":
        # 不入账事件流,仅登记台账(来源引用唯一,此处必为首次出现)。
        repo.insert_ledger_only(
            db,
            exchange_row={
                "local_event_id": local_eid,
                "local_plan_version": batch.local_plan_version,
                "batch_id": batch.batch_id,
                "source_institution_code": partner_code,
                "source_event_id": source_eid,
                "source_student_id": student_id,
                "source_event_type": source_type,
                "mapped_event_type": mapped_type,
                "status": status,
                "reason_code": reason,
                "suppress_reason": None,
                "payload_snapshot": payload,
            },
        )
        return {"skipped_count": 1}

    mapping_row, _ = repo.insert_mapped_event(
        db,
        event_row={
            "event_id": local_eid,
            "plan_version": batch.local_plan_version,
            "student_id": student_id,
            "event_type": mapped_type or source_type,
            "payload": payload_out,
            "source": "exchange",
            "source_institution": partner_code,
            "source_event_id": source_eid,
            "exchange_batch_id": batch.batch_id,
            "exchange_status": status,
            "exchange_suppressed": suppressed,
        },
        exchange_row={
            "local_event_id": local_eid,
            "local_plan_version": batch.local_plan_version,
            "batch_id": batch.batch_id,
            "source_institution_code": partner_code,
            "source_event_id": source_eid,
            "source_student_id": student_id,
            "source_event_type": source_type,
            "mapped_event_type": mapped_type,
            "status": status,
            "reason_code": reason,
            "suppress_reason": "same_activity_overlap" if suppressed else None,
            "payload_snapshot": payload,
        },
    )
    if mapping_row is None:
        # 并发/唯一约束竞争:按重复来源处理(已存在的那笔为准)。
        return {"skipped_count": 1}

    if (
        mapped_type == EventType.CHECKIN
        and status in ACTIVE_STATUSES | {"DISPUTED"}
        and not suppressed
    ):
        intervals[student_id].append(
            _Interval(
                start_utc=to_utc(
                    datetime.fromisoformat(payload_out["check_in_at"])
                ),
                end_utc=to_utc(
                    datetime.fromisoformat(payload_out["check_out_at"])
                ),
                activity_id=str(payload_out.get("activity_id", "")),
            )
        )
        intervals[student_id].sort(key=lambda iv: iv.start_utc)

    return {_counter_for(status, "same_activity_overlap" if suppressed else None): 1}


def _classify_checkin(
    *,
    payload: dict[str, Any],
    accepted_types: list[str] | None,
    student_intervals: list[_Interval],
) -> tuple[str, str | None, bool, dict[str, Any]]:
    try:
        start = to_utc(datetime.fromisoformat(payload["check_in_at"]))
        end = to_utc(datetime.fromisoformat(payload["check_out_at"]))
    except (KeyError, TypeError, ValueError):
        return "SKIPPED", "invalid_payload", False, {}
    if end <= start:
        return "SKIPPED", "invalid_payload", False, {}

    activity_type = str(payload.get("activity_type", "regular"))
    if accepted_types is not None and activity_type not in accepted_types:
        return "DISPUTED", "activity_not_in_rule", False, payload

    overlap = _classify_overlap(
        student_intervals,
        start=start,
        end=end,
        activity_id=str(payload.get("activity_id", "")),
    )
    if overlap == "suppressed":
        return "ACCEPTED", "same_activity_duplicate", True, payload
    if overlap == "disputed":
        return "DISPUTED", "time_overlap", False, payload
    return "ACCEPTED", None, False, payload


def _classify_confirm(
    db: Session, *, partner_code: str, payload: dict[str, Any]
) -> tuple[str, str | None, bool, dict[str, Any]]:
    target_source_id = payload.get("checkin_event_id")
    if not target_source_id:
        return "SKIPPED", "invalid_payload", False, {}
    target = repo.get_exchange_event(db, partner_code, str(target_source_id))
    if target is None or target.mapped_event_type != EventType.CHECKIN:
        # 签到尚未到达属于乱序:序号追平后仍缺失则挂争议等待裁决。
        return "DISPUTED", "target_checkin_missing", False, payload
    return (
        "ACCEPTED",
        None,
        False,
        {"checkin_event_id": target.local_event_id},
    )


# ---------------------------------------------------------------------------
# 查询 / 对账 / 裁决 / 来源解释
# ---------------------------------------------------------------------------

def _batch_dict(batch: Any) -> dict[str, Any]:
    return {
        "batch_id": batch.batch_id,
        "local_plan_version": batch.local_plan_version,
        "partner_institution_code": batch.partner_institution_code,
        "partner_plan_version": batch.partner_plan_version,
        "rule_code": batch.rule_code,
        "rule_version": batch.rule_version,
        "seq": batch.seq,
        "status": batch.status,
        "error_code": batch.error_code,
        "entries_count": batch.entries_count,
        "accepted_count": batch.accepted_count,
        "skipped_count": batch.skipped_count,
        "disputed_count": batch.disputed_count,
        "suppressed_count": batch.suppressed_count,
        "received_at": batch.received_at.isoformat() if batch.received_at else None,
        "processed_at": batch.processed_at.isoformat()
        if batch.processed_at
        else None,
    }


def get_batch(db: Session, batch_id: str) -> dict[str, Any]:
    batch = repo.get_batch(db, batch_id)
    if batch is None:
        raise ExchangeError("BATCH_NOT_FOUND", "交换批次不存在", 404)
    return _batch_dict(batch)


def list_batches(db: Session, **filters: Any) -> list[dict[str, Any]]:
    return [_batch_dict(b) for b in repo.list_batches(db, **filters)]


def _mapping_dict(row: Any) -> dict[str, Any]:
    return {
        "local_event_id": row.local_event_id,
        "local_plan_version": row.local_plan_version,
        "batch_id": row.batch_id,
        "source_institution_code": row.source_institution_code,
        "source_event_id": row.source_event_id,
        "source_student_id": row.source_student_id,
        "source_event_type": row.source_event_type,
        "mapped_event_type": row.mapped_event_type,
        "status": row.status,
        "reason_code": row.reason_code,
        "suppress_reason": row.suppress_reason,
        "arbitration_reason": row.arbitration_reason,
        "arbitrated_by": row.arbitrated_by,
        "arbitrated_at": row.arbitrated_at.isoformat()
        if row.arbitrated_at
        else None,
    }


def list_exchange_events(db: Session, **filters: Any) -> list[dict[str, Any]]:
    return [_mapping_dict(r) for r in repo.list_exchange_events(db, **filters)]


def reconcile(db: Session, local_plan_version: str) -> dict[str, Any]:
    """对账:序号缺口、挂起批次、争议与入账计数。"""
    if get_plan(db, local_plan_version) is None:
        raise ExchangeError("PLAN_NOT_FOUND", "本校培养方案不存在", 404)

    batches = repo.list_batches(db, local_plan_version=local_plan_version)
    channels: dict[str, dict[str, Any]] = {}
    for batch in batches:
        ch = channels.setdefault(
            batch.partner_institution_code,
            {
                "partner_institution_code": batch.partner_institution_code,
                "received_seqs": [],
                "ingested_seqs": [],
                "waiting_batches": [],
            },
        )
        ch["received_seqs"].append(batch.seq)
        if batch.status == "INGESTED":
            ch["ingested_seqs"].append(batch.seq)
        else:
            ch["waiting_batches"].append(batch.batch_id)

    channel_reports: list[dict[str, Any]] = []
    totals = {
        "batches_received": 0,
        "batches_ingested": 0,
        "entries_accepted": 0,
        "entries_skipped": 0,
        "entries_disputed": 0,
        "entries_suppressed": 0,
    }
    for batch in batches:
        totals["batches_received"] += 1
        if batch.status == "INGESTED":
            totals["batches_ingested"] += 1
        totals["entries_accepted"] += batch.accepted_count
        totals["entries_skipped"] += batch.skipped_count
        totals["entries_disputed"] += batch.disputed_count
        totals["entries_suppressed"] += batch.suppressed_count

    for partner_code in sorted(channels):
        ch = channels[partner_code]
        received = sorted(ch["received_seqs"])
        ingested = sorted(ch["ingested_seqs"])
        max_received = received[-1] if received else 0
        contiguous = 0
        for seq in range(1, max_received + 1):
            if seq in ingested:
                contiguous = seq
            else:
                break
        missing = [
            seq
            for seq in range(1, max_received + 1)
            if seq not in set(received)
        ]
        channel_reports.append(
            {
                "partner_institution_code": partner_code,
                "next_expected_seq": contiguous + 1,
                "max_received_seq": max_received,
                "missing_seqs": missing,
                "waiting_batches": ch["waiting_batches"],
                "caught_up": not missing and not ch["waiting_batches"],
            }
        )

    open_disputes = [
        _mapping_dict(row)
        for row in repo.list_exchange_events(
            db, local_plan_version=local_plan_version, status="DISPUTED"
        )
    ]
    return {
        "local_plan_version": local_plan_version,
        "totals": totals,
        "channels": channel_reports,
        "open_disputes": open_disputes,
        "open_dispute_count": len(open_disputes),
        "caught_up": all(c["caught_up"] for c in channel_reports),
    }


def arbitrate(
    db: Session,
    *,
    source_institution_code: str,
    source_event_id: str,
    verdict: str,
    reason: str,
    actor: str,
    suppress: bool = False,
) -> dict[str, Any]:
    if verdict not in ("UPHELD", "REJECTED"):
        raise ExchangeError(
            "INVALID_VERDICT", "裁决结果只能是 UPHELD 或 REJECTED", 400
        )
    if not reason or not actor:
        raise ExchangeError("ARBITRATION_REASON_REQUIRED", "裁决必须记录理由与裁决人", 400)
    mapping = repo.get_exchange_event(
        db, source_institution_code, source_event_id
    )
    if mapping is None:
        raise ExchangeError("DISPUTE_NOT_FOUND", "来源事件未登记", 404)
    if mapping.status != "DISPUTED":
        raise ExchangeError(
            "NOT_IN_DISPUTE", f"事件当前状态为 {mapping.status},无需裁决"
        )
    if verdict == "REJECTED":
        suppress = False
    repo.set_arbitration(
        db,
        mapping,
        verdict=verdict,
        reason=reason,
        actor=actor,
        suppressed=suppress,
    )
    if verdict == "UPHELD" and mapping.mapped_event_type == EventType.MENTOR_CONFIRM:
        # 裁决维持的确认事件需要指向本校事件流中的签到 id 才能生效。
        target_source_id = mapping.payload_snapshot.get("checkin_event_id")
        target = (
            repo.get_exchange_event(db, source_institution_code, str(target_source_id))
            if target_source_id is not None
            else None
        )
        event = repo.get_local_event(db, mapping.local_event_id)
        if event is not None and target is not None:
            event.payload = {"checkin_event_id": target.local_event_id}
            db.commit()
    row = repo.get_exchange_event(db, source_institution_code, source_event_id)
    return _mapping_dict(row)


def explain_source(
    db: Session, *, source_institution_code: str, source_event_id: str
) -> dict[str, Any]:
    """解释一笔外校事件从来源到本校计时的完整链路。"""
    partner = repo.get_institution(db, source_institution_code)
    mapping = repo.get_exchange_event(
        db, source_institution_code, source_event_id
    )
    if mapping is None:
        raise ExchangeError("SOURCE_EVENT_NOT_FOUND", "来源事件未登记", 404)
    batch = repo.get_batch(db, mapping.batch_id)
    rule = repo.get_rule(db, batch.rule_code, batch.rule_version)
    return {
        "source": {
            "institution_code": source_institution_code,
            "institution_name": partner.display_name if partner else None,
            "institution_timezone": partner.iana_timezone if partner else None,
            "event_id": mapping.source_event_id,
            "student_id": mapping.source_student_id,
            "event_type": mapping.source_event_type,
            "payload": mapping.payload_snapshot,
        },
        "mapping": _mapping_dict(mapping),
        "batch": {
            "batch_id": batch.batch_id,
            "seq": batch.seq,
            "status": batch.status,
            "received_at": batch.received_at.isoformat()
            if batch.received_at
            else None,
        },
        "rule": _rule_dict(rule) if rule is not None else None,
        "counts_toward_plan": mapping.status == "UPHELD"
        or (mapping.status == "ACCEPTED" and not mapping.suppress_reason),
        "pending_until_arbitration": mapping.status == "DISPUTED",
    }
