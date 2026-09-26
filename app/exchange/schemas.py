"""跨校交换 API 的请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class InstitutionIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    display_name: str = Field(..., min_length=1, max_length=256)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    verification_key: str = Field(..., min_length=1, max_length=4096)
    is_active: bool = True


class InstitutionOut(BaseModel):
    code: str
    display_name: str
    iana_timezone: str
    is_active: bool


class RuleIn(BaseModel):
    rule_code: str = Field(..., min_length=1, max_length=64)
    local_plan_version: str = Field(..., min_length=1, max_length=128)
    partner_institution_code: str = Field(..., min_length=1, max_length=64)
    partner_plan_version: str | None = Field(None, max_length=128)
    event_type_mapping: dict[str, str]
    accepted_activity_types: list[str] | None = None


class RuleOut(BaseModel):
    rule_code: str
    version: int
    local_plan_version: str
    partner_institution_code: str
    partner_plan_version: str | None
    event_type_mapping: dict[str, str]
    accepted_activity_types: list[str] | None
    status: str
    published_at: str | None


class ExchangeEntryIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: str = Field(..., min_length=1, max_length=32)
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class ExchangeBatchIn(BaseModel):
    batch_id: str = Field(..., min_length=1, max_length=128)
    local_plan_version: str = Field(..., min_length=1, max_length=128)
    partner_institution_code: str = Field(..., min_length=1, max_length=64)
    partner_plan_version: str = Field(..., min_length=1, max_length=128)
    rule_code: str = Field(..., min_length=1, max_length=64)
    rule_version: int = Field(..., ge=1)
    seq: int = Field(..., ge=1)
    issued_at: str | None = None
    entries: list[ExchangeEntryIn]
    signature: str = Field(..., min_length=1, max_length=128)


class ExchangeBatchOut(BaseModel):
    batch_id: str
    local_plan_version: str
    partner_institution_code: str
    partner_plan_version: str
    rule_code: str
    rule_version: int
    seq: int
    status: str
    error_code: str | None
    entries_count: int
    accepted_count: int
    skipped_count: int
    disputed_count: int
    suppressed_count: int
    received_at: str | None
    processed_at: str | None
    duplicate_delivery: bool = False


class ExchangeEventOut(BaseModel):
    local_event_id: str
    local_plan_version: str
    batch_id: str
    source_institution_code: str
    source_event_id: str
    source_student_id: str
    source_event_type: str
    mapped_event_type: str | None
    status: str
    reason_code: str | None
    suppress_reason: str | None
    arbitration_reason: str | None
    arbitrated_by: str | None
    arbitrated_at: str | None


class ChannelReconcileOut(BaseModel):
    partner_institution_code: str
    next_expected_seq: int
    max_received_seq: int
    missing_seqs: list[int]
    waiting_batches: list[str]
    caught_up: bool


class ReconcileOut(BaseModel):
    local_plan_version: str
    totals: dict[str, int]
    channels: list[ChannelReconcileOut]
    open_disputes: list[ExchangeEventOut]
    open_dispute_count: int
    caught_up: bool


class ArbitrationIn(BaseModel):
    source_institution_code: str = Field(..., min_length=1, max_length=64)
    source_event_id: str = Field(..., min_length=1, max_length=128)
    verdict: str = Field(..., pattern="^(UPHELD|REJECTED)$")
    reason: str = Field(..., min_length=1)
    actor: str = Field(..., min_length=1, max_length=128)
    suppress: bool = False


class RecoverOut(BaseModel):
    channels_recovered: int
    batches: list[ExchangeBatchOut]


class SourceExplanationOut(BaseModel):
    source: dict[str, Any]
    mapping: ExchangeEventOut
    batch: dict[str, Any]
    rule: RuleOut | None
    counts_toward_plan: bool
    pending_until_arbitration: bool
