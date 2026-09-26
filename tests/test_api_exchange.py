"""跨校交换批次:接收、校验、对账、裁决与来源解释。"""

from __future__ import annotations

from tests.conftest import SHANGHAI_PLAN
from tests.exchange_helpers import (
    checkin_entry,
    make_envelope,
    post_batch,
    setup_exchange,
)


def _progress(client, student_id: str, plan: str = SHANGHAI_PLAN["plan_version"]):
    resp = client.get(f"/api/plans/{plan}/students/{student_id}/progress")
    if resp.status_code == 404:
        # 尚无该学生的任何事件。
        return {
            "total_seconds": 0,
            "confirmed_seconds": 0,
            "pending_seconds": 0,
            "daily": [],
            "checkins": [],
        }
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_cross_timezone_checkin_maps_to_local_academic_day(client):
    setup_exchange(client, plan=SHANGHAI_PLAN)
    # 纽约 2024-03-14 20:00-22:00 (UTC-4) = 上海 2024-03-15 08:00-10:00。
    envelope = make_envelope(
        batch_id="B-1",
        seq=1,
        entries=[
            checkin_entry(
                "NY-E1",
                "S1",
                "2024-03-14T20:00:00-04:00",
                "2024-03-14T22:00:00-04:00",
            )
        ],
    )
    resp = post_batch(client, envelope)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "INGESTED"
    assert body["accepted_count"] == 1

    progress = _progress(client, "S1")
    assert progress["total_seconds"] == 7200
    # 学时按本校方案时区归入上海 3 月 15 日,而不是纽约 3 月 14 日。
    assert progress["daily"] == [{"academic_day": "2024-03-15", "seconds": 7200}]
    checkin = progress["checkins"][0]
    assert checkin["source"] == "exchange"
    assert checkin["exchange_status"] == "ACCEPTED"
    assert checkin["check_in_at_utc"] == "2024-03-15T00:00:00Z"


def test_out_of_order_batches_wait_for_gap_then_ingest_in_sequence(client):
    setup_exchange(client, plan=SHANGHAI_PLAN)
    # 先发 seq=2,再发 seq=1;seq=2 必须挂起直到缺口补齐。
    resp2 = post_batch(
        client,
        make_envelope(
            batch_id="B-2",
            seq=2,
            entries=[
                checkin_entry(
                    "NY-E2",
                    "S1",
                    "2024-03-15T09:00:00-04:00",
                    "2024-03-15T10:00:00-04:00",
                )
            ],
        ),
    )
    assert resp2.status_code == 202, resp2.text
    assert resp2.json()["status"] == "RECEIVED"
    assert _progress(client, "S1")["total_seconds"] == 0

    resp1 = post_batch(
        client,
        make_envelope(
            batch_id="B-1",
            seq=1,
            entries=[
                checkin_entry(
                    "NY-E1",
                    "S1",
                    "2024-03-15T08:00:00-04:00",
                    "2024-03-15T09:00:00-04:00",
                )
            ],
        ),
    )
    assert resp1.status_code == 202, resp1.text
    assert resp1.json()["status"] == "INGESTED"

    # 缺口补齐后 seq=2 被自动追平入账。
    batch2 = client.get("/api/exchange/batches/B-2").json()
    assert batch2["status"] == "INGESTED"
    assert batch2["accepted_count"] == 1
    assert _progress(client, "S1")["total_seconds"] == 7200

    reconcile = client.get(
        f"/api/exchange/plans/{SHANGHAI_PLAN['plan_version']}/reconcile"
    ).json()
    assert reconcile["caught_up"] is True
    assert reconcile["channels"][0]["missing_seqs"] == []
    assert reconcile["channels"][0]["next_expected_seq"] == 3


def test_gap_is_reported_in_reconciliation(client):
    setup_exchange(client, plan=SHANGHAI_PLAN)
    post_batch(
        client,
        make_envelope(
            batch_id="B-2",
            seq=2,
            entries=[
                checkin_entry(
                    "NY-E2",
                    "S1",
                    "2024-03-15T08:00:00-04:00",
                    "2024-03-15T09:00:00-04:00",
                )
            ],
        ),
    )
    reconcile = client.get(
        f"/api/exchange/plans/{SHANGHAI_PLAN['plan_version']}/reconcile"
    ).json()
    assert reconcile["caught_up"] is False
    channel = reconcile["channels"][0]
    assert channel["missing_seqs"] == [1]
    assert channel["waiting_batches"] == ["B-2"]


def test_duplicate_source_event_is_skipped_not_double_counted(client):
    setup_exchange(client, plan=SHANGHAI_PLAN)
    entry = checkin_entry(
        "NY-E1",
        "S1",
        "2024-03-15T08:00:00-04:00",
        "2024-03-15T10:00:00-04:00",
    )
    resp1 = post_batch(client, make_envelope(batch_id="B-1", seq=1, entries=[entry]))
    assert resp1.json()["accepted_count"] == 1

    # 同一来源事件出现在后续批次(重复来源),不得重复计时。
    resp2 = post_batch(client, make_envelope(batch_id="B-2", seq=2, entries=[entry]))
    assert resp2.json()["skipped_count"] == 1

    progress = _progress(client, "S1")
    assert progress["total_seconds"] == 7200
    assert len(progress["checkins"]) == 1

    # 重复来源不再产生第二张台账,原始映射保持 ACCEPTED。
    events = client.get(
        "/api/exchange/events", params={"local_plan_version": SHANGHAI_PLAN["plan_version"]}
    ).json()
    assert len(events) == 1
    assert events[0]["status"] == "ACCEPTED"
    assert events[0]["source_event_id"] == "NY-E1"


def test_same_activity_same_window_is_suppressed(client):
    setup_exchange(client, plan=SHANGHAI_PLAN)
    # 本校已登记联合活动 A-JOINT 的签到。
    client.post(
        f"/api/plans/{SHANGHAI_PLAN['plan_version']}/events",
        json={
            "events": [
                {
                    "event_id": "L-1",
                    "event_type": "checkin",
                    "student_id": "S1",
                    "payload": {
                        "activity_id": "A-JOINT",
                        "activity_type": "regular",
                        "check_in_at": "2024-03-15T08:00:00+08:00",
                        "check_out_at": "2024-03-15T10:00:00+08:00",
                    },
                }
            ]
        },
    )
    # 合作院校就同一活动同一时段(以纽约时区表达)再次上报。
    resp = post_batch(
        client,
        make_envelope(
            batch_id="B-1",
            seq=1,
            entries=[
                checkin_entry(
                    "NY-E1",
                    "S1",
                    "2024-03-14T20:00:00-04:00",
                    "2024-03-14T22:00:00-04:00",
                    activity_id="A-JOINT",
                )
            ],
        ),
    )
    body = resp.json()
    assert body["suppressed_count"] == 1

    progress = _progress(client, "S1")
    # 同一活动不得在两校重复计时。
    assert progress["total_seconds"] == 7200
    foreign = [c for c in progress["checkins"] if c["source"] == "exchange"][0]
    assert foreign["counts"] is False
    assert foreign["duplicate_of"] == "suppressed"

    explanation = client.get(
        "/api/exchange/sources/NYU/events/NY-E1"
    ).json()
    assert explanation["mapping"]["suppress_reason"] == "same_activity_overlap"
    assert explanation["counts_toward_plan"] is False


def test_partial_overlap_becomes_dispute_until_arbitration(client):
    setup_exchange(client, plan=SHANGHAI_PLAN)
    client.post(
        f"/api/plans/{SHANGHAI_PLAN['plan_version']}/events",
        json={
            "events": [
                {
                    "event_id": "L-1",
                    "event_type": "checkin",
                    "student_id": "S1",
                    "payload": {
                        "activity_id": "A1",
                        "activity_type": "regular",
                        "check_in_at": "2024-03-15T08:00:00+08:00",
                        "check_out_at": "2024-03-15T10:00:00+08:00",
                    },
                }
            ]
        },
    )
    # 外校上报 09:00-11:00(上海时区),与本校记录部分重叠。
    resp = post_batch(
        client,
        make_envelope(
            batch_id="B-1",
            seq=1,
            entries=[
                checkin_entry(
                    "NY-E1",
                    "S1",
                    "2024-03-15T09:00:00+08:00",
                    "2024-03-15T11:00:00+08:00",
                    activity_id="A1",
                )
            ],
        ),
    )
    assert resp.json()["disputed_count"] == 1

    progress = _progress(client, "S1")
    # 争议记录在裁决前保持待定:本校 2 小时确认,外校 2 小时待定。
    assert progress["confirmed_seconds"] == 7200
    assert progress["pending_seconds"] == 7200
    assert progress["total_seconds"] == 7200

    reconcile = client.get(
        f"/api/exchange/plans/{SHANGHAI_PLAN['plan_version']}/reconcile"
    ).json()
    assert reconcile["open_dispute_count"] == 1

    # 裁决维持(不抑制):重叠区间由并集去重,净增 1 小时。
    resp = client.post(
        "/api/exchange/arbitrations",
        json={
            "source_institution_code": "NYU",
            "source_event_id": "NY-E1",
            "verdict": "UPHELD",
            "reason": "两校均确认联合实训,重叠部分只计一次",
            "actor": "registrar-01",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "UPHELD"
    progress = _progress(client, "S1")
    assert progress["total_seconds"] == 3 * 3600
    assert progress["pending_seconds"] == 0

    # 已裁决的争议不可重复裁决。
    resp = client.post(
        "/api/exchange/arbitrations",
        json={
            "source_institution_code": "NYU",
            "source_event_id": "NY-E1",
            "verdict": "REJECTED",
            "reason": "再次裁决",
            "actor": "registrar-01",
        },
    )
    assert resp.status_code == 422


def test_arbitration_rejection_drops_disputed_hours(client):
    setup_exchange(client, plan=SHANGHAI_PLAN)
    client.post(
        f"/api/plans/{SHANGHAI_PLAN['plan_version']}/events",
        json={
            "events": [
                {
                    "event_id": "L-1",
                    "event_type": "checkin",
                    "student_id": "S1",
                    "payload": {
                        "activity_id": "A1",
                        "activity_type": "regular",
                        "check_in_at": "2024-03-15T08:00:00+08:00",
                        "check_out_at": "2024-03-15T10:00:00+08:00",
                    },
                }
            ]
        },
    )
    post_batch(
        client,
        make_envelope(
            batch_id="B-1",
            seq=1,
            entries=[
                checkin_entry(
                    "NY-E1",
                    "S1",
                    "2024-03-15T09:00:00+08:00",
                    "2024-03-15T11:00:00+08:00",
                    activity_id="A1",
                )
            ],
        ),
    )
    assert _progress(client, "S1")["pending_seconds"] == 7200

    resp = client.post(
        "/api/exchange/arbitrations",
        json={
            "source_institution_code": "NYU",
            "source_event_id": "NY-E1",
            "verdict": "REJECTED",
            "reason": "外校记录与本校冲突且无法举证",
            "actor": "registrar-01",
        },
    )
    assert resp.status_code == 200
    progress = _progress(client, "S1")
    assert progress["total_seconds"] == 7200
    assert progress["pending_seconds"] == 0
    assert progress["checkins"][0]["source"] == "local"


def test_rule_upgrade_only_affects_new_batches(client):
    setup_exchange(client, plan=SHANGHAI_PLAN, accepted_activity_types=None)
    # v1 不限活动类型:workshop 正常互认。
    resp = post_batch(
        client,
        make_envelope(
            batch_id="B-1",
            seq=1,
            entries=[
                checkin_entry(
                    "NY-E1",
                    "S1",
                    "2024-03-15T08:00:00-04:00",
                    "2024-03-15T09:00:00-04:00",
                    activity_type="workshop",
                )
            ],
        ),
    )
    assert resp.json()["accepted_count"] == 1

    # 升级规则 v2:仅互认 regular/internship。
    resp = client.post(
        "/api/exchange/rules",
        json={
            "rule_code": "R-NY-SH",
            "local_plan_version": SHANGHAI_PLAN["plan_version"],
            "partner_institution_code": "NYU",
            "event_type_mapping": {"checkin": "checkin"},
            "accepted_activity_types": ["regular", "internship"],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["version"] == 2
    client.post("/api/exchange/rules/R-NY-SH/versions/2/publish")

    # 新批次绑定 v2:workshop 进入争议待定。
    resp = post_batch(
        client,
        make_envelope(
            batch_id="B-2",
            seq=2,
            rule_version=2,
            entries=[
                checkin_entry(
                    "NY-E2",
                    "S1",
                    "2024-03-16T08:00:00-04:00",
                    "2024-03-16T09:00:00-04:00",
                    activity_type="workshop",
                )
            ],
        ),
    )
    assert resp.json()["disputed_count"] == 1

    # 老批次仍按 v1 解释(来源解释可见绑定版本)。
    explanation = client.get("/api/exchange/sources/NYU/events/NY-E1").json()
    assert explanation["rule"]["version"] == 1
    assert explanation["counts_toward_plan"] is True
    explanation2 = client.get("/api/exchange/sources/NYU/events/NY-E2").json()
    assert explanation2["rule"]["version"] == 2
    assert explanation2["pending_until_arbitration"] is True

    # 重传老批次(重复投递)依旧幂等,不重复计时。
    resp = post_batch(
        client,
        make_envelope(
            batch_id="B-1",
            seq=1,
            entries=[
                checkin_entry(
                    "NY-E1",
                    "S1",
                    "2024-03-15T08:00:00-04:00",
                    "2024-03-15T09:00:00-04:00",
                    activity_type="workshop",
                )
            ],
        ),
    )
    assert resp.json()["duplicate_delivery"] is True
    assert _progress(client, "S1")["total_seconds"] == 3600


def test_signature_verification_rejects_tampered_batch(client):
    setup_exchange(client, plan=SHANGHAI_PLAN)
    envelope = make_envelope(
        batch_id="B-1",
        seq=1,
        entries=[
            checkin_entry(
                "NY-E1",
                "S1",
                "2024-03-15T08:00:00-04:00",
                "2024-03-15T09:00:00-04:00",
            )
        ],
        key="wrong-key",
    )
    resp = post_batch(client, envelope)
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "SIGNATURE_INVALID"

    # 篡改条目后沿用原签名同样失败。
    envelope = make_envelope(
        batch_id="B-1",
        seq=1,
        entries=[
            checkin_entry(
                "NY-E1",
                "S1",
                "2024-03-15T08:00:00-04:00",
                "2024-03-15T09:00:00-04:00",
            )
        ],
    )
    envelope["entries"][0]["payload"]["check_out_at"] = "2024-03-15T12:00:00-04:00"
    resp = post_batch(client, envelope)
    assert resp.status_code == 401
    assert _progress(client, "S1")["total_seconds"] == 0


def test_restart_recovery_replays_received_batches(client, db):
    setup_exchange(client, plan=SHANGHAI_PLAN)
    envelope = make_envelope(
        batch_id="B-1",
        seq=1,
        entries=[
            checkin_entry(
                "NY-E1",
                "S1",
                "2024-03-15T08:00:00-04:00",
                "2024-03-15T09:00:00-04:00",
            )
        ],
    )
    resp = post_batch(client, envelope)
    assert resp.json()["status"] == "INGESTED"
    assert _progress(client, "S1")["total_seconds"] == 3600

    # 模拟崩溃:批次已接收但入账标记丢失(事件流仍在)。
    from app.exchange import repository as exchange_repo

    exchange_repo.mark_batch_received(db, "B-1", "PROCESSING_RETRY")
    assert client.get("/api/exchange/batches/B-1").json()["status"] == "RECEIVED"

    # 重启恢复:重新驱动挂起批次;映射幂等,绝不重复计时。
    resp = client.post("/api/exchange/recover")
    assert resp.status_code == 200
    batch = client.get("/api/exchange/batches/B-1").json()
    assert batch["status"] == "INGESTED"
    assert batch["accepted_count"] == 1
    assert _progress(client, "S1")["total_seconds"] == 3600


def test_recovery_also_catches_up_out_of_order_channels(client, db):
    setup_exchange(client, plan=SHANGHAI_PLAN)
    # seq=2 挂起;随后进程重启,恢复时 seq=1 与 seq=2 都在 RECEIVED。
    from app.exchange import repository as exchange_repo

    post_batch(
        client,
        make_envelope(
            batch_id="B-2",
            seq=2,
            entries=[
                checkin_entry(
                    "NY-E2",
                    "S1",
                    "2024-03-15T09:00:00-04:00",
                    "2024-03-15T10:00:00-04:00",
                )
            ],
        ),
    )
    exchange_repo.mark_batch_received(db, "B-2", "PROCESSING_RETRY")
    post_batch(
        client,
        make_envelope(
            batch_id="B-1",
            seq=1,
            entries=[
                checkin_entry(
                    "NY-E1",
                    "S1",
                    "2024-03-15T08:00:00-04:00",
                    "2024-03-15T09:00:00-04:00",
                )
            ],
        ),
    )
    exchange_repo.mark_batch_received(db, "B-1", "PROCESSING_RETRY")
    exchange_repo.mark_batch_received(db, "B-2", "PROCESSING_RETRY")

    resp = client.post("/api/exchange/recover")
    assert resp.status_code == 200
    assert _progress(client, "S1")["total_seconds"] == 7200
    reconcile = client.get(
        f"/api/exchange/plans/{SHANGHAI_PLAN['plan_version']}/reconcile"
    ).json()
    assert reconcile["caught_up"] is True


def test_source_explanation_traces_full_chain(client):
    setup_exchange(client, plan=SHANGHAI_PLAN)
    post_batch(
        client,
        make_envelope(
            batch_id="B-1",
            seq=1,
            entries=[
                checkin_entry(
                    "NY-E1",
                    "S1",
                    "2024-03-15T08:00:00-04:00",
                    "2024-03-15T09:00:00-04:00",
                )
            ],
        ),
    )
    explanation = client.get("/api/exchange/sources/NYU/events/NY-E1").json()
    assert explanation["source"]["institution_code"] == "NYU"
    assert explanation["source"]["institution_timezone"] == "America/New_York"
    assert explanation["source"]["payload"]["check_in_at"] == "2024-03-15T08:00:00-04:00"
    assert explanation["mapping"]["local_event_id"] == "EXG:NYU:NY-E1"
    assert explanation["mapping"]["status"] == "ACCEPTED"
    assert explanation["batch"]["batch_id"] == "B-1"
    assert explanation["rule"]["version"] == 1
    assert explanation["counts_toward_plan"] is True

    resp = client.get("/api/exchange/sources/NYU/events/NOPE")
    assert resp.status_code == 404


def test_foreign_mentor_confirm_promotes_foreign_checkin(client):
    setup_exchange(client, plan=SHANGHAI_PLAN)
    post_batch(
        client,
        make_envelope(
            batch_id="B-1",
            seq=1,
            entries=[
                checkin_entry(
                    "NY-E1",
                    "S1",
                    "2024-03-15T08:00:00-04:00",
                    "2024-03-15T12:00:00-04:00",
                    activity_type="internship",
                ),
                {
                    "event_id": "NY-E2",
                    "event_type": "mentor_confirm",
                    "student_id": "S1",
                    "payload": {"checkin_event_id": "NY-E1"},
                },
            ],
        ),
    )
    progress = _progress(client, "S1")
    assert progress["confirmed_seconds"] == 4 * 3600
    assert progress["pending_seconds"] == 0


def test_disputed_mentor_confirm_counts_after_upheld_arbitration(client):
    # 确认事件先到、签到后到(跨批次乱序),确认进入争议待定;
    # 裁决维持后引用改写为映射后的本校签到 id,签到转为确认。
    setup_exchange(client, plan=SHANGHAI_PLAN)
    post_batch(
        client,
        make_envelope(
            batch_id="B-1",
            seq=1,
            entries=[
                {
                    "event_id": "NY-C1",
                    "event_type": "mentor_confirm",
                    "student_id": "S1",
                    "payload": {"checkin_event_id": "NY-I1"},
                }
            ],
        ),
    )
    post_batch(
        client,
        make_envelope(
            batch_id="B-2",
            seq=2,
            entries=[
                checkin_entry(
                    "NY-I1",
                    "S1",
                    "2024-03-15T08:00:00-04:00",
                    "2024-03-15T12:00:00-04:00",
                    activity_type="internship",
                )
            ],
        ),
    )
    # 签到仍为待定:先到的确认事件此前已挂争议。
    assert _progress(client, "S1")["pending_seconds"] == 4 * 3600

    resp = client.post(
        "/api/exchange/arbitrations",
        json={
            "source_institution_code": "NYU",
            "source_event_id": "NY-C1",
            "verdict": "UPHELD",
            "reason": "外校导师确认与后续签到均已核实",
            "actor": "registrar-01",
        },
    )
    assert resp.status_code == 200, resp.text
    progress = _progress(client, "S1")
    assert progress["confirmed_seconds"] == 4 * 3600
    assert progress["pending_seconds"] == 0


def test_unmapped_event_type_is_skipped(client):
    setup_exchange(client, plan=SHANGHAI_PLAN)
    resp = post_batch(
        client,
        make_envelope(
            batch_id="B-1",
            seq=1,
            entries=[
                {
                    "event_id": "NY-E1",
                    "event_type": "attendance_ping",
                    "student_id": "S1",
                    "payload": {},
                }
            ],
        ),
    )
    assert resp.json()["skipped_count"] == 1
    events = client.get("/api/exchange/events").json()
    assert events[0]["status"] == "SKIPPED"
    assert events[0]["reason_code"] == "unmapped_event_type"


def test_batch_requires_active_rule_and_known_institution(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201
    envelope = make_envelope(batch_id="B-1", seq=1, entries=[])
    resp = post_batch(client, envelope)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "INSTITUTION_NOT_FOUND"

    setup_exchange(client, plan=SHANGHAI_PLAN)
    # 规则版本不存在。
    envelope = make_envelope(batch_id="B-1", seq=1, entries=[], rule_version=9)
    resp = post_batch(client, envelope)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "RULE_NOT_FOUND"
