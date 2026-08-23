"""Durable, non-blocking Improve Product research worker.

The OpenAI response ID is persisted inside CrawlJob.configuration before polling,
so a process restart resumes the same paid request instead of creating another.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime

from app.database import SessionLocal
from app.models import (
    CanonicalProduct, CrawlJob, ImportJob, ImportJobItem, ProductVariant, User,
)

logger = logging.getLogger("app.product_research_worker")
RESEARCH_DOMAIN = "product-research.internal"
ACTIVE_STATUSES = {"queued", "discovering", "crawling", "parsing"}


def _assign_configuration(job: CrawlJob, **updates) -> dict:
    configuration = {**(job.configuration or {}), **updates}
    job.configuration = configuration
    return configuration


def recover_product_research_jobs(db) -> int:
    jobs = db.query(CrawlJob).filter(
        CrawlJob.domain == RESEARCH_DOMAIN,
        CrawlJob.status.in_(["discovering", "crawling", "parsing"]),
    ).all()
    for job in jobs:
        job.status = "queued"
        job.error_summary = "Recovered the same background research request after restart."
    if jobs:
        db.commit()
    return len(jobs)


def _claim_job(db) -> CrawlJob | None:
    query = db.query(CrawlJob).filter(
        CrawlJob.domain == RESEARCH_DOMAIN,
        CrawlJob.status == "queued",
    ).order_by(CrawlJob.created_at)
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    job = query.first()
    if job:
        job.status = "discovering"
        job.started_at = job.started_at or datetime.utcnow()
        job.heartbeat_at = datetime.utcnow()
        job.error_summary = None
        db.commit()
        db.refresh(job)
    return job


def run_product_research_job(job_id: uuid.UUID, stop_event: threading.Event | None = None) -> None:
    from app.routes.products import _automatic_product_research, _product_expected_format
    from app.services.web_discovery import (
        poll_product_source_discovery, start_product_source_discovery,
    )
    from app.worker import process_item_enrichment

    db = SessionLocal()
    try:
        job = db.query(CrawlJob).filter(CrawlJob.id == job_id).first()
        if not job or job.domain != RESEARCH_DOMAIN or job.status == "cancelled":
            return
        configuration = job.configuration or {}
        product = db.query(CanonicalProduct).filter(
            CanonicalProduct.id == configuration.get("research_product_id"),
            CanonicalProduct.is_deleted == False,
        ).first()
        user = db.query(User).filter(User.id == job.requested_by_id).first()
        item = db.query(ImportJobItem).filter(
            ImportJobItem.id == configuration.get("research_item_id"),
        ).first()
        if not product or not user or not item:
            raise RuntimeError("The product, user or source record for background research no longer exists.")

        from app.services.product_identity import (
            preferred_product_variant, product_is_fragrance, trusted_product_version,
        )
        variant = preferred_product_variant(db, product.id)
        research_objectives = list(configuration.get("research_objectives") or [])
        safe_market_only = bool(product_is_fragrance(db, product) and not trusted_product_version(db, product))
        if safe_market_only:
            # Continue useful product-family research while preventing the
            # unresolved concentration from authorizing formulation/claim data.
            safe = {"consumer_brand", "product_family", "category", "product_type",
                    "image_url", "reviews", "rating", "review_count", "review_summary"}
            research_objectives = [field for field in research_objectives if field in safe]
            for field in ("image_url", "reviews"):
                if field not in research_objectives:
                    research_objectives.append(field)
        discovery = configuration.get("discovery")
        if not discovery:
            discovery = start_product_source_discovery(
                brand=product.brand.name if product.brand else "",
                product_name=product.product_name,
                product_format=_product_expected_format(db, product),
                gtin=variant.gtin if variant and variant.gtin else "",
                approved_domains=[],
                research_objectives=research_objectives,
            )
            _assign_configuration(job, discovery=discovery)
            job.heartbeat_at = datetime.utcnow()
            db.commit()

        while discovery.get("status") in {"queued", "in_progress"}:
            if stop_event and stop_event.is_set():
                # Leave the persisted provider response ID available for startup recovery.
                return
            discovery = poll_product_source_discovery(discovery)
            _assign_configuration(job, discovery=discovery)
            job.heartbeat_at = datetime.utcnow()
            db.commit()
            if discovery.get("status") in {"queued", "in_progress"}:
                time.sleep(2)

        job.status = "crawling"
        job.heartbeat_at = datetime.utcnow()
        db.commit()
        result = _automatic_product_research(
            db, product, user, candidates=discovery.get("candidates") or [],
            research_objectives=research_objectives,
        )

        # Re-run Product Understanding after phase-one evidence. The next plan
        # is calculated from the newly persisted evidence, never from the old
        # flat objective list.
        import_job = db.query(ImportJob).filter(ImportJob.id == item.import_job_id).first()
        if import_job:
            process_item_enrichment(
                db, item, import_job.column_mapping or {},
                mode=configuration.get("requested_mode") or "missing_only",
                selected_fields=configuration.get("selected_fields") or [],
            )
        from app.services.product_improvement import product_improvement_summary
        next_plan = product_improvement_summary(db, product)
        result["identity_phase_completed"] = configuration.get("research_phase") == "identity_resolution"
        result["next_phase"] = next_plan.get("research_phase")
        if next_plan.get("research_phase") == "attribute_completion":
            attribute_objectives = [entry["field"] for entry in next_plan.get("research_objectives") or []]
            if attribute_objectives and configuration.get("research_phase") == "identity_resolution":
                attribute_result = _automatic_product_research(
                    db, product, user, candidates=discovery.get("candidates") or [],
                    research_objectives=attribute_objectives,
                )
                result["attribute_completion"] = attribute_result
                if import_job:
                    process_item_enrichment(
                        db, item, import_job.column_mapping or {},
                        mode=configuration.get("requested_mode") or "missing_only",
                        selected_fields=configuration.get("selected_fields") or [],
                    )
            # Review synthesis is downstream product intelligence and therefore
            # cannot run during an unresolved identity phase.
            try:
                from app.services.review_summarization import summarize_product_reviews
                review_summary = summarize_product_reviews(db, product.id)
                result["review_summary_generated"] = bool(review_summary)
            except Exception as exc:
                logger.warning("Review synthesis failed for %s: %s", product.id, exc)
                result["review_summary_generated"] = False
                result["review_summary_error"] = str(exc)
        else:
            # Aggregate reviews and imagery are safe product-family market
            # observations even when an EDT/EDP distinction is unresolved.
            if result.get("review_evidence_found"):
                try:
                    from app.services.review_summarization import summarize_product_reviews
                    review_summary = summarize_product_reviews(db, product.id)
                    result["review_summary_generated"] = bool(review_summary)
                except Exception as exc:
                    logger.warning("Review synthesis failed for %s: %s", product.id, exc)
                    result["review_summary_generated"] = False
                    result["review_summary_error"] = str(exc)
            else:
                result["review_summary_generated"] = False
            result["identity_unresolved"] = True
        result["research_status"] = "completed"
        result["research_job_id"] = str(job.id)
        result["research_pending"] = False
        _assign_configuration(job, discovery=discovery, result=result)
        job.status = "completed" if not result.get("errors") else "partially_completed"
        job.completed_at = datetime.utcnow()
        job.heartbeat_at = job.completed_at
        job.error_summary = "\n".join(result.get("errors") or []) or None
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.query(CrawlJob).filter(CrawlJob.id == job_id).first()
        if job:
            job.status = "failed"
            job.completed_at = datetime.utcnow()
            job.heartbeat_at = job.completed_at
            job.error_summary = str(exc)
            _assign_configuration(job, result={
                "research_job_id": str(job.id), "research_status": "failed",
                "research_pending": False, "sources_ingested": 0,
                "errors": [str(exc)],
            })
            db.commit()
        logger.exception("Background product research failed for %s", job_id)
    finally:
        db.close()


def run_product_research_worker(stop_event: threading.Event) -> None:
    db = SessionLocal()
    try:
        recover_product_research_jobs(db)
    finally:
        db.close()
    while not stop_event.is_set():
        db = SessionLocal()
        try:
            job = _claim_job(db)
            job_id = job.id if job else None
        except Exception:
            logger.exception("Unable to claim a background product-research job")
            job_id = None
        finally:
            db.close()
        if job_id:
            run_product_research_job(job_id, stop_event)
        else:
            stop_event.wait(1.0)


def start_product_research_worker() -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_product_research_worker,
        args=(stop_event,),
        name="product-research-worker",
        daemon=True,
    )
    thread.start()
    return stop_event, thread
