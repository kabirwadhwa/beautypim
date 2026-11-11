"""Record and aggregate AI token consumption and its estimated cost.

Two responsibilities:

* ``record_ai_usage`` - one entry point every paid provider call uses. It must
  never be the reason a request fails: usage accounting is observability, so a
  failure to write it is logged and swallowed.
* the ``usage_*`` helpers - aggregate those rows for a product, an import job,
  a set of research jobs, or the whole deployment.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy import func as sa_func

from app.config import settings
from app.models import AiUsageEvent, CrawlJob, EnrichmentRun
from app.services.ai_pricing import estimate_cost, pricing_disclosure

logger = logging.getLogger(__name__)

VALID_OPERATIONS = {
    "enrichment", "web_research", "review_summary", "catalog_assistant", "other",
}


def response_elapsed_ms(response: Any) -> Optional[int]:
    """Milliseconds a provider call took, or None if that cannot be determined.

    Reading ``response.elapsed`` must never be able to fail a provider call:
    not every response object carries a real timedelta, so anything unexpected
    yields None instead of raising into the caller's error handling.
    """
    try:
        return int(response.elapsed.total_seconds() * 1000)
    except (AttributeError, TypeError, ValueError):
        return None


def record_ai_usage(
    db: Any,
    *,
    operation: str,
    provider: str,
    model: str,
    prompt_tokens: Optional[int] = 0,
    completion_tokens: Optional[int] = 0,
    cached_prompt_tokens: Optional[int] = 0,
    reasoning_tokens: Optional[int] = 0,
    web_search_calls: Optional[int] = 0,
    status: str = "success",
    canonical_product_id: Optional[uuid.UUID] = None,
    product_variant_id: Optional[uuid.UUID] = None,
    import_job_id: Optional[uuid.UUID] = None,
    research_job_id: Optional[uuid.UUID] = None,
    enrichment_run_id: Optional[uuid.UUID] = None,
    bulk_group_id: Optional[uuid.UUID] = None,
    requested_by_id: Optional[uuid.UUID] = None,
    processing_time_ms: Optional[int] = None,
    detail: Optional[dict[str, Any]] = None,
    commit: bool = True,
) -> Optional[AiUsageEvent]:
    """Persist one usage row. Returns the row, or None if it could not be saved.

    A failed provider call still consumed tokens, so pass what the provider
    reported along with ``status="failed"`` rather than skipping the row.
    """
    if operation not in VALID_OPERATIONS:
        operation = "other"

    prompt = max(0, int(prompt_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    cached = min(max(0, int(cached_prompt_tokens or 0)), prompt)
    reasoning = max(0, int(reasoning_tokens or 0))
    searches = max(0, int(web_search_calls or 0))

    cost, pricing_source = estimate_cost(
        model,
        prompt,
        completion,
        cached_prompt_tokens=cached,
        reasoning_tokens=reasoning,
        web_search_calls=searches,
        db=db,
    )

    event = AiUsageEvent(
        id=uuid.uuid4(),
        operation=operation,
        provider=provider or "unknown",
        model=model or "unknown",
        status="failed" if status == "failed" else "success",
        canonical_product_id=canonical_product_id,
        product_variant_id=product_variant_id,
        import_job_id=import_job_id,
        research_job_id=research_job_id,
        enrichment_run_id=enrichment_run_id,
        bulk_group_id=bulk_group_id,
        requested_by_id=requested_by_id,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_prompt_tokens=cached,
        reasoning_tokens=reasoning,
        total_tokens=prompt + completion + reasoning,
        web_search_calls=searches,
        estimated_cost_usd=cost,
        pricing_source=pricing_source,
        processing_time_ms=processing_time_ms,
        detail=detail or None,
    )
    try:
        db.add(event)
        if commit:
            db.commit()
        else:
            db.flush()
        return event
    except Exception as exc:
        # Never let accounting break the work it is accounting for.
        logger.warning("Could not record AI usage for %s/%s: %s", operation, model, exc)
        try:
            db.rollback()
        except Exception:  # pragma: no cover - defensive
            pass
        return None


def _empty_totals() -> dict[str, Any]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_prompt_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "web_search_calls": 0,
        "estimated_cost_usd": 0.0,
        "call_count": 0,
        "has_unpriced_calls": False,
        "is_estimate": True,
    }


def _aggregate(db: Any, *filters) -> dict[str, Any]:
    row = (
        db.query(
            sa_func.coalesce(sa_func.sum(AiUsageEvent.prompt_tokens), 0),
            sa_func.coalesce(sa_func.sum(AiUsageEvent.completion_tokens), 0),
            sa_func.coalesce(sa_func.sum(AiUsageEvent.cached_prompt_tokens), 0),
            sa_func.coalesce(sa_func.sum(AiUsageEvent.reasoning_tokens), 0),
            sa_func.coalesce(sa_func.sum(AiUsageEvent.total_tokens), 0),
            sa_func.coalesce(sa_func.sum(AiUsageEvent.web_search_calls), 0),
            sa_func.coalesce(sa_func.sum(AiUsageEvent.estimated_cost_usd), 0),
            sa_func.count(AiUsageEvent.id),
        )
        .filter(*filters)
        .first()
    )
    totals = _empty_totals()
    if not row:
        return totals
    totals.update(
        prompt_tokens=int(row[0] or 0),
        completion_tokens=int(row[1] or 0),
        cached_prompt_tokens=int(row[2] or 0),
        reasoning_tokens=int(row[3] or 0),
        total_tokens=int(row[4] or 0),
        web_search_calls=int(row[5] or 0),
        estimated_cost_usd=float(row[6] or 0.0),
        call_count=int(row[7] or 0),
    )
    # A call whose model has no known rate contributes tokens but no cost. Say
    # so, rather than presenting a total that quietly excludes it.
    unpriced = (
        db.query(sa_func.count(AiUsageEvent.id))
        .filter(*filters, AiUsageEvent.estimated_cost_usd.is_(None))
        .scalar()
    )
    totals["has_unpriced_calls"] = bool(unpriced)
    return totals


def _breakdown(db: Any, *filters) -> list[dict[str, Any]]:
    """Per-operation split, so a user can see what the money went on."""
    rows = (
        db.query(
            AiUsageEvent.operation,
            sa_func.coalesce(sa_func.sum(AiUsageEvent.total_tokens), 0),
            sa_func.coalesce(sa_func.sum(AiUsageEvent.estimated_cost_usd), 0),
            sa_func.count(AiUsageEvent.id),
        )
        .filter(*filters)
        .group_by(AiUsageEvent.operation)
        .all()
    )
    return [
        {
            "operation": row[0],
            "total_tokens": int(row[1] or 0),
            "estimated_cost_usd": float(row[2] or 0.0),
            "call_count": int(row[3] or 0),
        }
        for row in sorted(rows, key=lambda r: float(r[2] or 0.0), reverse=True)
    ]


def _models_used(db: Any, *filters) -> list[dict[str, Any]]:
    rows = (
        db.query(AiUsageEvent.model, AiUsageEvent.pricing_source)
        .filter(*filters)
        .group_by(AiUsageEvent.model, AiUsageEvent.pricing_source)
        .all()
    )
    out = []
    for model, source in rows:
        labels = {
            "override": "Configured rate",
            "reference": "Published reference rate",
            "calibrated": "Estimated from this deployment's historical runs",
            "unknown": "No rate available; cost not estimated",
        }
        out.append({
            "model": model,
            "pricing_source": source,
            "pricing_source_label": labels.get(source, source),
        })
    return out


def usage_for_product(db: Any, product_id: uuid.UUID, limit: int = 20) -> dict[str, Any]:
    """Totals and recent calls for one product."""
    scope = (AiUsageEvent.canonical_product_id == product_id,)
    recent = (
        db.query(AiUsageEvent)
        .filter(*scope)
        .order_by(AiUsageEvent.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "scope": "product",
        "scope_id": str(product_id),
        "totals": _aggregate(db, *scope),
        "by_operation": _breakdown(db, *scope),
        "models": _models_used(db, *scope),
        "recent_events": [
            {
                "id": str(event.id),
                "operation": event.operation,
                "provider": event.provider,
                "model": event.model,
                "status": event.status,
                "prompt_tokens": event.prompt_tokens,
                "completion_tokens": event.completion_tokens,
                "reasoning_tokens": event.reasoning_tokens,
                "total_tokens": event.total_tokens,
                "web_search_calls": event.web_search_calls,
                "estimated_cost_usd": (
                    float(event.estimated_cost_usd)
                    if event.estimated_cost_usd is not None
                    else None
                ),
                "pricing_source": event.pricing_source,
                "created_at": event.created_at,
            }
            for event in recent
        ],
    }


def usage_for_import_job(db: Any, job_id: uuid.UUID) -> dict[str, Any]:
    scope = (AiUsageEvent.import_job_id == job_id,)
    totals = _aggregate(db, *scope)
    products = (
        db.query(sa_func.count(sa_func.distinct(AiUsageEvent.canonical_product_id)))
        .filter(*scope)
        .scalar()
    ) or 0
    return {
        "scope": "import_job",
        "scope_id": str(job_id),
        "totals": totals,
        "by_operation": _breakdown(db, *scope),
        "models": _models_used(db, *scope),
        "products_touched": int(products),
        "cost_per_product_usd": (
            round(totals["estimated_cost_usd"] / products, 6) if products else None
        ),
        "budget": budget_status(totals["estimated_cost_usd"]),
    }


def usage_for_research_jobs(db: Any, job_ids: Iterable[uuid.UUID]) -> dict[str, Any]:
    """Totals across a set of research jobs, which is what Bulk Improve creates."""
    ids = [job_id for job_id in job_ids]
    if not ids:
        return {
            "scope": "research_jobs", "scope_id": None, "totals": _empty_totals(),
            "by_operation": [], "models": [], "products_touched": 0,
            "cost_per_product_usd": None, "budget": budget_status(0.0),
        }
    scope = (AiUsageEvent.research_job_id.in_(ids),)
    totals = _aggregate(db, *scope)
    products = (
        db.query(sa_func.count(sa_func.distinct(AiUsageEvent.canonical_product_id)))
        .filter(*scope)
        .scalar()
    ) or 0
    return {
        "scope": "research_jobs",
        "scope_id": None,
        "job_count": len(ids),
        "totals": totals,
        "by_operation": _breakdown(db, *scope),
        "models": _models_used(db, *scope),
        "products_touched": int(products),
        "cost_per_product_usd": (
            round(totals["estimated_cost_usd"] / products, 6) if products else None
        ),
        "budget": budget_status(totals["estimated_cost_usd"]),
    }


def usage_summary(db: Any, since: Any = None, until: Any = None) -> dict[str, Any]:
    """Deployment-wide totals, optionally bounded by a date range."""
    filters = []
    if since is not None:
        filters.append(AiUsageEvent.created_at >= since)
    if until is not None:
        filters.append(AiUsageEvent.created_at <= until)
    totals = _aggregate(db, *filters)
    model_rows = (
        db.query(
            AiUsageEvent.model,
            AiUsageEvent.pricing_source,
            sa_func.coalesce(sa_func.sum(AiUsageEvent.total_tokens), 0),
            sa_func.coalesce(sa_func.sum(AiUsageEvent.estimated_cost_usd), 0),
            sa_func.count(AiUsageEvent.id),
        )
        .filter(*filters)
        .group_by(AiUsageEvent.model, AiUsageEvent.pricing_source)
        .all()
    )
    return {
        "scope": "deployment",
        "totals": totals,
        "by_operation": _breakdown(db, *filters),
        "by_model": [
            {
                "model": row[0],
                "pricing_source": row[1],
                "total_tokens": int(row[2] or 0),
                "estimated_cost_usd": float(row[3] or 0.0),
                "call_count": int(row[4] or 0),
            }
            for row in sorted(model_rows, key=lambda r: float(r[3] or 0.0), reverse=True)
        ],
        "budget": budget_status(totals["estimated_cost_usd"]),
    }


def budget_status(spent_usd: float) -> dict[str, Any]:
    """Compare spend against MAX_JOB_COST_LIMIT."""
    limit = float(getattr(settings, "MAX_JOB_COST_LIMIT", 0) or 0)
    if limit <= 0:
        return {"limit_usd": None, "spent_usd": round(spent_usd, 6),
                "remaining_usd": None, "used_fraction": None, "exceeded": False}
    return {
        "limit_usd": limit,
        "spent_usd": round(spent_usd, 6),
        "remaining_usd": round(max(0.0, limit - spent_usd), 6),
        "used_fraction": round(spent_usd / limit, 4),
        "exceeded": spent_usd >= limit,
    }


def job_spend_usd(
    db: Any,
    *,
    import_job_id: Optional[uuid.UUID] = None,
    research_job_id: Optional[uuid.UUID] = None,
    bulk_group_id: Optional[uuid.UUID] = None,
) -> float:
    """Spend so far for one job scope. Used to enforce the budget mid-run."""
    filters = []
    if import_job_id is not None:
        filters.append(AiUsageEvent.import_job_id == import_job_id)
    if research_job_id is not None:
        filters.append(AiUsageEvent.research_job_id == research_job_id)
    if bulk_group_id is not None:
        filters.append(AiUsageEvent.bulk_group_id == bulk_group_id)
    if not filters:
        return 0.0
    total = (
        db.query(sa_func.coalesce(sa_func.sum(AiUsageEvent.estimated_cost_usd), 0))
        .filter(*filters)
        .scalar()
    )
    return float(total or 0.0)


def job_budget_exceeded(
    db: Any,
    *,
    import_job_id: Optional[uuid.UUID] = None,
    research_job_id: Optional[uuid.UUID] = None,
    bulk_group_id: Optional[uuid.UUID] = None,
) -> tuple[bool, float, Optional[float]]:
    """Return ``(exceeded, spent, limit)`` for a job scope.

    With no limit configured, or a non-positive one, nothing is ever exceeded.
    """
    limit = float(getattr(settings, "MAX_JOB_COST_LIMIT", 0) or 0)
    spent = job_spend_usd(
        db,
        import_job_id=import_job_id,
        research_job_id=research_job_id,
        bulk_group_id=bulk_group_id,
    )
    if limit <= 0:
        return False, spent, None
    return spent >= limit, spent, limit


def backfill_usage_from_enrichment_runs(db: Any, batch_size: int = 500) -> int:
    """Create usage rows for historical ``EnrichmentRun`` records.

    Runs predate this table, and their tokens and cost are the only historical
    spend record that exists. Importing them means the first view of the
    feature is not an empty chart, and it gives calibration something to work
    from. Idempotent: a run already represented is skipped.
    """
    imported = 0
    existing = {
        row[0]
        for row in db.query(AiUsageEvent.enrichment_run_id)
        .filter(AiUsageEvent.enrichment_run_id.isnot(None))
        .all()
    }
    query = (
        db.query(EnrichmentRun)
        .order_by(EnrichmentRun.created_at.asc())
        .yield_per(batch_size)
    )
    for run in query:
        if run.id in existing:
            continue
        prompt = int(run.prompt_tokens or 0)
        completion = int(run.completion_tokens or 0)
        if prompt == 0 and completion == 0 and run.estimated_cost is None:
            continue
        cost = (
            Decimal(str(run.estimated_cost))
            if run.estimated_cost is not None
            else None
        )
        event = AiUsageEvent(
            id=uuid.uuid4(),
            operation="enrichment",
            provider=run.provider or "unknown",
            model=run.model or "unknown",
            status="failed" if run.status == "failed" else "success",
            canonical_product_id=run.canonical_product_id,
            product_variant_id=run.product_variant_id,
            import_job_id=run.import_job_id,
            enrichment_run_id=run.id,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cached_prompt_tokens=0,
            reasoning_tokens=0,
            total_tokens=prompt + completion,
            web_search_calls=0,
            estimated_cost_usd=cost,
            # The figure is whatever was recorded at the time, not a rate we
            # resolved now, so it is attributed to its historical origin.
            pricing_source="reference" if cost is not None else "unknown",
            processing_time_ms=run.processing_time_ms,
            detail={"backfilled_from_enrichment_run": True},
            created_at=run.created_at,
        )
        db.add(event)
        imported += 1
        if imported % batch_size == 0:
            db.commit()
    db.commit()
    return imported


def usage_pricing_disclosure(db: Any) -> list[dict[str, Any]]:
    """How each currently configured model is being priced."""
    models = [
        settings.OPENAI_MODEL,
        settings.OPENAI_WEB_SEARCH_MODEL,
        settings.OPENAI_WEB_SEARCH_FALLBACK_MODEL,
        settings.GEMINI_MODEL,
    ]
    seen: set[str] = set()
    out = []
    for model in models:
        key = str(model or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(pricing_disclosure(model, db=db))
    return out
