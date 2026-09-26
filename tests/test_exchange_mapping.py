"""跨校映射核心：乱序、跨时区、重复来源与规则快照。"""

from __future__ import annotations

from app.exchange.mapping import RuleSnapshot, local_event_id, map_batch

RULE = RuleSnapshot(
    rule_id="R1",
    version=1,
    sender_id="PARTNER",
    receiver_id="LEAD",
    plan_version="P1",
    activity_map={"regular": "regular", "internship": "internship"},
    cap_seconds=None,
)


def _checkin(eid, seq, student, start, end, *, activity_id="A1", atype="regular", tz=""):
    return {
        "event_id": eid,
        "sender_seq": seq,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": activity_id,
            "activity_type": atype,
            "check_in_at": start,
            "check_out_at": end,
            "timezone_hint": tz,
        },
    }


def test_out_of_order_batch_converges_by_sender_seq():
    # 送达顺序与 sender_seq 相反；映射结果必须按 seq 稳定。
    events = [
        _checkin(
            "X-03", 3, "S1",
            "2024-03-15T11:00:00+01:00", "2024-03-15T12:00:00+01:00",
            activity_id="A3",
        ),
        _checkin(
            "X-01", 1, "S1",
            "2024-03-15T09:00:00+01:00", "2024-03-15T10:00:00+01:00",
            activity_id="A1",
        ),
        _checkin(
            "X-02", 2, "S1",
            "2024-03-15T10:00:00+01:00", "2024-03-15T11:00:00+01:00",
            activity_id="A2",
        ),
    ]
    result_a = map_batch(events, RULE)
    result_b = map_batch(list(reversed(events)), RULE)

    ids_a = [m.local_event_id for m in result_a.accepted]
    ids_b = [m.local_event_id for m in result_b.accepted]
    assert ids_a == ids_b
    assert ids_a == [
        local_event_id("PARTNER", 1, "X-01"),
        local_event_id("PARTNER", 2, "X-02"),
        local_event_id("PARTNER", 3, "X-03"),
    ]


def test_cross_timezone_timestamps_normalized_to_utc():
    # 柏林 09:00-11:00 (+01:00) = 08:00-10:00 UTC。
    events = [
        _checkin(
            "X-01", 1, "S1",
            "2024-03-15T09:00:00+01:00", "2024-03-15T11:00:00+01:00",
            tz="Europe/Berlin",
        )
    ]
    result = map_batch(events, RULE)
    payload = result.accepted[0].local_event["payload"]
    assert payload["check_in_at"] == "2024-03-15T08:00:00Z"
    assert payload["check_out_at"] == "2024-03-15T10:00:00Z"
    assert payload["source"]["sender_timezone_hint"] == "Europe/Berlin"


def test_same_activity_id_from_two_schools_counts_once():
    # 两校上报同一 activity_id（联合活动双签到）：第二条整判重。
    existing = [
        {
            "student_id": "S1",
            "event_id": "L-1",
            "local_event_id": "L-1",
            "payload": {
                "activity_id": "JOINT-ACT",
                "activity_type": "regular",
                "check_in_at": "2024-03-15T01:00:00Z",
                "check_out_at": "2024-03-15T03:00:00Z",
            },
        }
    ]
    events = [
        _checkin(
            "X-01", 1, "S1",
            "2024-03-15T09:30:00+08:00", "2024-03-15T11:30:00+08:00",
            activity_id="JOINT-ACT",
        )
    ]
    result = map_batch(events, RULE, already_registered=existing)
    assert len(result.duplicates) == 1
    assert result.accepted == []
    assert "L-1" in result.duplicates[0].reason


def test_overlapping_intervals_clip_without_double_counting():
    # 无共同 activity_id 但时间区间重叠：只保留未覆盖的尾部。
    existing = [
        {
            "student_id": "S1",
            "local_event_id": "L-1",
            "payload": {
                "activity_id": "LOCAL-A",
                "activity_type": "regular",
                "check_in_at": "2024-03-15T01:00:00Z",
                "check_out_at": "2024-03-15T03:00:00Z",
            },
        }
    ]
    # 柏林 03:00-05:30 (+01:00) = 02:00-04:30 UTC；与本地重叠 02:00-03:00，
    # 剩余 03:00-04:30（5400 秒）。
    events = [
        _checkin(
            "X-01", 1, "S1",
            "2024-03-15T03:00:00+01:00", "2024-03-15T05:30:00+01:00",
            activity_id="PARTNER-B",
        )
    ]
    result = map_batch(events, RULE, already_registered=existing)
    accepted = result.accepted[0]
    payload = accepted.local_event["payload"]
    assert payload["check_in_at"] == "2024-03-15T03:00:00Z"
    assert payload["check_out_at"] == "2024-03-15T04:30:00Z"
    assert payload["source"]["overlap_trimmed_seconds"] == 3600


def test_fully_overlapping_interval_is_duplicate():
    existing = [
        {
            "student_id": "S1",
            "local_event_id": "L-1",
            "payload": {
                "activity_id": "LOCAL-A",
                "check_in_at": "2024-03-15T00:00:00Z",
                "check_out_at": "2024-03-15T06:00:00Z",
            },
        }
    ]
    events = [
        _checkin(
            "X-01", 1, "S1",
            "2024-03-15T09:00:00+08:00", "2024-03-15T11:00:00+08:00",
            activity_id="PARTNER-B",
        )
    ]
    result = map_batch(events, RULE, already_registered=existing)
    assert result.duplicates[0].reason == "签到区间与已登记活动完全重叠"
    assert result.accepted == []


def test_other_student_interval_does_not_block():
    existing = [
        {
            "student_id": "S2",
            "local_event_id": "L-1",
            "payload": {
                "activity_id": "JOINT",
                "check_in_at": "2024-03-15T01:00:00Z",
                "check_out_at": "2024-03-15T03:00:00Z",
            },
        }
    ]
    events = [
        _checkin(
            "X-01", 1, "S1",
            "2024-03-15T09:00:00+08:00", "2024-03-15T11:00:00+08:00",
            activity_id="JOINT",
        )
    ]
    result = map_batch(events, RULE, already_registered=existing)
    assert len(result.accepted) == 1


def test_cap_seconds_trims_long_external_activity():
    rule = RuleSnapshot(
        rule_id="R1", version=1, sender_id="PARTNER", receiver_id="LEAD",
        plan_version="P1", activity_map={"regular": "regular"}, cap_seconds=3600,
    )
    events = [
        _checkin(
            "X-01", 1, "S1",
            "2024-03-15T09:00:00+01:00", "2024-03-15T12:00:00+01:00",
            activity_id="A1",
        )
    ]
    result = map_batch(events, rule)
    payload = result.accepted[0].local_event["payload"]
    assert payload["check_out_at"] == "2024-03-15T09:00:00Z"  # 08:00 + 1h
    assert payload["source"]["cap_trimmed_seconds"] == 2 * 3600


def test_unmapped_activity_type_is_quarantined():
    events = [
        _checkin(
            "X-01", 1, "S1",
            "2024-03-15T09:00:00+01:00", "2024-03-15T10:00:00+01:00",
            atype="workshop",
        )
    ]
    result = map_batch(events, RULE)
    q = result.quarantined[0]
    assert q.external_event_id == "X-01"
    assert q.local_event_id is None
    assert "未覆盖活动类型" in q.reason


def test_non_checkin_event_is_quarantined():
    events = [
        {
            "event_id": "X-09",
            "sender_seq": 9,
            "event_type": "mentor_confirm",
            "student_id": "S1",
            "payload": {"checkin_event_id": "X-01"},
        }
    ]
    result = map_batch(events, RULE)
    assert result.quarantined[0].status == "quarantined"


def test_naive_timestamp_is_quarantined():
    events = [
        _checkin(
            "X-01", 1, "S1",
            "2024-03-15T09:00:00", "2024-03-15T10:00:00",
        )
    ]
    result = map_batch(events, RULE)
    assert len(result.quarantined) == 1
    assert "时区" in result.quarantined[0].reason


def test_batch_internal_duplicate_activity_takes_one():
    events = [
        _checkin(
            "X-01", 1, "S1",
            "2024-03-15T09:00:00+01:00", "2024-03-15T10:00:00+01:00",
            activity_id="DUP",
        ),
        _checkin(
            "X-02", 2, "S1",
            "2024-03-15T09:30:00+01:00", "2024-03-15T10:30:00+01:00",
            activity_id="DUP",
        ),
    ]
    result = map_batch(events, RULE)
    assert len(result.accepted) == 1
    assert len(result.duplicates) == 1


def test_mapped_payload_keeps_rule_version_for_provenance():
    events = [
        _checkin(
            "X-01", 1, "S1",
            "2024-03-15T09:00:00+01:00", "2024-03-15T10:00:00+01:00",
            atype="internship",
        )
    ]
    result = map_batch(events, RULE)
    source = result.accepted[0].local_event["payload"]["source"]
    assert source["rule_id"] == "R1"
    assert source["rule_version"] == 1
    assert source["external_event_id"] == "X-01"
    assert result.accepted[0].local_event["payload"]["activity_type"] == "internship"
