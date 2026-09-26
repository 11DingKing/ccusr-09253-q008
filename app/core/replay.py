"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable

from .clock import (
    elapsed_seconds,
    merge_intervals,
    split_by_academic_day,
    to_utc,
    union_seconds,
)


class EventType(StrEnum):
    CHECKIN = "checkin"
    MENTOR_CONFIRM = "mentor_confirm"
    LEAVE_CORRECTION = "leave_correction"


class CheckinStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PENDING = "PENDING"


INTERNSHIP_TYPE = "internship"

# 外校事件的互认状态:ACCEPTED/UPHELD 正常入账;DISPUTED 在裁决前按待定处理;
# SKIPPED/REJECTED 不参与回放。
EXCHANGE_ACTIVE_STATUSES = frozenset({"ACCEPTED", "UPHELD"})
EXCHANGE_PENDING_STATUSES = frozenset({"DISPUTED"})


@dataclass(frozen=True)
class Event:
    """封装领域状态与业务约束。"""

    event_id: str
    plan_version: str
    event_type: EventType
    student_id: str
    payload: dict[str, Any]
    created_at: datetime
    source: str = "local"
    exchange_status: str | None = None
    suppressed: bool = False


@dataclass
class CheckinRecord:
    event_id: str
    student_id: str
    activity_id: str
    activity_type: str
    start_utc: datetime
    end_utc: datetime
    status: CheckinStatus
    source: str = "local"
    exchange_status: str | None = None
    disputed: bool = False
    # 指向同一活动同一时段的先行记录(event_id 更小者),本笔不重复计时。
    duplicate_of: str | None = None

    @property
    def seconds(self) -> int:
        return elapsed_seconds(self.start_utc, self.end_utc)

    @property
    def counts(self) -> bool:
        return (
            self.status == CheckinStatus.CONFIRMED
            and not self.disputed
            and self.duplicate_of is None
        )

    @property
    def is_pending(self) -> bool:
        return (self.status == CheckinStatus.PENDING or self.disputed) and (
            self.duplicate_of is None
        )


@dataclass
class Adjustment:
    event_id: str
    student_id: str
    seconds: int
    reason: str


@dataclass
class DayTotal:
    academic_day: str
    seconds: int


@dataclass
class StudentProgress:
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DayTotal] = field(default_factory=list)
    checkins: list[CheckinRecord] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)


@dataclass
class ReplayState:
    plan_version: str
    timezone: str
    required_seconds: int
    students: dict[str, StudentProgress]


def _parse_checkin(
    event: Event, tz_name: str
) -> CheckinRecord:
    start = to_utc(datetime.fromisoformat(event.payload["check_in_at"]))
    end = to_utc(datetime.fromisoformat(event.payload["check_out_at"]))
    activity_type = event.payload.get("activity_type", "regular")
    requires_confirmation = activity_type == INTERNSHIP_TYPE
    status = (
        CheckinStatus.PENDING if requires_confirmation else CheckinStatus.CONFIRMED
    )
    disputed = (
        event.source == "exchange"
        and event.exchange_status in EXCHANGE_PENDING_STATUSES
    )
    return CheckinRecord(
        event_id=event.event_id,
        student_id=event.student_id,
        activity_id=event.payload.get("activity_id", ""),
        activity_type=activity_type,
        start_utc=start,
        end_utc=end,
        status=status,
        source=event.source,
        exchange_status=event.exchange_status,
        disputed=disputed,
        duplicate_of="suppressed" if event.suppressed else None,
    )


def _is_active_exchange(event: Event) -> bool:
    if event.source != "exchange":
        return True
    return event.exchange_status in EXCHANGE_ACTIVE_STATUSES or (
        event.exchange_status in EXCHANGE_PENDING_STATUSES
    )


def _suppression_precedence(record: CheckinRecord) -> tuple[int, str]:
    """同一活动重复时的归属优先级:本校记录优先,其次已互认外校记录,
    再次争议待定记录;同档内按 event_id 保持确定性。"""
    if record.source == "local":
        rank = 0
    elif record.disputed:
        rank = 2
    else:
        rank = 1
    return rank, record.event_id


def mark_same_activity_duplicates(
    records: list[CheckinRecord],
) -> list[CheckinRecord]:
    """同一学生同一活动,被更高优先级记录完全覆盖的签到不重复计时。

    跨校联合活动在两校各自登记时,优先级较低的整段重复区间标记为
    duplicate_of;部分重叠不在此静默处理,而在入账阶段进入争议。
    """
    earlier: dict[tuple[str, str], list[CheckinRecord]] = {}
    for record in sorted(records, key=_suppression_precedence):
        peers = earlier.setdefault((record.student_id, record.activity_id), [])
        if record.duplicate_of is None:
            for prev in peers:
                if prev.start_utc <= record.start_utc and record.end_utc <= prev.end_utc:
                    record.duplicate_of = prev.event_id
                    break
        peers.append(record)
    return records


def replay(
    events: Iterable[Event],
    *,
    plan_version: str,
    timezone_name: str,
    required_seconds: int,
    up_to_event_id: str | None = None,
) -> ReplayState:
    """执行确定性的业务处理。"""
    sorted_events = sorted(
        (
            e
            for e in events
            if e.plan_version == plan_version and _is_active_exchange(e)
        ),
        key=lambda e: e.event_id,
    )
    if up_to_event_id is not None:
        sorted_events = [e for e in sorted_events if e.event_id <= up_to_event_id]

    checkins_by_student: dict[str, list[CheckinRecord]] = {}
    checkin_index: dict[str, CheckinRecord] = {}
    pending_confirms: list[Event] = []
    adjustments_by_student: dict[str, list[Adjustment]] = {}

    for event in sorted_events:
        if event.event_type == EventType.CHECKIN:
            record = _parse_checkin(event, timezone_name)
            checkins_by_student.setdefault(event.student_id, []).append(record)
            checkin_index[event.event_id] = record
        elif event.event_type == EventType.MENTOR_CONFIRM:
            # 第二遍统一应用,使确认与签到的 event_id 相对顺序无关(跨校乱序)。
            if not (
                event.source == "exchange"
                and event.exchange_status in EXCHANGE_PENDING_STATUSES
            ):
                pending_confirms.append(event)
        elif event.event_type == EventType.LEAVE_CORRECTION:
            # 争议中的外校修正裁决前不计入(也不抵减)学时。
            if (
                event.source == "exchange"
                and event.exchange_status in EXCHANGE_PENDING_STATUSES
            ):
                continue
            seconds = int(event.payload.get("adjustment_seconds", 0))
            adjustments_by_student.setdefault(event.student_id, []).append(
                Adjustment(
                    event_id=event.event_id,
                    student_id=event.student_id,
                    seconds=seconds,
                    reason=str(event.payload.get("reason", "")),
                )
            )

    for event in pending_confirms:
        target_id = event.payload.get("checkin_event_id")
        target = checkin_index.get(target_id)
        if target is not None and target.student_id == event.student_id:
            target.status = CheckinStatus.CONFIRMED

    all_students = set(checkins_by_student) | set(adjustments_by_student)
    students: dict[str, StudentProgress] = {}
    for student_id in all_students:
        records = mark_same_activity_duplicates(
            checkins_by_student.get(student_id, [])
        )
        adjustments = adjustments_by_student.get(student_id, [])

        confirmed_intervals = [
            (r.start_utc, r.end_utc) for r in records if r.counts
        ]
        pending_intervals = [
            (r.start_utc, r.end_utc) for r in records if r.is_pending
        ]

        confirmed_seconds = union_seconds(confirmed_intervals)
        pending_seconds = union_seconds(pending_intervals)
        adjustment_seconds = sum(a.seconds for a in adjustments)
        total_seconds = confirmed_seconds + adjustment_seconds
        if total_seconds < 0:
            total_seconds = 0

        day_totals: dict[str, int] = {}
        for start, end in merge_intervals(confirmed_intervals):
            for day, seg_start, seg_end in split_by_academic_day(
                start, end, timezone_name
            ):
                key = day.isoformat()
                day_totals[key] = day_totals.get(key, 0) + elapsed_seconds(
                    seg_start, seg_end
                )
        daily = [
            DayTotal(academic_day=day, seconds=secs)
            for day, secs in sorted(day_totals.items())
        ]

        students[student_id] = StudentProgress(
            student_id=student_id,
            confirmed_seconds=confirmed_seconds,
            pending_seconds=pending_seconds,
            adjustment_seconds=adjustment_seconds,
            total_seconds=total_seconds,
            lesson_units=total_seconds // (45 * 60),
            pending_lesson_units=pending_seconds // (45 * 60),
            meets_requirement=total_seconds >= required_seconds,
            daily=daily,
            checkins=sorted(records, key=lambda r: r.start_utc),
            adjustments=sorted(adjustments, key=lambda a: a.event_id),
        )

    return ReplayState(
        plan_version=plan_version,
        timezone=timezone_name,
        required_seconds=required_seconds,
        students=students,
    )


def explain_checkin(record: CheckinRecord, tz_name: str) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    segments = split_by_academic_day(record.start_utc, record.end_utc, tz_name)
    return {
        "event_id": record.event_id,
        "activity_id": record.activity_id,
        "activity_type": record.activity_type,
        "status": record.status.value,
        "counts": record.counts,
        "source": record.source,
        "exchange_status": record.exchange_status,
        "disputed": record.disputed,
        "duplicate_of": record.duplicate_of,
        "check_in_at_utc": record.start_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "check_out_at_utc": record.end_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "raw_seconds": record.seconds,
        "academic_days": [
            {
                "day": day.isoformat(),
                "start_utc": seg_start.isoformat().replace("+00:00", "Z"),
                "end_utc": seg_end.isoformat().replace("+00:00", "Z"),
                "seconds": elapsed_seconds(seg_start, seg_end),
            }
            for day, seg_start, seg_end in segments
        ],
    }
