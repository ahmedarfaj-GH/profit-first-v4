import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import db
from app.auth import require_api_key
from app.allocation import compute_run
from app.engine.allocation_engine import validate_override
from app.validation import validate_period

router = APIRouter(tags=["runs"])

# The API key is platform-wide, so reads here span every organization
# (org_id=None). Per-organization keys are tracked in plan.md.
ALL_ORGS = None


def _amount() -> float:
    return Field(default=0, ge=0, le=1_000_000_000_000, allow_inf_nan=False)


class FinancialInputs(BaseModel):
    opening_cash_balance: float = _amount()
    total_collections: float = _amount()
    total_operating_expenses_paid: float = _amount()
    payroll_due: float = _amount()
    vat_due: float = _amount()
    royalty_due: float = _amount()
    suppliers_due: float = _amount()
    other_short_term_due: float = _amount()
    operational_reserve_target: float = _amount()


class SnapshotIn(BaseModel):
    period_start: str = Field(max_length=10)
    period_end: str = Field(max_length=10)
    inputs: FinancialInputs
    source_note: Optional[str] = Field(default=None, max_length=500)


class ReviewIn(BaseModel):
    decision: Literal["APPROVE", "MODIFY", "REJECT"]
    override: Optional[dict[str, float]] = None
    justification: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="Required (non-empty) when `override` touches a protected bucket.",
    )
    decided_by: str = Field(min_length=1, max_length=100)
    actual_outcome: str = Field(default="PENDING", max_length=50)


def _new_run_id(entity_id: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"RUN-{entity_id}-{ts}-{uuid.uuid4().hex[:6].upper()}"


@router.post("/entities/{entity_id}/snapshots", dependencies=[Depends(require_api_key)])
def submit_snapshot(entity_id: str, snapshot: SnapshotIn):
    entity = db.get_entity(entity_id, org_id=ALL_ORGS)
    if not entity:
        raise HTTPException(status_code=404, detail=f"Unknown entity_id: {entity_id}")

    try:
        validate_period(snapshot.period_start, snapshot.period_end)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    values = snapshot.inputs.model_dump()
    liquidity, allocation = compute_run(entity["org_id"], values)

    run_id = _new_run_id(entity_id)
    dataset_hash = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    db.insert_run(
        run_id, entity_id, snapshot.period_start, snapshot.period_end, dataset_hash,
        values, liquidity, allocation, allocation["policy_id"],
    )

    return {
        "run_id": run_id,
        "entity_id": entity_id,
        "period_start": snapshot.period_start,
        "period_end": snapshot.period_end,
        "dataset_hash": dataset_hash,
        "liquidity": liquidity,
        "allocation": allocation,
        "human_review_required": True,
    }


@router.get("/entities/{entity_id}/latest", dependencies=[Depends(require_api_key)])
def get_latest(entity_id: str):
    run = db.get_latest_run(entity_id, org_id=ALL_ORGS)
    if not run:
        raise HTTPException(status_code=404, detail="No runs found for this entity")
    return run


@router.get("/runs/{run_id}", dependencies=[Depends(require_api_key)])
def get_run(run_id: str):
    run = db.get_run(run_id, org_id=ALL_ORGS)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    review = db.get_review(run_id)
    if review:
        run["review"] = review
    return run


@router.post("/runs/{run_id}/review", dependencies=[Depends(require_api_key)])
def submit_review(run_id: str, review: ReviewIn):
    run = db.get_run(run_id, org_id=ALL_ORGS)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    touched: list[str] = []
    if review.override:
        try:
            touched = validate_override(review.override, run["allocation"]["allocations"], review.justification)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    if not db.save_review(
        run_id, review.decision, review.override, review.justification, review.decided_by, review.actual_outcome
    ):
        raise HTTPException(status_code=409, detail="Run already reviewed — the first decision is final")
    return {
        "status": "recorded",
        "run_id": run_id,
        "decision": review.decision,
        "protected_buckets_overridden": touched,
    }
