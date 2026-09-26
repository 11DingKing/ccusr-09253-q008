"""跨校交换测试的公共构造器。"""

from __future__ import annotations

from typing import Any

from app.exchange.signing import sign_batch

PARTNER_KEY = "partner-secret-key"


def make_envelope(
    *,
    batch_id: str,
    seq: int,
    entries: list[dict[str, Any]],
    local_plan_version: str = "P-SH-2024",
    partner_institution_code: str = "NYU",
    partner_plan_version: str = "P-NY-2024",
    rule_code: str = "R-NY-SH",
    rule_version: int = 1,
    key: str = PARTNER_KEY,
    signature: str | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "batch_id": batch_id,
        "local_plan_version": local_plan_version,
        "partner_institution_code": partner_institution_code,
        "partner_plan_version": partner_plan_version,
        "rule_code": rule_code,
        "rule_version": rule_version,
        "seq": seq,
        "entries": entries,
    }
    envelope["signature"] = (
        signature if signature is not None else sign_batch(envelope, key)
    )
    return envelope


def checkin_entry(
    event_id: str,
    student_id: str,
    check_in_at: str,
    check_out_at: str,
    *,
    activity_id: str = "A1",
    activity_type: str = "regular",
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": "checkin",
        "student_id": student_id,
        "payload": {
            "activity_id": activity_id,
            "activity_type": activity_type,
            "check_in_at": check_in_at,
            "check_out_at": check_out_at,
        },
    }


def setup_exchange(
    client,
    *,
    plan: dict[str, Any],
    partner_code: str = "NYU",
    partner_tz: str = "America/New_York",
    rule_code: str = "R-NY-SH",
    accepted_activity_types: list[str] | None = None,
    event_type_mapping: dict[str, str] | None = None,
    publish: bool = True,
) -> dict[str, Any]:
    """登记本校方案、合作院校与互认规则,返回规则版本号。"""
    resp = client.post("/api/plans", json=plan)
    assert resp.status_code == 201, resp.text
    resp = client.post(
        "/api/exchange/institutions",
        json={
            "code": partner_code,
            "display_name": "New York University",
            "iana_timezone": partner_tz,
            "verification_key": PARTNER_KEY,
        },
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        "/api/exchange/rules",
        json={
            "rule_code": rule_code,
            "local_plan_version": plan["plan_version"],
            "partner_institution_code": partner_code,
            "event_type_mapping": event_type_mapping
            or {
                "checkin": "checkin",
                "mentor_confirm": "mentor_confirm",
                "leave_correction": "leave_correction",
            },
            "accepted_activity_types": accepted_activity_types,
        },
    )
    assert resp.status_code == 201, resp.text
    version = resp.json()["version"]
    if publish:
        resp = client.post(
            f"/api/exchange/rules/{rule_code}/versions/{version}/publish"
        )
        assert resp.status_code == 200, resp.text
    return {"rule_code": rule_code, "version": version}


def post_batch(client, envelope: dict[str, Any]) -> Any:
    return client.post("/api/exchange/batches", json=envelope)
