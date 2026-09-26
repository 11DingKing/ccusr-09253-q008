"""跨校事件在回放层的去重、争议与裁决语义。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.replay import (
    Event,
    EventType,
    replay,
)

PLAN = "P1"


def _checkin(
    eid: str,
    student: str,
    start: str,
    end: str,
    *,
    activity_id: str = "A1",
    activity_type: str = "regular",
    source: str = "local",
    exchange_status: str | None = None,
    suppressed: bool = False,
) -> Event:
    return Event(
        event_id=eid,
        plan_version=PLAN,
        event_type=EventType.CHECKIN,
        student_id=student,
        payload={
            "activity_id": activity_id,
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
        created_at=datetime.now(timezone.utc),
        source=source,
        exchange_status=exchange_status,
        suppressed=suppressed,
    )


def test_foreign_event_id_sorting_first_does_not_suppress_local():
    # "EXG:..." 的字典序排在 "L-1" 之前,但本校记录必须优先保留。
    events = [
        _checkin(
            "EXG:NYU:E1",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            activity_id="A-JOINT",
            source="exchange",
            exchange_status="ACCEPTED",
        ),
        _checkin(
            "L-1",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            activity_id="A-JOINT",
        ),
    ]
    state = replay(
        events, plan_version=PLAN, timezone_name="Asia/Shanghai", required_seconds=0
    )
    progress = state.students["S1"]
    assert progress.total_seconds == 7200
    by_id = {c.event_id: c for c in progress.checkins}
    assert by_id["L-1"].duplicate_of is None
    assert by_id["EXG:NYU:E1"].duplicate_of == "L-1"


def test_persisted_suppression_flag_is_honoured():
    events = [
        _checkin("L-1", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin(
            "E2",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            source="exchange",
            exchange_status="ACCEPTED",
            suppressed=True,
        ),
    ]
    state = replay(
        events, plan_version=PLAN, timezone_name="Asia/Shanghai", required_seconds=0
    )
    progress = state.students["S1"]
    assert progress.total_seconds == 7200


def test_disputed_checkin_is_pending_then_counts_after_upheld_replay():
    events = [
        _checkin(
            "E1",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            source="exchange",
            exchange_status="DISPUTED",
        ),
    ]
    state = replay(
        events, plan_version=PLAN, timezone_name="Asia/Shanghai", required_seconds=0
    )
    progress = state.students["S1"]
    assert progress.pending_seconds == 7200
    assert progress.confirmed_seconds == 0
    assert progress.total_seconds == 0

    # 裁决后状态更新为 UPHELD,重新回放即确认。
    events[0] = Event(
        event_id="E1",
        plan_version=PLAN,
        event_type=EventType.CHECKIN,
        student_id="S1",
        payload=events[0].payload,
        created_at=events[0].created_at,
        source="exchange",
        exchange_status="UPHELD",
    )
    state = replay(
        events, plan_version=PLAN, timezone_name="Asia/Shanghai", required_seconds=0
    )
    progress = state.students["S1"]
    assert progress.pending_seconds == 0
    assert progress.confirmed_seconds == 7200


def test_rejected_exchange_event_does_not_replay():
    events = [
        _checkin(
            "E1",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            source="exchange",
            exchange_status="REJECTED",
        ),
        _checkin("E2", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
    ]
    state = replay(
        events, plan_version=PLAN, timezone_name="Asia/Shanghai", required_seconds=0
    )
    assert "S1" not in state.students
    assert state.students["S2"].total_seconds == 3600


def test_partial_overlap_upheld_unions_without_double_counting():
    # 本校 08:00-10:00,外校 09:00-11:00,裁决维持 -> 并集 3 小时。
    events = [
        _checkin(
            "E1",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            activity_id="A1",
        ),
        _checkin(
            "E2",
            "S1",
            "2024-03-15T09:00:00+08:00",
            "2024-03-15T11:00:00+08:00",
            activity_id="A1",
            source="exchange",
            exchange_status="UPHELD",
        ),
    ]
    state = replay(
        events, plan_version=PLAN, timezone_name="Asia/Shanghai", required_seconds=0
    )
    assert state.students["S1"].total_seconds == 3 * 3600


def test_disputed_leave_correction_is_excluded_until_upheld():
    correction = Event(
        event_id="E2",
        plan_version=PLAN,
        event_type=EventType.LEAVE_CORRECTION,
        student_id="S1",
        payload={"adjustment_seconds": -1800, "reason": "absence"},
        created_at=datetime.now(timezone.utc),
        source="exchange",
        exchange_status="DISPUTED",
    )
    base = [
        _checkin("E1", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        correction,
    ]
    state = replay(
        base, plan_version=PLAN, timezone_name="Asia/Shanghai", required_seconds=0
    )
    assert state.students["S1"].total_seconds == 7200

    correction = Event(
        event_id="E2",
        plan_version=PLAN,
        event_type=EventType.LEAVE_CORRECTION,
        student_id="S1",
        payload=correction.payload,
        created_at=correction.created_at,
        source="exchange",
        exchange_status="UPHELD",
    )
    state = replay(
        [base[0], correction],
        plan_version=PLAN,
        timezone_name="Asia/Shanghai",
        required_seconds=0,
    )
    assert state.students["S1"].total_seconds == 7200 - 1800
