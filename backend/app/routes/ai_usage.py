"""Read-only endpoints exposing AI token consumption and its estimated cost.

Every figure here is an estimate. Token counts come from the provider and are
exact; the dollar amounts are derived from a configured or inferred rate, and
each response carries the ``pricing_source`` behind it so the interface can
say where the number came from rather than implying an invoice.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth import require_editor_or_admin, require_internal_viewer_or_above
from app.database import get_db
from app.models import CanonicalProduct, CrawlJob, ImportJob, User
from app.services.ai_usage import (
    backfill_usage_from_enrichment_runs,
    usage_for_import_job,
    usage_for_product,
    usage_for_research_jobs,
    usage_pricing_disclosure,
    usage_summary,
)

router = APIRouter(prefix="/ai-usage", tags=["AI Usage & Cost"])

ESTIMATE_NOTE = (
    "Token counts are reported by the AI provider. Costs are estimates derived "
    "from the configured rate for each model and will not match an invoice "
    "exactly."
)


def _decorate(payload: dict[str, Any]) -> dict[str, Any]:
    payload["estimate_note"] = ESTIMATE_NOTE
    return payload


@router.get("/summary")
def read_usage_summary(
    days: Optional[int] = Query(
        None, ge=1, le=365,
        description="Limit to the last N days. Omit for all recorded usage.",
    ),
    db: Session = Depends(get_db),
    _: User = Depends(require_internal_viewer_or_above),
):
    """Deployment-wide token and cost totals, split by operation and model."""
    since = (
        datetime.now(timezone.utc) - timedelta(days=days) if days else None
    )
    payload = usage_summary(db, since=since)
    payload["window_days"] = days
    payload["pricing"] = usage_pricing_disclosure(db)
    return _decorate(payload)


@router.get("/products/{product_id}")
def read_product_usage(
    product_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(require_internal_viewer_or_above),
):
    """What one product has cost so far, across enrichment and research."""
    exists = (
        db.query(CanonicalProduct.id)
        .filter(CanonicalProduct.id == product_id, CanonicalProduct.is_deleted == False)
        .first()
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Product not found")
    return _decorate(usage_for_product(db, product_id, limit=limit))


@router.get("/import-jobs/{job_id}")
def read_import_job_usage(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_internal_viewer_or_above),
):
    """What one import job consumed, with cost per product and budget state."""
    if not db.query(ImportJob.id).filter(ImportJob.id == job_id).first():
        raise HTTPException(status_code=404, detail="Import job not found")
    return _decorate(usage_for_import_job(db, job_id))


@router.get("/research-jobs/{job_id}")
def read_research_job_usage(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_internal_viewer_or_above),
):
    """What one research (Improve Product) job consumed."""
    if not db.query(CrawlJob.id).filter(CrawlJob.id == job_id).first():
        raise HTTPException(status_code=404, detail="Research job not found")
    return _decorate(usage_for_research_jobs(db, [job_id]))


@router.post("/research-jobs/aggregate")
def read_research_jobs_usage(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    _: User = Depends(require_internal_viewer_or_above),
):
    """Combined usage for a set of research jobs, as Bulk Improve creates."""
    raw = payload.get("research_job_ids") or []
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=422, detail="Provide at least one research job.")
    if len(raw) > 100:
        raise HTTPException(status_code=422, detail="Up to 100 research jobs per request.")
    try:
        job_ids = [uuid.UUID(str(value)) for value in raw]
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="research_job_ids must be UUIDs.")
    return _decorate(usage_for_research_jobs(db, job_ids))


@router.get("/pricing")
def read_pricing(
    db: Session = Depends(get_db),
    _: User = Depends(require_internal_viewer_or_above),
):
    """The rate and its provenance for every currently configured model."""
    return {
        "models": usage_pricing_disclosure(db),
        "estimate_note": ESTIMATE_NOTE,
        "override_env_var": "AI_MODEL_PRICING_JSON",
    }


@router.post("/backfill")
def backfill_usage(
    db: Session = Depends(get_db),
    _: User = Depends(require_editor_or_admin),
):
    """Import historical EnrichmentRun token/cost data into the usage table.

    Idempotent, so it is safe to run more than once. Enrichment runs predate
    this table and are the only historical spend record that exists.
    """
    imported = backfill_usage_from_enrichment_runs(db)
    return {
        "imported_events": imported,
        "message": (
            f"Imported {imported} historical enrichment run(s) into AI usage."
            if imported
            else "No new historical enrichment runs to import."
        ),
    }
