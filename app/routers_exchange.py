"""跨校实训互认 API。

覆盖：机构身份登记、互认规则版本登记、批次接收/恢复、校验对账、
争议裁决与来源解释。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import exchange_service as xsvc
from .db import get_db
from .schemas_exchange import (
    AdjudicationIn,
    BatchOut,
    BatchSubmissionIn,
    BatchValidateOut,
    ExternalEventOut,
    InstitutionIn,
    InstitutionOut,
    PlanReconcileOut,
    ProvenanceOut,
    ReconcileOut,
    RuleIn,
    RuleOut,
)

router = APIRouter(prefix="/api/exchange")


@router.post(
    "/institutions",
    response_model=InstitutionOut,
    status_code=status.HTTP_201_CREATED,
)
def post_institution(body: InstitutionIn, db: Session = Depends(get_db)) -> Any:
    try:
        return xsvc.register_institution(db, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/institutions", response_model=list[InstitutionOut])
def list_institutions(db: Session = Depends(get_db)) -> Any:
    return xsvc.list_institutions_plain(db)


@router.post(
    "/rules", response_model=RuleOut, status_code=status.HTTP_201_CREATED
)
def post_rule(body: RuleIn, db: Session = Depends(get_db)) -> Any:
    try:
        return xsvc.publish_rule_version(db, **body.model_dump())
    except xsvc.InstitutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except xsvc.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/rules/{rule_id}/versions", response_model=list[RuleOut])
def list_rule_versions(rule_id: str, db: Session = Depends(get_db)) -> Any:
    versions = xsvc.list_rule_versions_plain(db, rule_id)
    if not versions:
        raise HTTPException(status_code=404, detail="rule not found")
    return versions


# ---------------------------------------------------------------------------
# 批次接收 / 恢复
# ---------------------------------------------------------------------------


@router.post("/batches/validate", response_model=BatchValidateOut)
def validate_batch(body: BatchSubmissionIn, db: Session = Depends(get_db)) -> Any:
    envelope = body.envelope.model_dump()
    try:
        return xsvc.validate_batch(db, envelope, body.signature)
    except xsvc.SignatureError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except xsvc.InstitutionInactiveError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (xsvc.InstitutionNotFoundError, xsvc.RuleNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except xsvc.BatchConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except xsvc.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/batches", response_model=BatchOut, status_code=status.HTTP_201_CREATED
)
def post_batch(body: BatchSubmissionIn, db: Session = Depends(get_db)) -> Any:
    envelope = body.envelope.model_dump()
    try:
        return xsvc.receive_batch(db, envelope, body.signature)
    except xsvc.SignatureError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except xsvc.InstitutionInactiveError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (
        xsvc.InstitutionNotFoundError,
        xsvc.RuleNotFoundError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except xsvc.BatchConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except xsvc.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/batches/{batch_id}/resume", response_model=BatchOut)
def resume_batch(batch_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return xsvc.resume_batch(db, batch_id)
    except xsvc.ExternalEventNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except xsvc.SignatureError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/batches/{batch_id}/reconcile",
    response_model=ReconcileOut,
)
def reconcile_batch(batch_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return xsvc.reconcile_batch(db, batch_id)
    except xsvc.ExternalEventNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/reconcile",
    response_model=PlanReconcileOut,
)
def reconcile_plan(plan_version: str, db: Session = Depends(get_db)) -> Any:
    return xsvc.reconcile_plan(db, plan_version)


# ---------------------------------------------------------------------------
# 争议裁决
# ---------------------------------------------------------------------------


@router.get(
    "/batches/{batch_id}/events",
    response_model=list[ExternalEventOut],
)
def list_batch_events(batch_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        xsvc.get_batch_or_404(db, batch_id)
    except xsvc.ExternalEventNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [
        xsvc.external_event_plain(r)
        for r in xsvc.list_external_events_plain(db, batch_id=batch_id)
    ]


@router.post(
    "/batches/{batch_id}/events/{external_event_id}/adjudicate",
    response_model=ExternalEventOut,
)
def adjudicate_event(
    batch_id: str,
    external_event_id: str,
    body: AdjudicationIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return xsvc.adjudicate(
            db,
            batch_id,
            external_event_id,
            decision=body.decision,
            adjudicator=body.adjudicator,
            note=body.note,
        )
    except xsvc.ExternalEventNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        xsvc.DisputeAlreadyResolvedError,
        xsvc.AdjudicationError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 来源解释
# ---------------------------------------------------------------------------


@router.get(
    "/plans/{plan_version}/students/{student_id}/provenance",
    response_model=ProvenanceOut,
)
def explain_provenance(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return xsvc.explain_provenance(db, plan_version, student_id)
    except xsvc.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
