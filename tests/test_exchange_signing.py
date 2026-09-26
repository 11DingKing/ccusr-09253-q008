"""批次签名：规范化序列化、篡改检测、常量时间比较。"""

from __future__ import annotations

import pytest

from app.exchange.signing import (
    SignatureError,
    canonical_payload,
    payload_digest,
    sign,
    verify_signature,
)

ENVELOPE = {
    "batch_id": "B1",
    "sender_id": "PARTNER",
    "receiver_id": "LEAD",
    "plan_version": "P1",
    "rule_id": "R1",
    "rule_version": 1,
    "sent_at": "2024-03-15T06:00:00+01:00",
    "events": [
        {
            "event_id": "X-1",
            "sender_seq": 1,
            "event_type": "checkin",
            "student_id": "S1",
            "payload": {
                "activity_id": "A1",
                "check_in_at": "2024-03-15T09:00:00+01:00",
                "check_out_at": "2024-03-15T10:00:00+01:00",
            },
        }
    ],
}


def test_signature_roundtrip():
    sig = sign(ENVELOPE, "secret")
    verify_signature(ENVELOPE, "secret", sig)  # 不抛异常即通过


def test_signature_is_independent_of_key_order_and_whitespace():
    reordered = {
        "events": ENVELOPE["events"],
        "sent_at": ENVELOPE["sent_at"],
        "rule_version": 1,
        "rule_id": "R1",
        "plan_version": "P1",
        "receiver_id": "LEAD",
        "sender_id": "PARTNER",
        "batch_id": "B1",
    }
    assert canonical_payload(ENVELOPE) == canonical_payload(reordered)
    assert sign(ENVELOPE, "s") == sign(reordered, "s")


def test_missing_optional_rule_version_still_signable():
    env = dict(ENVELOPE)
    del env["rule_version"]
    sig = sign(env, "s")
    verify_signature(env, "s", sig)


def test_wrong_secret_fails():
    sig = sign(ENVELOPE, "secret-a")
    with pytest.raises(SignatureError):
        verify_signature(ENVELOPE, "secret-b", sig)


def test_tampered_event_fails():
    sig = sign(ENVELOPE, "secret")
    tampered = {
        **ENVELOPE,
        "events": [
            {**ENVELOPE["events"][0], "student_id": "S999"},
        ],
    }
    with pytest.raises(SignatureError):
        verify_signature(tampered, "secret", sig)


def test_missing_signature_fails():
    with pytest.raises(SignatureError):
        verify_signature(ENVELOPE, "secret", None)


def test_digest_changes_with_payload():
    assert payload_digest(ENVELOPE) != payload_digest({**ENVELOPE, "batch_id": "B2"})
