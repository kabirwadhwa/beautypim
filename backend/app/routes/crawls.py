from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.auth import get_current_user, log_audit_event, require_editor_or_admin, require_viewer_or_above
from app.database import get_db
from app.limiter import rate_limit
from app.models import (
    CrawlConflict, CrawlJob, CrawlUrl, FieldValue, ScrapedProductObservation, User,
    ValidationIssue,
)
from app.scraping.schemas import CrawlConfiguration
from app.scraping.url_safety import UnsafeUrl, validate_public_url
from app.scraping.worker import prepare_recrawl

router = APIRouter(prefix="/crawl-jobs", tags=["Knowledge Crawl"])


def _job(db, job_id):
    value = db.query(CrawlJob).filter(CrawlJob.id == job_id).first()
    if not value:
        raise HTTPException(404, "Crawl job not found")
    return value


def _serialize(job):
    return {
        "id": str(job.id), "domain": job.domain, "starting_urls": job.starting_urls,
        "crawl_mode": job.crawl_mode, "status": job.status,
        "configuration": job.configuration, "created_at": job.created_at,
        "started_at": job.started_at, "completed_at": job.completed_at,
        "pages_discovered": job.pages_discovered, "pages_fetched": job.pages_fetched,
        "product_pages_found": job.product_pages_found,
        "products_parsed": job.products_parsed, "products_persisted": job.products_persisted,
        "products_failed": job.products_failed, "pages_skipped": job.pages_skipped,
        "current_queue_size": job.current_queue_size, "retry_count": job.retry_count,
        "error_summary": job.error_summary, "crawler_version": job.crawler_version,
    }


@router.post("/validate")
def validate_configuration(
    config: CrawlConfiguration,
    user: User = Depends(require_editor_or_admin),
):
    if config.crawl_mode == "full_domain" and user.role != "admin":
        raise HTTPException(403, "Full-domain crawls require an administrator")
    urls = [*config.starting_urls, *([config.sitemap_url] if config.sitemap_url else [])]
    if not urls and config.crawl_mode != "full_domain":
        raise HTTPException(422, "At least one starting or sitemap URL is required")
    try:
        for url in urls or [f"https://{config.domain}/"]:
            validate_public_url(url, expected_domain=config.domain, allow_subdomains=config.allow_subdomains)
    except UnsafeUrl as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"valid": True, "normalized_configuration": config.model_dump(mode="json")}


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(rate_limit("crawl_create", "RATE_LIMIT_CRAWL_CREATE"))])
def create_job(
    config: CrawlConfiguration,
    db: Session = Depends(get_db),
    user: User = Depends(require_editor_or_admin),
):
    validate_configuration(config, user)
    job = CrawlJob(
        id=uuid.uuid4(), domain=config.domain, starting_urls=config.starting_urls,
        crawl_mode=config.crawl_mode, status="queued",
        configuration=config.model_dump(mode="json"), requested_by_id=user.id,
    )
    db.add(job)
    log_audit_event(
        db, "CrawlJob", job.id, config.domain, "create",
        after={"crawl_mode": config.crawl_mode, "configuration": job.configuration},
        changed={"status": "queued"}, user_id=user.id,
    )
    db.commit()
    db.refresh(job)
    return _serialize(job)


@router.get("")
def list_jobs(db: Session = Depends(get_db), _: User = Depends(require_viewer_or_above)):
    return [_serialize(job) for job in db.query(CrawlJob).filter(
        CrawlJob.domain != "product-research.internal",
    ).order_by(CrawlJob.created_at.desc()).limit(100)]


@router.get("/{job_id}")
def job_status(job_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(require_viewer_or_above)):
    return _serialize(_job(db, job_id))


def _transition(db, job, target, allowed, user):
    if job.status not in allowed:
        raise HTTPException(409, f"Cannot change a {job.status} crawl to {target}")
    before = job.status
    job.status = target
    if target == "queued":
        job.completed_at = None
    log_audit_event(
        db, "CrawlJob", job.id, job.domain, "update",
        before={"status": before}, after={"status": target},
        changed={"status": {"before": before, "after": target}},
        user_id=user.id,
    )
    db.commit()
    return _serialize(job)


@router.post("/{job_id}/start")
def start(job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_editor_or_admin)):
    return _transition(db, _job(db, job_id), "queued", {"paused", "partially_completed", "failed", "blocked"}, user)


@router.post("/{job_id}/pause")
def pause(job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_editor_or_admin)):
    return _transition(db, _job(db, job_id), "paused", {"queued", "discovering", "crawling", "parsing"}, user)


@router.post("/{job_id}/resume")
def resume(job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_editor_or_admin)):
    return _transition(db, _job(db, job_id), "queued", {"paused", "partially_completed", "failed", "blocked"}, user)


@router.post("/{job_id}/cancel")
def cancel(job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_editor_or_admin)):
    return _transition(db, _job(db, job_id), "cancelled", {"queued", "discovering", "crawling", "parsing", "paused"}, user)


@router.get("/{job_id}/urls")
def urls(
    job_id: uuid.UUID, state: Optional[str] = None, page_type: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db),
    _: User = Depends(require_viewer_or_above),
):
    _job(db, job_id)
    query = db.query(CrawlUrl).filter(CrawlUrl.crawl_job_id == job_id)
    if state:
        query = query.filter(CrawlUrl.state == state)
    if page_type:
        query = query.filter(CrawlUrl.page_type == page_type)
    return [{
        "id": str(row.id), "url": row.normalized_url, "state": row.state,
        "page_type": row.page_type, "depth": row.depth, "attempts": row.attempts,
        "http_status": row.http_status, "error_reason": row.error_reason,
        "classification_reasons": row.classification_reasons,
    } for row in query.order_by(CrawlUrl.discovered_at.desc()).limit(limit)]


@router.post("/{job_id}/retry-failed")
def retry_failed(job_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(require_editor_or_admin)):
    job = _job(db, job_id)
    count = db.query(CrawlUrl).filter(
        CrawlUrl.crawl_job_id == job.id, CrawlUrl.state == "failed",
    ).update({"state": "queued", "next_attempt_at": None, "error_reason": None})
    job.current_queue_size += count
    job.status = "queued"
    db.commit()
    return {"retried": count, "job": _serialize(job)}


@router.post("/{job_id}/recrawl")
def recrawl(
    job_id: uuid.UUID, strategy: Optional[str] = None,
    db: Session = Depends(get_db), user: User = Depends(require_editor_or_admin),
):
    job = _job(db, job_id)
    selected = strategy or (job.configuration or {}).get("recrawl_strategy", "product_pages_only")
    if selected == "crawl_once":
        selected = "product_pages_only"
    if selected not in {
        "product_pages_only", "rediscover_catalogue", "refresh_stale",
        "prices_and_availability",
    }:
        raise HTTPException(422, "Unsupported recrawl strategy")
    prepare_recrawl(db, job, selected)
    log_audit_event(
        db, "CrawlJob", job.id, job.domain, "update",
        before={"status": "completed"}, after={"status": "queued", "recrawl_strategy": selected},
        changed={"recrawl": selected}, user_id=user.id,
    )
    db.commit()
    return _serialize(job)


@router.get("/{job_id}/products")
def products(job_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(require_viewer_or_above)):
    _job(db, job_id)
    rows = db.query(ScrapedProductObservation).filter(
        ScrapedProductObservation.crawl_job_id == job_id,
    ).order_by(ScrapedProductObservation.scraped_at.desc()).limit(200)
    return [{
        "id": str(row.id), "canonical_product_id": str(row.canonical_product_id) if row.canonical_product_id else None,
        "possible_match_product_id": str(row.possible_match_product_id) if row.possible_match_product_id else None,
        "source_url": row.source_url, "source_domain": row.source_domain,
        "match_status": row.match_status, "scraped_at": row.scraped_at,
        "product": row.normalized_payload,
    } for row in rows]


@router.post("/observations/{observation_id}/match/{decision}")
def review_possible_match(
    observation_id: uuid.UUID, decision: str,
    db: Session = Depends(get_db), user: User = Depends(require_editor_or_admin),
):
    if decision not in {"accept", "reject"}:
        raise HTTPException(422, "Decision must be accept or reject")
    observation = db.query(ScrapedProductObservation).filter(
        ScrapedProductObservation.id == observation_id,
        ScrapedProductObservation.match_status == "possible_match",
    ).first()
    if not observation or not observation.canonical_product_id:
        raise HTTPException(404, "Possible match observation not found")
    draft_product_id = observation.canonical_product_id
    candidate = observation.possible_match_product_id
    if decision == "accept":
        if not candidate:
            raise HTTPException(409, "No candidate product is recorded")
        from app.services.deduplication import merge_canonical_products
        merge_canonical_products(
            db, observation.canonical_product_id, candidate, user.id,
            "Accepted crawler possible-match review",
        )
        observation.canonical_product_id = candidate
        if observation.source_listing_id:
            from app.models import SourceListing
            listing = db.query(SourceListing).filter(
                SourceListing.id == observation.source_listing_id,
            ).first()
            if listing:
                listing.canonical_product_id = candidate
        observation.match_status = "matched"
    else:
        observation.match_status = "unmatched"
    observation.possible_match_product_id = None
    db.query(ValidationIssue).filter(
        ValidationIssue.canonical_product_id == draft_product_id,
        ValidationIssue.issue_type == "possible_crawl_product_match",
        ValidationIssue.resolved.is_(False),
    ).update({
        "resolved": True, "resolved_by_id": user.id,
        "resolved_at": datetime.utcnow(),
        "resolution_note": f"Possible match {decision}ed.",
    })
    db.commit()
    return {"id": str(observation.id), "match_status": observation.match_status}


@router.get("/{job_id}/conflicts")
def conflicts(job_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(require_viewer_or_above)):
    _job(db, job_id)
    rows = db.query(CrawlConflict).filter(CrawlConflict.crawl_job_id == job_id).order_by(CrawlConflict.created_at.desc())
    return [{
        "id": str(row.id), "canonical_product_id": str(row.canonical_product_id),
        "field_name": row.field_name, "current_value": row.current_value,
        "observed_value": row.observed_value, "status": row.status,
        "created_at": row.created_at,
    } for row in rows]


@router.post("/conflicts/{conflict_id}/{decision}")
def review_conflict(
    conflict_id: uuid.UUID, decision: str, note: Optional[str] = None,
    db: Session = Depends(get_db), user: User = Depends(require_editor_or_admin),
):
    if decision not in {"accept", "reject"}:
        raise HTTPException(422, "Decision must be accept or reject")
    conflict = db.query(CrawlConflict).filter(CrawlConflict.id == conflict_id).first()
    if not conflict or conflict.status != "pending":
        raise HTTPException(404, "Pending conflict not found")
    if decision == "accept":
        db.query(FieldValue).filter(
            FieldValue.canonical_product_id == conflict.canonical_product_id,
            FieldValue.field_name == conflict.field_name, FieldValue.is_current.is_(True),
        ).update({"is_current": False})
        db.add(FieldValue(
            id=uuid.uuid4(), canonical_product_id=conflict.canonical_product_id,
            field_name=conflict.field_name, value=conflict.observed_value,
            source_type="source_data", source_reference=f"crawl-observation:{conflict.scraped_product_id}",
            confidence_score=1, review_status="confirmed", reviewer_id=user.id,
            is_current=True, override_reason=note,
            evidence={"scraped_product_observation_id": str(conflict.scraped_product_id)},
        ))
    conflict.status = "accepted" if decision == "accept" else "rejected"
    conflict.reviewed_by_id, conflict.reviewed_at, conflict.review_note = user.id, datetime.utcnow(), note
    db.query(ValidationIssue).filter(
        ValidationIssue.canonical_product_id == conflict.canonical_product_id,
        ValidationIssue.field_name == conflict.field_name,
        ValidationIssue.issue_type == "scraped_value_conflict",
        ValidationIssue.resolved.is_(False),
    ).update({
        "resolved": True, "resolved_by_id": user.id,
        "resolved_at": datetime.utcnow(),
        "resolution_note": f"Crawl observation {decision}ed. {note or ''}".strip(),
    })
    log_audit_event(
        db, "CrawlConflict", conflict.id, conflict.field_name,
        "approve" if decision == "accept" else "reject",
        before={"status": "pending", "current_value": conflict.current_value},
        after={"status": conflict.status, "observed_value": conflict.observed_value},
        changed={"status": conflict.status}, user_id=user.id,
    )
    db.commit()
    return {"id": str(conflict.id), "status": conflict.status}
