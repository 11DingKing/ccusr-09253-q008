"""交换批次的签名构造与校验。

双方机构在登记时各自保存一个共享密钥(verification_key)。批次签名覆盖
除 signature 外的全部信封字段(含条目列表),采用确定性的规范化 JSON 与
HMAC-SHA256,保证乱序重传和重放时签名可重复验证。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

# 参与签名的信封字段(顺序无关,规范化时按键排序)。
SIGNED_FIELDS = (
    "batch_id",
    "local_plan_version",
    "partner_institution_code",
    "partner_plan_version",
    "rule_code",
    "rule_version",
    "seq",
    "issued_at",
    "entries",
)


def canonical_payload(envelope: dict[str, Any]) -> bytes:
    signed = {
        key: envelope[key]
        for key in SIGNED_FIELDS
        if key in envelope and envelope[key] is not None
    }
    return json.dumps(
        signed,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sign_payload(payload: bytes, key: str) -> str:
    return hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def sign_batch(envelope: dict[str, Any], key: str) -> str:
    """供发送方(与测试)生成签名。"""
    return sign_payload(canonical_payload(envelope), key)


def verify_signature(envelope: dict[str, Any], key: str) -> bool:
    provided = str(envelope.get("signature", ""))
    if not provided:
        return False
    expected = sign_payload(canonical_payload(envelope), key)
    return hmac.compare_digest(expected, provided)
