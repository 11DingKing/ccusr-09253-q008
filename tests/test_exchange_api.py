"""跨校互认 API：接收、校验、对账、裁决、来源解释的端到端测试。

覆盖乱序交换、跨时区（含 DST）、重复来源与重启恢复。
"""

from __future__ import annotations

from app.exchange.signing import sign
from tests.conftest import TestSessionLocal

PLAN = {
    "plan_version": "P-SH",
    "iana_timezone": "Asia/Shanghai",
    "required_seconds": 3600,
}

LEAD = "LEAD"
PARTNER = "PARTNER"
PARTNER_SECRET = "partner-secret"
LEAD_SECRET = "lead-secret"


def _setup(client, plan=PLAN):
    resp = client.post("/api/plans", json=plan)
    assert resp.status_code == 201, resp.text
    client.post(
        "/api/exchange/institutions",
        json={
            "institution_id": LEAD,
            "name": "牵头院校",
            "iana_timezone": "Asia/Shanghai",
            "signing_secret": LEAD_SECRET,
        },
    )
    client.post(
        "/api/exchange/institutions",
        json={
            "institution_id": PARTNER,
            "name": "合作院校",
            "iana_timezone": "Europe/Berlin",
            "signing_secret": PARTNER_SECRET,
        },
    )


def _publish_rule(
    client,
    *,
    rule_id="R1",
    activity_map=None,
    cap_seconds=None,
    sender=PARTNER,
    receiver=LEAD,
    plan=PLAN["plan_version"],
):
    resp = client.post(
        "/api/exchange/rules",
        json={
            "rule_id": rule_id,
            "sender_id": sender,
            "receiver_id": receiver,
            "plan_version": plan,
            "activity_map": activity_map
            or {"regular": "regular", "internship": "internship"},
            "cap_seconds": cap_seconds,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["version"]


def _checkin(eid, seq, student, start, end, **kw):
    payload = {
        "activity_id": kw.get("activity_id", f"A-{eid}"),
        "activity_type": kw.get("atype", "regular"),
        "check_in_at": start,
        "check_out_at": end,
    }
    if "tz" in kw:
        payload["timezone_hint"] = kw["tz"]
    return {
        "event_id": eid,
        "sender_seq": seq,
        "event_type": "checkin",
        "student_id": student,
        "payload": payload,
    }


def _envelope(batch_id, events, *, rule_id="R1", rule_version=None, plan=PLAN["plan_version"]):
    env = {
        "batch_id": batch_id,
        "sender_id": PARTNER,
        "receiver_id": LEAD,
        "plan_version": plan,
        "rule_id": rule_id,
        "sent_at": "2024-03-15T06:00:00+01:00",
        "events": events,
    }
    if rule_version is not None:
        env["rule_version"] = rule_version
    return env


def _submit(client, envelope, secret=PARTNER_SECRET):
    return client.post(
        "/api/exchange/batches",
        json={"envelope": envelope, "signature": sign(envelope, secret)},
    )


def _local_checkin(client, eid, student, start, end, activity_id="L"):
    resp = client.post(
        f"/api/plans/{PLAN['plan_version']}/events",
        json={
            "events": [
                {
                    "event_id": eid,
                    "event_type": "checkin",
                    "student_id": student,
                    "payload": {
                        "activity_id": activity_id,
                        "activity_type": "regular",
                        "check_in_at": start,
                        "check_out_at": end,
                    },
                }
            ]
        },
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# 接收与校验
# ---------------------------------------------------------------------------


def test_receive_signed_batch_maps_foreign_event(client):
    _setup(client)
    _publish_rule(client)
    env = _envelope(
        "B1",
        [
            _checkin(
                "X-1", 1, "S1",
                "2024-03-15T09:00:00+01:00", "2024-03-15T11:00:00+01:00",
                tz="Europe/Berlin",
            )
        ],
    )
    resp = _submit(client, env)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "applied"
    assert body["accepted_count"] == 1
    assert body["rule_version"] == 1
    assert body["resumed"] is False

    progress = client.get(
        f"/api/plans/{PLAN['plan_version']}/students/S1/progress"
    ).json()
    # 08:00-10:00 UTC = 2 小时。
    assert progress["total_seconds"] == 7200


def test_validate_endpoint_previews_without_persisting(client):
    _setup(client)
    _publish_rule(client, activity_map={"regular": "regular"})
    env = _envelope(
        "B1",
        [
            _checkin("X-1", 1, "S1", "2024-03-15T09:00:00+01:00", "2024-03-15T10:00:00+01:00"),
            _checkin("X-2", 2, "S1", "2024-03-15T11:00:00+01:00", "2024-03-15T12:00:00+01:00", atype="workshop"),
        ],
    )
    resp = client.post(
        "/api/exchange/batches/validate",
        json={"envelope": env, "signature": sign(env, PARTNER_SECRET)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["signature_valid"] is True
    assert body["accepted_count"] == 1
    assert body["quarantined_count"] == 1
    assert body["duplicate_batch"] is False
    # 未落库：恢复 404、对账 404。
    assert client.post("/api/exchange/batches/B1/resume").status_code == 404
    assert client.get("/api/exchange/batches/B1/reconcile").status_code == 404

    # 正式接收后再预检，同载荷被标记为重复批次。
    assert _submit(client, env).status_code == 201
    again = client.post(
        "/api/exchange/batches/validate",
        json={"envelope": env, "signature": sign(env, PARTNER_SECRET)},
    ).json()
    assert again["duplicate_batch"] is True


def test_bad_signature_is_rejected_and_not_persisted(client):
    _setup(client)
    _publish_rule(client)
    env = _envelope(
        "B1",
        [_checkin("X-1", 1, "S1", "2024-03-15T09:00:00+01:00", "2024-03-15T10:00:00+01:00")],
    )
    resp = client.post(
        "/api/exchange/batches",
        json={"envelope": env, "signature": "0" * 64},
    )
    assert resp.status_code == 401
    # 批次未落库，恢复接口 404。
    assert client.post("/api/exchange/batches/B1/resume").status_code == 404


def test_unknown_and_inactive_sender_are_rejected(client):
    _setup(client)
    _publish_rule(client)
    env = _envelope(
        "B1",
        [_checkin("X-1", 1, "S1", "2024-03-15T09:00:00+01:00", "2024-03-15T10:00:00+01:00")],
    )
    rogue = dict(env)
    rogue["sender_id"] = "GHOST"
    rogue["batch_id"] = "B2"
    resp = client.post(
        "/api/exchange/batches",
        json={"envelope": rogue, "signature": sign(rogue, PARTNER_SECRET)},
    )
    assert resp.status_code == 404

    # 停用发送方。
    client.post(
        "/api/exchange/institutions",
        json={
            "institution_id": PARTNER,
            "name": "合作院校",
            "iana_timezone": "Europe/Berlin",
            "signing_secret": PARTNER_SECRET,
            "active": False,
        },
    )
    resp = _submit(client, dict(env, batch_id="B3"))
    assert resp.status_code == 403


def test_same_batch_id_with_different_payload_conflicts(client):
    _setup(client)
    _publish_rule(client)
    env = _envelope(
        "B1",
        [_checkin("X-1", 1, "S1", "2024-03-15T09:00:00+01:00", "2024-03-15T10:00:00+01:00")],
    )
    assert _submit(client, env).status_code == 201
    tampered = _envelope(
        "B1",
        [_checkin("X-1", 1, "S1", "2024-03-15T09:00:00+01:00", "2024-03-15T11:00:00+01:00")],
    )
    assert _submit(client, tampered).status_code == 409


# ---------------------------------------------------------------------------
# 乱序交换
# ---------------------------------------------------------------------------


def test_out_of_order_batches_converge_to_same_union(client):
    _setup(client)
    _publish_rule(client)

    # B1: 柏林 03:00-05:00 (+01) = 02:00-04:00 UTC
    # B2: 柏林 04:00-06:30 (+01) = 03:00-05:30 UTC（与 B1 重叠 1 小时）
    b1 = _envelope(
        "B1",
        [_checkin("X-1", 1, "S1", "2024-03-15T03:00:00+01:00", "2024-03-15T05:00:00+01:00")],
    )
    b2 = _envelope(
        "B2",
        [_checkin("Y-1", 1, "S1", "2024-03-15T04:00:00+01:00", "2024-03-15T06:30:00+01:00")],
    )

    # 先收 B2 再收 B1（逆序交换）。
    assert _submit(client, b2).status_code == 201
    assert _submit(client, b1).status_code == 201

    progress = client.get(
        f"/api/plans/{PLAN['plan_version']}/students/S1/progress"
    ).json()
    # 并集 02:00-05:30 UTC = 3.5 小时，与先 B1 后 B2 完全相同。
    assert progress["total_seconds"] == 12600

    rec = client.get(
        f"/api/exchange/plans/{PLAN['plan_version']}/reconcile"
    ).json()
    assert rec["balanced"] is True
    assert rec["batch_count"] == 2


def test_within_batch_events_arrive_out_of_seq_but_map_deterministically(client):
    _setup(client)
    _publish_rule(client)
    # 序号逆序送达，且区间首尾相接。
    env = _envelope(
        "B1",
        [
            _checkin("X-3", 3, "S1", "2024-03-15T05:00:00+01:00", "2024-03-15T06:00:00+01:00", activity_id="A3"),
            _checkin("X-1", 1, "S1", "2024-03-15T03:00:00+01:00", "2024-03-15T04:00:00+01:00", activity_id="A1"),
            _checkin("X-2", 2, "S1", "2024-03-15T04:00:00+01:00", "2024-03-15T05:00:00+01:00", activity_id="A2"),
        ],
    )
    assert _submit(client, env).status_code == 201
    progress = client.get(
        f"/api/plans/{PLAN['plan_version']}/students/S1/progress"
    ).json()
    assert progress["total_seconds"] == 3 * 3600


# ---------------------------------------------------------------------------
# 跨时区
# ---------------------------------------------------------------------------


def test_cross_timezone_event_counts_real_elapsed_time(client):
    _setup(client)
    _publish_rule(client)
    env = _envelope(
        "B1",
        [
            _checkin(
                "X-1", 1, "S1",
                "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00",
                tz="Asia/Shanghai",
            )
        ],
    )
    assert _submit(client, env).status_code == 201
    progress = client.get(
        f"/api/plans/{PLAN['plan_version']}/students/S1/progress"
    ).json()
    assert progress["total_seconds"] == 4 * 3600
    days = {d["academic_day"]: d["seconds"] for d in progress["daily"]}
    # 按本校（上海）教学日切分。
    assert days == {"2024-03-15": 2 * 3600, "2024-03-16": 2 * 3600}


def test_new_york_dst_fallback_event_counts_real_hours(client):
    _setup(client)
    _publish_rule(client)
    # 2024-11-03 美东回拨夜：01:30 EDT(-04) -> 04:00 EST(-05) = 3.5 真实小时。
    env = _envelope(
        "B1",
        [
            _checkin(
                "X-1", 1, "S1",
                "2024-11-03T01:30:00-04:00", "2024-11-03T04:00:00-05:00",
                tz="America/New_York",
            )
        ],
    )
    assert _submit(client, env).status_code == 201
    progress = client.get(
        f"/api/plans/{PLAN['plan_version']}/students/S1/progress"
    ).json()
    assert progress["total_seconds"] == 12600


# ---------------------------------------------------------------------------
# 重复来源
# ---------------------------------------------------------------------------


def test_same_activity_from_two_schools_does_not_double_count(client):
    _setup(client)
    _publish_rule(client)
    # 本校先登记联合活动 09:00-11:30 上海。
    _local_checkin(
        client, "L-1", "S1",
        "2024-03-15T09:00:00+08:00", "2024-03-15T11:30:00+08:00",
        activity_id="JOINT",
    )
    # 合作校就同一 activity_id 再次上报。
    env = _envelope(
        "B1",
        [
            _checkin(
                "X-1", 1, "S1",
                "2024-03-15T09:30:00+08:00", "2024-03-15T12:00:00+08:00",
                activity_id="JOINT",
            )
        ],
    )
    body = _submit(client, env).json()
    assert body["duplicate_count"] == 1
    assert body["accepted_count"] == 0
    progress = client.get(
        f"/api/plans/{PLAN['plan_version']}/students/S1/progress"
    ).json()
    assert progress["total_seconds"] == int(2.5 * 3600)


def test_overlapping_external_interval_clips_to_uncovered_part(client):
    _setup(client)
    _publish_rule(client)
    _local_checkin(
        client, "L-1", "S1",
        "2024-03-15T09:00:00+08:00", "2024-03-15T11:00:00+08:00",
        activity_id="LOCAL-A",
    )
    # 柏林 03:00-05:30 (+01) = 02:00-04:30 UTC = 上海 10:00-12:30。
    env = _envelope(
        "B1",
        [
            _checkin(
                "X-1", 1, "S1",
                "2024-03-15T03:00:00+01:00", "2024-03-15T05:30:00+01:00",
                activity_id="PARTNER-B",
            )
        ],
    )
    _submit(client, env)
    progress = client.get(
        f"/api/plans/{PLAN['plan_version']}/students/S1/progress"
    ).json()
    # 并集：01:00-04:30 UTC = 3.5 小时（本地 2h + 外校补 1.5h）。
    assert progress["total_seconds"] == 12600


def test_resending_same_batch_is_idempotent(client):
    _setup(client)
    _publish_rule(client)
    env = _envelope(
        "B1",
        [_checkin("X-1", 1, "S1", "2024-03-15T09:00:00+01:00", "2024-03-15T11:00:00+01:00")],
    )
    first = _submit(client, env).json()
    second = _submit(client, env).json()
    assert first["resumed"] is False
    assert second["resumed"] is True
    assert second["accepted_count"] == 1
    progress = client.get(
        f"/api/plans/{PLAN['plan_version']}/students/S1/progress"
    ).json()
    assert progress["total_seconds"] == 7200


# ---------------------------------------------------------------------------
# 重启恢复
# ---------------------------------------------------------------------------


def test_resume_after_restart_realigns_batch_without_double_counting(client):
    _setup(client)
    _publish_rule(client)
    env_a = _envelope(
        "B1",
        [_checkin("X-1", 1, "S1", "2024-03-15T03:00:00+01:00", "2024-03-15T05:00:00+01:00")],
    )
    env_b = _envelope(
        "B2",
        [_checkin("Y-1", 1, "S1", "2024-03-15T04:00:00+01:00", "2024-03-15T06:00:00+01:00")],
    )
    _submit(client, env_a)
    _submit(client, env_b)
    assert (
        client.get(f"/api/plans/{PLAN['plan_version']}/students/S1/progress")
        .json()["total_seconds"]
        == 3 * 3600
    )

    # 模拟服务重启：用全新会话依次恢复两个批次（含逆序恢复）。
    for bid in ("B2", "B1", "B1"):
        resp = client.post(f"/api/exchange/batches/{bid}/resume")
        assert resp.status_code == 200, resp.text
        assert resp.json()["resumed"] is True

    progress = client.get(
        f"/api/plans/{PLAN['plan_version']}/students/S1/progress"
    ).json()
    assert progress["total_seconds"] == 3 * 3600

    rec = client.get("/api/exchange/batches/B1/reconcile").json()
    assert rec["balanced"] is True
    assert rec["remap_consistent"] is True


def test_resume_unknown_batch_returns_404(client):
    _setup(client)
    assert client.post("/api/exchange/batches/NOPE/resume").status_code == 404


def test_recovery_survives_engine_restart(client, tmp_path):
    """销毁并重建数据库引擎（模拟进程重启）后，批次仍可恢复且不重复计时。"""
    _setup(client)
    _publish_rule(client)
    db_path = tmp_path / "restart.db"
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.main import app as fastapi_app
    from app.models import Base
    from app.db import get_db

    engine_a = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine_a)
    SessionA = sessionmaker(bind=engine_a)

    def _client_with(engine):
        session = sessionmaker(bind=engine)()

        def override():
            yield session

        fastapi_app.dependency_overrides[get_db] = override
        return TestClientFactory(fastapi_app), session

    from fastapi.testclient import TestClient as TestClientFactory

    c1, s1 = _client_with(engine_a)
    c1.post("/api/plans", json=PLAN)
    c1.post(
        "/api/exchange/institutions",
        json={"institution_id": LEAD, "name": "牵头", "iana_timezone": "Asia/Shanghai", "signing_secret": LEAD_SECRET},
    )
    c1.post(
        "/api/exchange/institutions",
        json={"institution_id": PARTNER, "name": "合作", "iana_timezone": "Europe/Berlin", "signing_secret": PARTNER_SECRET},
    )
    c1.post(
        "/api/exchange/rules",
        json={"rule_id": "R1", "sender_id": PARTNER, "receiver_id": LEAD,
              "plan_version": PLAN["plan_version"],
              "activity_map": {"regular": "regular"}, "cap_seconds": None},
    )
    env = _envelope(
        "B1",
        [_checkin("X-1", 1, "S1", "2024-03-15T09:00:00+01:00", "2024-03-15T11:00:00+01:00")],
    )
    assert c1.post(
        "/api/exchange/batches",
        json={"envelope": env, "signature": sign(env, PARTNER_SECRET)},
    ).status_code == 201
    s1.close()
    engine_a.dispose()  # “进程退出”

    # 新引擎指向同一数据库文件重新打开。
    engine_b = create_engine(f"sqlite:///{db_path}")
    c2, s2 = _client_with(engine_b)
    resp = c2.post("/api/exchange/batches/B1/resume")
    assert resp.status_code == 200, resp.text
    assert resp.json()["resumed"] is True
    progress = c2.get(
        f"/api/plans/{PLAN['plan_version']}/students/S1/progress"
    ).json()
    assert progress["total_seconds"] == 7200
    rec = c2.get("/api/exchange/batches/B1/reconcile").json()
    assert rec["balanced"] is True
    s2.close()
    engine_b.dispose()
    fastapi_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 规则升级只影响新批次
# ---------------------------------------------------------------------------


def test_rule_upgrade_only_affects_new_batches(client):
    _setup(client)
    v1 = _publish_rule(client, activity_map={"regular": "regular"})
    env1 = _envelope(
        "B1",
        [_checkin("X-1", 1, "S1", "2024-03-15T09:00:00+01:00", "2024-03-15T10:00:00+01:00")],
    )
    _submit(client, env1)

    # 升级：regular 一律按实习处理（需导师确认 -> 挂起，不计学时）。
    v2 = _publish_rule(client, activity_map={"regular": "internship"})
    assert v2 == v1 + 1
    versions = client.get("/api/exchange/rules/R1/versions").json()
    assert [v["version"] for v in versions] == [1, 2]

    env2 = _envelope(
        "B2",
        [_checkin("Y-1", 1, "S1", "2024-03-15T11:00:00+01:00", "2024-03-15T12:00:00+01:00")],
    )
    _submit(client, env2)

    progress = client.get(
        f"/api/plans/{PLAN['plan_version']}/students/S1/progress"
    ).json()
    # B1 旧规则：regular 已计 1 小时；B2 新规则：internship 挂起不计。
    assert progress["confirmed_seconds"] == 3600
    assert progress["pending_seconds"] == 3600

    b1 = client.get("/api/exchange/batches/B1/events").json()
    b2 = client.get("/api/exchange/batches/B2/events").json()
    assert b1[0]["rule_version"] == 1
    assert b2[0]["rule_version"] == 2

    # 恢复旧批次不会被新规则改写。
    client.post("/api/exchange/batches/B1/resume")
    assert client.get("/api/exchange/batches/B1/events").json()[0]["rule_version"] == 1


# ---------------------------------------------------------------------------
# 争议与裁决
# ---------------------------------------------------------------------------


def test_quarantined_event_pends_until_adjudication(client):
    _setup(client)
    _publish_rule(client, activity_map={"regular": "regular"})
    env = _envelope(
        "B1",
        [
            _checkin(
                "X-1", 1, "S1",
                "2024-03-15T09:00:00+01:00", "2024-03-15T10:00:00+01:00",
                atype="workshop",
            )
        ],
    )
    body = _submit(client, env).json()
    assert body["quarantined_count"] == 1

    # 裁决前不计学时。
    progress = client.get(
        f"/api/plans/{PLAN['plan_version']}/students/S1/progress"
    )
    assert progress.status_code == 404  # 没有任何已采信事件

    listing = client.get("/api/exchange/batches/B1/events").json()
    assert listing[0]["status"] == "quarantined"
    assert listing[0]["resolution"] is None

    # 来源解释显示一条待裁决争议。
    prov = client.get(
        f"/api/exchange/plans/{PLAN['plan_version']}/students/S1/provenance"
    ).json()
    assert len(prov["pending_disputes"]) == 1

    # 驳回：永不计学时。
    resp = client.post(
        "/api/exchange/batches/B1/events/X-1/adjudicate",
        json={"decision": "reject", "adjudicator": "dean-chen", "note": "无互认依据"},
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "rejected"
    assert out["resolution"] == "rejected"
    assert out["adjudicator"] == "dean-chen"

    # 二次裁决被拒绝。
    again = client.post(
        "/api/exchange/batches/B1/events/X-1/adjudicate",
        json={"decision": "accept", "adjudicator": "dean-chen"},
    )
    assert again.status_code == 409

    rec = client.get("/api/exchange/batches/B1/reconcile").json()
    assert rec["balanced"] is True
    assert rec["pending_disputes"] == 0


def test_adjudication_accept_manual_override_counts_event(client):
    _setup(client)
    _publish_rule(client, activity_map={"regular": "regular"})
    env = _envelope(
        "B1",
        [
            _checkin(
                "X-1", 1, "S1",
                "2024-03-15T09:00:00+01:00", "2024-03-15T11:00:00+01:00",
                atype="workshop",
            )
        ],
    )
    _submit(client, env)
    resp = client.post(
        "/api/exchange/batches/B1/events/X-1/adjudicate",
        json={"decision": "accept", "adjudicator": "dean-chen", "note": "人工采信"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "accepted"

    progress = client.get(
        f"/api/plans/{PLAN['plan_version']}/students/S1/progress"
    ).json()
    assert progress["total_seconds"] == 7200

    prov = client.get(
        f"/api/exchange/plans/{PLAN['plan_version']}/students/S1/provenance"
    ).json()
    ext_sources = [s for s in prov["sources"] if s["origin"] == "external"]
    assert len(ext_sources) == 1
    assert ext_sources[0]["manual_override"] is True
    assert ext_sources[0]["adjudicator"] == "dean-chen"
    assert prov["pending_disputes"] == []


def test_adjudicate_nonexistent_event_404_and_bad_decision_422(client):
    _setup(client)
    _publish_rule(client)
    env = _envelope(
        "B1",
        [_checkin("X-1", 1, "S1", "2024-03-15T09:00:00+01:00", "2024-03-15T10:00:00+01:00")],
    )
    _submit(client, env)
    assert (
        client.post(
            "/api/exchange/batches/B1/events/NOPE/adjudicate",
            json={"decision": "accept", "adjudicator": "dean"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/exchange/batches/B1/events/X-1/adjudicate",
            json={"decision": "accept", "adjudicator": "dean"},
        ).status_code
        == 409  # 已 accepted 的事件无需裁决
    )
    assert (
        client.post(
            "/api/exchange/batches/B1/events/X-1/adjudicate",
            json={"decision": "maybe", "adjudicator": "dean"},
        ).status_code
        == 422
    )


# ---------------------------------------------------------------------------
# 对账与来源解释
# ---------------------------------------------------------------------------


def test_reconcile_detects_tampered_signature(client):
    _setup(client)
    _publish_rule(client)
    env = _envelope(
        "B1",
        [_checkin("X-1", 1, "S1", "2024-03-15T09:00:00+01:00", "2024-03-15T10:00:00+01:00")],
    )
    _submit(client, env)

    session = TestSessionLocal()
    try:
        from app.models import ExchangeBatch

        batch = session.get(ExchangeBatch, "B1")
        batch.signature = "0" * 64
        session.commit()
    finally:
        session.close()

    rec = client.get("/api/exchange/batches/B1/reconcile").json()
    assert rec["signature_valid"] is False
    assert rec["balanced"] is False
    assert "signature_invalid" in rec["discrepancies"]


def test_provenance_explains_local_and_external_sources(client):
    _setup(client)
    _publish_rule(client, cap_seconds=4 * 3600)
    _local_checkin(
        client, "L-1", "S1",
        "2024-03-15T09:00:00+08:00", "2024-03-15T10:00:00+08:00",
    )
    env = _envelope(
        "B1",
        [
            _checkin(
                "X-1", 1, "S1",
                "2024-03-15T10:00:00+08:00", "2024-03-15T12:00:00+08:00",
                tz="Asia/Shanghai",
            )
        ],
    )
    _submit(client, env)
    prov = client.get(
        f"/api/exchange/plans/{PLAN['plan_version']}/students/S1/provenance"
    ).json()
    origins = {s["local_event_id"]: s["origin"] for s in prov["sources"]}
    assert origins["L-1"] == "local"
    external = [s for s in prov["sources"] if s["origin"] == "external"]
    assert len(external) == 1
    assert external[0]["sender_id"] == PARTNER
    assert external[0]["external_event_id"] == "X-1"
    assert external[0]["batch_id"] == "B1"
    assert external[0]["rule_version"] == 1
    assert prov["seconds_by_origin"]["local"] == 3600
    assert prov["seconds_by_sender"][PARTNER] == 7200
    assert prov["rules_applied"][0]["cap_seconds"] == 4 * 3600


def test_plan_reconcile_aggregates_batches(client):
    _setup(client)
    _publish_rule(client)
    for bid, seq in (("B1", 1), ("B2", 2)):
        env = _envelope(
            bid,
            [_checkin(f"X-{seq}", seq, "S1",
                      f"2024-03-1{4+seq}T09:00:00+01:00",
                      f"2024-03-1{4+seq}T10:00:00+01:00")],
        )
        assert _submit(client, env).status_code == 201
    rec = client.get(
        f"/api/exchange/plans/{PLAN['plan_version']}/reconcile"
    ).json()
    assert rec["batch_count"] == 2
    assert rec["balanced"] is True
