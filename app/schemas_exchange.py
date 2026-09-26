"""跨校互认 API 的请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_ALLOWED_LOCAL_ACTIVITY_TYPES = {"regular", "internship"}


class InstitutionIn(BaseModel):
    institution_id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=256)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    signing_secret: str = Field(..., min_length=1, max_length=256)
    active: bool = True

    @field_validator("iana_timezone")
    @classmethod
    def _check_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:  # pragma: no cover - 输入校验
            raise ValueError("iana_timezone 不是合法的 IANA 时区") from exc
        return v


class InstitutionOut(BaseModel):
    institution_id: str
    name: str
    iana_timezone: str
    active: bool


class RuleIn(BaseModel):
    rule_id: str = Field(..., min_length=1, max_length=64)
    sender_id: str = Field(..., min_length=1, max_length=64)
    receiver_id: str = Field(..., min_length=1, max_length=64)
    plan_version: str = Field(..., min_length=1, max_length=128)
    activity_map: dict[str, str]
    cap_seconds: int | None = Field(None, ge=0)

    @model_validator(mode="after")
    def _check_activity_map(self) -> "RuleIn":
        if not self.activity_map:
            raise ValueError("activity_map 不能为空")
        bad = [v for v in self.activity_map.values() if v not in _ALLOWED_LOCAL_ACTIVITY_TYPES]
        if bad:
            raise ValueError(
                "activity_map 的目标类型只能是 regular 或 internship"
            )
        return self


class RuleOut(BaseModel):
    rule_id: str
    version: int
    sender_id: str
    receiver_id: str
    plan_version: str
    activity_map: dict[str, str]
    cap_seconds: int | None
    active: bool


class ExchangeEventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    sender_seq: int = Field(..., ge=0)
    event_type: str = Field(..., min_length=1)
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class BatchEnvelopeIn(BaseModel):
    batch_id: str = Field(..., min_length=1, max_length=128)
    sender_id: str = Field(..., min_length=1, max_length=64)
    receiver_id: str = Field(..., min_length=1, max_length=64)
    plan_version: str = Field(..., min_length=1, max_length=128)
    rule_id: str = Field(..., min_length=1, max_length=64)
    # 缺省 = 接收方采用当前生效版本；显式给出 = 必须存在且匹配。
    rule_version: int | None = Field(None, ge=1)
    sent_at: str = Field(..., min_length=1, max_length=64)
    events: list[ExchangeEventIn]


class BatchSubmissionIn(BaseModel):
    envelope: BatchEnvelopeIn
    signature: str = Field(..., min_length=1, max_length=128)


class BatchValidateOut(BaseModel):
    batch_id: str
    signature_valid: bool
    duplicate_batch: bool
    rule_id: str
    rule_version: int
    expected_count: int
    accepted_count: int
    duplicate_count: int
    quarantined_count: int
    quarantined: list[dict[str, Any]]


class BatchOut(BaseModel):
    batch_id: str
    sender_id: str
    receiver_id: str
    plan_version: str
    rule_id: str
    rule_version: int
    status: str
    sent_at: str
    expected_count: int
    accepted_count: int
    duplicate_count: int
    quarantined_count: int
    payload_digest: str
    resumed: bool = False


class ExternalEventOut(BaseModel):
    batch_id: str
    sender_id: str
    receiver_id: str
    plan_version: str
    external_event_id: str
    sender_seq: int
    student_id: str
    event_type: str
    status: str
    local_event_id: str | None
    rule_id: str
    rule_version: int
    reason: str
    resolution: str | None
    adjudicator: str | None
    adjudication_note: str | None
    adjudicated_at: str | None
    occurred_at_utc: str | None


class AdjudicationIn(BaseModel):
    decision: str
    adjudicator: str = Field(..., min_length=1, max_length=128)
    note: str = Field("", max_length=512)

    @field_validator("decision")
    @classmethod
    def _check_decision(cls, v: str) -> str:
        if v not in {"accept", "reject"}:
            raise ValueError("decision 必须是 accept 或 reject")
        return v


class ReconcileOut(BaseModel):
    batch_id: str
    status: str
    signature_valid: bool
    payload_digest_match: bool
    remap_consistent: bool
    counts: dict[str, int]
    stored_counts: dict[str, int]
    pending_disputes: int
    balanced: bool
    discrepancies: list[str]


class PlanReconcileOut(BaseModel):
    plan_version: str
    batch_count: int
    balanced: bool
    pending_disputes: int
    batches: list[ReconcileOut]


class ProvenanceOut(BaseModel):
    plan_version: str
    student_id: str
    sources: list[dict[str, Any]]
    seconds_by_origin: dict[str, int]
    seconds_by_sender: dict[str, int]
    rules_applied: list[dict[str, Any]]
    pending_disputes: list[ExternalEventOut]
