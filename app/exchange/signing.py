"""批次载荷的规范化摘要与 HMAC-SHA256 签名。

签名覆盖批次信封与全部事件，时间戳必须带时区（跨校时区不同，
统一在验签后归一为 UTC，签名本身不依赖本地时钟解释）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

CANONICAL_SEPARATORS = (",", ":")

ENVELOPE_KEYS = (
    "batch_id",
    "sender_id",
    "receiver_id",
    "plan_version",
    "rule_id",
    "rule_version",
    "sent_at",
)


class SignatureError(ValueError):
    """封装领域状态与业务约束。"""


def canonical_payload(envelope: dict[str, Any]) -> bytes:
    """对信封做确定性 JSON 序列化；键序与空白差异不会影响验签。"""
    canonical: dict[str, Any] = {key: envelope.get(key) for key in ENVELOPE_KEYS}
    # 事件不参与信封键排序，单独拷贝并逐元素规范化。
    canonical["events"] = [
        json.loads(json.dumps(event, sort_keys=True, ensure_ascii=False))
        for event in envelope.get("events", [])
    ]
    text = json.dumps(
        canonical,
        sort_keys=True,
        ensure_ascii=False,
        separators=CANONICAL_SEPARATORS,
    )
    return text.encode("utf-8")


def payload_digest(envelope: dict[str, Any]) -> str:
    """载荷的 SHA-256 摘要，用于批次幂等与对账。"""
    return hashlib.sha256(canonical_payload(envelope)).hexdigest()


def sign(envelope: dict[str, Any], secret: str) -> str:
    """用发送院校登记的密钥对批次签名（HMAC-SHA256，十六进制）。"""
    return hmac.new(
        secret.encode("utf-8"), canonical_payload(envelope), hashlib.sha256
    ).hexdigest()


def verify_signature(
    envelope: dict[str, Any], secret: str, signature: str | None
) -> None:
    """校验失败抛出 SignatureError；常量时间比较防时序泄露。"""
    if not signature:
        raise SignatureError("批次缺少签名")
    expected = sign(envelope, secret)
    if not hmac.compare_digest(expected, signature):
        raise SignatureError("批次签名校验失败")
