import logging
import time
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.database import SessionLocal
from app.models import CrawlJob, CrawlUrl
from app.scraping.runner import run_crawl_job
from sqlalchemy import text

logger = logging.getLogger("app.scraping.worker")


def recover_stale_jobs(db):
    cutoff = datetime.utcnow() - timedelta(seconds=settings.CRAWL_WORKER_STALE_SECONDS)
    jobs = db.query(CrawlJob).filter(
        CrawlJob.domain != "product-research.internal",
        CrawlJob.status.in_(["discovering", "crawling", "parsing"]),
        CrawlJob.heartbeat_at < cutoff,
    ).all()
    for job in jobs:
        db.query(CrawlUrl).filter(
            CrawlUrl.crawl_job_id == job.id, CrawlUrl.state == "fetching",
        ).update({"state": "queued"})
        job.status = "queued"
        job.error_summary = "Recovered after worker interruption."
    if jobs:
        db.commit()


def claim_job(db):
    active_domains = db.query(CrawlJob.domain).filter(
        CrawlJob.status.in_(["discovering", "crawling", "parsing"]),
    )
    query = db.query(CrawlJob).filter(
        CrawlJob.domain != "product-research.internal",
        CrawlJob.status == "queued",
        ~CrawlJob.domain.in_(active_domains),
    ).order_by(CrawlJob.created_at)
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    job = query.first()
    if job:
        if db.bind.dialect.name == "postgresql":
            locked = db.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:domain))"),
                {"domain": job.domain},
            ).scalar()
            if not locked:
                db.rollback()
                return None
        job.status = "discovering"
        job.heartbeat_at = datetime.utcnow()
        db.commit()
    return job


def schedule_due_recrawls(db):
    now = datetime.now(timezone.utc)
    jobs = db.query(CrawlJob).filter(
        CrawlJob.status.in_(["completed", "partially_completed"]),
        CrawlJob.completed_at.isnot(None),
    ).all()
    for job in jobs:
        interval = (job.configuration or {}).get("rescrape_interval_hours")
        strategy = (job.configuration or {}).get("recrawl_strategy", "crawl_once")
        if not interval or strategy == "crawl_once":
            continue
        completed_at = job.completed_at
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=timezone.utc)
        if completed_at > now - timedelta(hours=int(interval)):
            continue
        prepare_recrawl(db, job, strategy)
    db.commit()


def prepare_recrawl(db, job, strategy):
    query = db.query(CrawlUrl).filter(CrawlUrl.crawl_job_id == job.id)
    if strategy in {"product_pages_only", "prices_and_availability"}:
        query = query.filter(CrawlUrl.page_type == "product")
    query.update({
        "state": "queued", "next_attempt_at": None, "error_reason": None,
        "completed_at": None,
    })
    queued = db.query(CrawlUrl).filter(
        CrawlUrl.crawl_job_id == job.id, CrawlUrl.state == "queued",
    ).count()
    job.status = "queued"
    job.current_queue_size = queued
    job.completed_at = None
    job.error_summary = None
    job.pages_fetched = 0
    job.product_pages_found = 0
    job.products_parsed = 0
    job.products_persisted = 0
    job.products_failed = 0
    job.pages_skipped = 0
    job.retry_count = 0


def run_forever():
    logging.basicConfig(level=settings.LOG_LEVEL)
    while True:
        db = SessionLocal()
        try:
            recover_stale_jobs(db)
            schedule_due_recrawls(db)
            job = claim_job(db)
            if job:
                try:
                    run_crawl_job(db, job.id)
                finally:
                    if db.bind.dialect.name == "postgresql":
                        db.execute(
                            text("SELECT pg_advisory_unlock(hashtext(:domain))"),
                            {"domain": job.domain},
                        )
            else:
                time.sleep(settings.CRAWL_WORKER_POLL_SECONDS)
        except Exception:
            logger.exception("Crawler worker iteration failed")
            time.sleep(settings.CRAWL_WORKER_POLL_SECONDS)
        finally:
            db.close()


if __name__ == "__main__":
    run_forever()
