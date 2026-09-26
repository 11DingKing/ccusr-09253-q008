"""跨校交换 API 路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from . import service
from .schemas import (
    ArbitrationIn,
    ExchangeBatchIn,
    ExchangeBatchOut,
    ExchangeEventOut,
    InstitutionIn,
    InstitutionOut,
    ReconcileOut,
    RecoverOut,
    RuleIn,
    RuleOut,
    SourceExplanationOut,
)

router = APIRouter(prefix="/api/exchange", tags=["exchange"])


def _raise(exc: service.ExchangeError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": str(exc)})


@router.post("/institutions", response_model=InstitutionOut, status_code=201)
def post_institution(body: InstitutionIn, db: Session = Depends(get_db)) -> Any:
    return service.register_institution(db, **body.model_dump())


@router.get("/institutions", response_model=list[InstitutionOut])
def get_institutions(db: Session = Depends(get_db)) -> Any:
    return service.list_institutions(db)


@router.post("/rules", response_model=RuleOut, status_code=201)
def post_rule(body: RuleIn, db: Session = Depends(get_db)) -> Any:
    try:
        return service.create_rule(db, **body.model_dump())
    except service.ExchangeError as exc:
        raise _raise(exc) from exc


@router.post("/rules/{rule_code}/versions/{version}/publish", response_model=RuleOut)
def post_publish_rule(rule_code: str, version: int, db: Session = Depends(get_db)) -> Any:
    try:
        return service.publish_rule(db, rule_code, version)
    except service.ExchangeError as exc:
        raise _raise(exc) from exc


@router.get("/rules", response_model=list[RuleOut])
def get_rules(rule_code: str | None = None, db: Session = Depends(get_db)) -> Any:
    return service.list_rules(db, rule_code=rule_code)


@router.post("/batches", response_model=ExchangeBatchOut, status_code=202)
def post_batch(body: ExchangeBatchIn, db: Session = Depends(get_db)) -> Any:
    """接收一个外校交换批次;若通道已追平则同步入账。"""
    try:
        return service.receive_batch(db, body.model_dump())
    except service.ExchangeError as exc:
        raise _raise(exc) from exc


@router.post("/recover", response_model=RecoverOut, status_code=200)
def post_recover(db: Session = Depends(get_db)) -> Any:
    """重启恢复:重新驱动所有停留在 RECEIVED 的批次。"""
    return service.recover_pending(db)


@router.get("/batches/{batch_id}", response_model=ExchangeBatchOut)
def get_batch(batch_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return service.get_batch(db, batch_id)
    except service.ExchangeError as exc:
        raise _raise(exc) from exc


@router.get("/batches", response_model=list[ExchangeBatchOut])
def list_batches(
    local_plan_version: str | None = None,
    partner_institution_code: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    return service.list_batches(
        db,
        local_plan_version=local_plan_version,
        partner_institution_code=partner_institution_code,
        status=status,
    )


@router.get("/events", response_model=list[ExchangeEventOut])
def list_exchange_events(
    local_plan_version: str | None = None,
    batch_id: str | None = None,
    student_id: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    return service.list_exchange_events(
        db,
        local_plan_version=local_plan_version,
        batch_id=batch_id,
        student_id=student_id,
        status=status,
    )


@router.get("/plans/{local_plan_version}/reconcile", response_model=ReconcileOut)
def get_reconcile(local_plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return service.reconcile(db, local_plan_version)
    except service.ExchangeError as exc:
        raise _raise(exc) from exc


@router.post("/arbitrations", response_model=ExchangeEventOut)
def post_arbitration(body: ArbitrationIn, db: Session = Depends(get_db)) -> Any:
    try:
        return service.arbitrate(db, **body.model_dump())
    except service.ExchangeError as exc:
        raise _raise(exc) from exc


@router.get(
    "/sources/{source_institution_code}/events/{source_event_id}",
    response_model=SourceExplanationOut,
)
def get_source_explanation(
    source_institution_code: str,
    source_event_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return service.explain_source(
            db,
            source_institution_code=source_institution_code,
            source_event_id=source_event_id,
        )
    except service.ExchangeError as exc:
        raise _raise(exc) from exc
