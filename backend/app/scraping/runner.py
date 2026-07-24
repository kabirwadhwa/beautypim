import hashlib
import logging
import random
import re
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import CrawlJob, CrawlUrl, RawPageObservation, ScrapedProductObservation
from app.scraping import PARSER_VERSION
from app.scraping.adapters import adapter_for
from app.scraping.classification import classify_page
from app.scraping.discovery import discover_links, parse_sitemap
from app.scraping.fetcher import FetchBlocked, ResponseTooLarge, fetch
from app.scraping.persistence import persist_product
from app.scraping.schemas import CrawlConfiguration
from app.scraping.storage import LocalRawPageStorage
from app.scraping.url_safety import UnsafeUrl, normalize_url, path_is_irrelevant, validate_public_url

logger = logging.getLogger("app.scraping.runner")


def enqueue_url(db: Session, job: CrawlJob, url: str, depth=0, parent_id=None, priority=100) -> bool:
    config = CrawlConfiguration.model_validate(job.configuration)
    if job.pages_discovered >= config.maximum_discovered_urls:
        return False
    normalized = normalize_url(url)
    try:
        validate_public_url(normalized, expected_domain=config.domain, allow_subdomains=config.allow_subdomains)
    except UnsafeUrl:
        return False
    if depth > config.maximum_crawl_depth or path_is_irrelevant(normalized):
        return False
    first_segment = urlsplit(normalized).path.strip("/").split("/", 1)[0].lower()
    if config.locale and re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", first_segment):
        requested = config.locale.lower().replace("_", "-")
        if first_segment not in {requested, requested.split("-", 1)[0]}:
            return False
    if config.allowed_url_patterns and not any(re.search(pattern, normalized) for pattern in config.allowed_url_patterns):
        return False
    if any(re.search(pattern, normalized) for pattern in config.denied_url_patterns):
        return False
    if db.query(CrawlUrl.id).filter(CrawlUrl.crawl_job_id == job.id, CrawlUrl.normalized_url == normalized).first():
        return False
    nested = db.begin_nested()
    try:
        db.add(CrawlUrl(
            id=uuid.uuid4(), crawl_job_id=job.id, url=url, normalized_url=normalized,
            parent_url_id=parent_id, depth=depth, priority=priority, state="queued",
        ))
        db.flush()
        nested.commit()
    except IntegrityError:
        nested.rollback()
        return False
    job.pages_discovered += 1
    job.current_queue_size += 1
    return True


def seed_job(db: Session, job: CrawlJob):
    config = CrawlConfiguration.model_validate(job.configuration)
    urls = list(config.starting_urls)
    if config.sitemap_url:
        urls.append(config.sitemap_url)
    if not urls:
        urls.append(f"https://{config.domain}/")
    for url in urls:
        enqueue_url(db, job, url, priority=0)
    job.status = "discovering"
    db.commit()


def _robots(db: Session, job: CrawlJob, config: CrawlConfiguration):
    parser = RobotFileParser()
    scheme = urlsplit(config.starting_urls[0]).scheme if config.starting_urls else "https"
    robots_url = f"{scheme}://{config.domain}/robots.txt"
    try:
        result = fetch(robots_url, config)
        text = result.content.decode("utf-8", "replace") if result.status_code < 400 else ""
        parser.set_url(robots_url)
        parser.parse(text.splitlines())
        job.robots_txt = text
        job.robots_fetched_at = datetime.utcnow()
        if config.use_sitemap:
            declared = [
                line.split(":", 1)[1].strip() for line in text.splitlines()
                if line.lower().startswith("sitemap:") and ":" in line
            ]
            for url in declared:
                enqueue_url(db, job, url, priority=5)
            if not declared and job.crawl_mode in {"full_domain", "sitemap", "sitemap_index"}:
                enqueue_url(db, job, f"{scheme}://{config.domain}/sitemap.xml", priority=5)
        db.commit()
    except Exception as exc:
        logger.info("robots.txt unavailable for %s: %s", config.domain, exc)
        parser.parse([])
    return parser


def _claim_url(db: Session, job_id):
    query = db.query(CrawlUrl).filter(
        CrawlUrl.crawl_job_id == job_id, CrawlUrl.state == "queued",
        or_(CrawlUrl.next_attempt_at.is_(None), CrawlUrl.next_attempt_at <= datetime.utcnow()),
    ).order_by(CrawlUrl.priority, CrawlUrl.discovered_at)
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    item = query.first()
    if item:
        item.state = "fetching"
        item.attempts += 1
        db.commit()
    return item


def _previous_headers(db: Session, item: CrawlUrl):
    previous = db.query(RawPageObservation).filter(
        RawPageObservation.crawl_url_id == item.id,
    ).order_by(RawPageObservation.fetched_at.desc()).first()
    headers = {}
    if previous:
        if previous.etag:
            headers["If-None-Match"] = previous.etag
        if previous.last_modified:
            headers["If-Modified-Since"] = previous.last_modified
    return previous, headers


def run_crawl_job(db: Session, job_id):
    job = db.query(CrawlJob).filter(CrawlJob.id == job_id).first()
    if not job or job.status in {"cancelled", "completed"}:
        return
    config = CrawlConfiguration.model_validate(job.configuration)
    if job.pages_discovered == 0:
        seed_job(db, job)
    job.status = "crawling"
    job.started_at = job.started_at or datetime.utcnow()
    job.heartbeat_at = datetime.utcnow()
    db.commit()
    robots = _robots(db, job, config)
    started = time.monotonic()

    while True:
        db.refresh(job)
        if job.status in {"paused", "cancelled"}:
            return
        if time.monotonic() - started >= config.maximum_runtime_seconds:
            job.status = "partially_completed"
            job.error_summary = "Maximum crawl runtime reached; resume to continue."
            db.commit()
            return
        effective_page_limit = min(
            config.maximum_pages,
            config.browser_page_limit if config.use_browser_rendering else config.maximum_pages,
        )
        if job.pages_fetched >= effective_page_limit or job.product_pages_found >= config.maximum_product_pages:
            job.status = "partially_completed"
            job.error_summary = "Configured page or product limit reached."
            db.commit()
            return
        item = _claim_url(db, job.id)
        if not item:
            break
        job.current_queue_size = max(0, job.current_queue_size - 1)
        current_item_id = item.id
        if config.respect_robots_txt and not robots.can_fetch(config.user_agent, item.normalized_url):
            item.state, item.page_type, item.error_reason = "skipped", "blocked", "Disallowed by robots.txt"
            job.pages_skipped += 1
            db.commit()
            continue
        try:
            previous, conditional = _previous_headers(db, item)
            result = fetch(item.normalized_url, config, conditional)
            item.http_status, item.content_type = result.status_code, result.headers.get("content-type")
            item.fetched_at = datetime.utcnow()
            if result.status_code == 304 and previous:
                item.state, item.completed_at = "completed", datetime.utcnow()
                db.commit()
                continue
            content_type = result.headers.get("content-type", "").lower()
            is_xml = "xml" in content_type or result.content.lstrip().startswith(b"<?xml")
            html = None if is_xml else result.content.decode("utf-8", "replace")
            preclassified = classify_page(result.final_url, html) if html is not None else None
            content_hash = hashlib.sha256(result.content).hexdigest()
            storage_ref = None
            if is_xml or (preclassified and preclassified.page_type == "product"):
                storage_ref, content_hash = LocalRawPageStorage().put(
                    result.content, ".xml" if is_xml else ".html",
                )
            structured_matches = re.findall(
                rb'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                result.content, flags=re.IGNORECASE | re.DOTALL,
            )
            structured_hash = (
                hashlib.sha256(b"\n".join(structured_matches)).hexdigest()
                if structured_matches else None
            )
            raw = RawPageObservation(
                id=uuid.uuid4(), crawl_job_id=job.id, crawl_url_id=item.id,
                source_url=item.normalized_url, final_url=result.final_url,
                http_status=result.status_code, response_headers=result.headers,
                content_hash=content_hash, etag=result.headers.get("etag"),
                structured_data_hash=structured_hash,
                last_modified=result.headers.get("last-modified"),
                storage_reference=storage_ref, response_size=len(result.content),
                parser_version=PARSER_VERSION,
                unchanged_from_id=previous.id if previous and previous.content_hash == content_hash else None,
            )
            db.add(raw)
            db.flush()
            job.pages_fetched += 1
            if is_xml:
                kind, urls = parse_sitemap(result.content)
                item.page_type = "sitemap"
                for url in urls[:config.maximum_discovered_urls - job.pages_discovered]:
                    enqueue_url(db, job, url, item.depth + 1, item.id, 10 if kind == "sitemap_index" else 30)
            else:
                classification = preclassified
                item.page_type = classification.page_type
                item.classification_reasons = classification.reasons
                if classification.page_type == "product":
                    job.product_pages_found += 1
                    adapter = adapter_for(config.domain)
                    product = adapter.parse(html, result.final_url, country=config.country, locale=config.locale)
                    if product:
                        product.raw_payload_reference = storage_ref
                        job.products_parsed += 1
                        persist_product(db, job, raw, product, adapter)
                        job.products_persisted += 1
                    else:
                        job.products_failed += 1
                if classification.page_type in {"category", "pagination", "unknown"} or (
                    classification.page_type == "product" and config.use_category_discovery
                ):
                    remaining = config.maximum_discovered_urls - job.pages_discovered
                    for url in discover_links(html, result.final_url)[:max(0, remaining)]:
                        enqueue_url(db, job, url, item.depth + 1, item.id)
            item.state, item.completed_at = "completed", datetime.utcnow()
            job.consecutive_blocks = 0
            job.heartbeat_at = datetime.utcnow()
            db.commit()
            time.sleep(config.request_delay_seconds + random.uniform(0, min(0.5, config.request_delay_seconds / 4)))
        except FetchBlocked as exc:
            db.rollback()
            item = db.query(CrawlUrl).filter(CrawlUrl.id == current_item_id).first()
            job = db.query(CrawlJob).filter(CrawlJob.id == job_id).first()
            item.error_reason = str(exc)
            job.consecutive_blocks += 1
            if job.consecutive_blocks >= 3:
                item.state, item.page_type = "failed", "blocked"
                job.status, job.error_summary = "blocked", str(exc)
                db.commit()
                return
            _retry_or_fail(item, config, str(exc))
            db.commit()
        except Exception as exc:
            db.rollback()
            item = db.query(CrawlUrl).filter(CrawlUrl.id == current_item_id).first()
            job = db.query(CrawlJob).filter(CrawlJob.id == job_id).first()
            _retry_or_fail(item, config, str(exc))
            job.retry_count += 1
            job.error_summary = str(exc)
            db.commit()

    failed = db.query(CrawlUrl).filter(CrawlUrl.crawl_job_id == job.id, CrawlUrl.state == "failed").count()
    queued = db.query(CrawlUrl).filter(CrawlUrl.crawl_job_id == job.id, CrawlUrl.state == "queued").count()
    job.current_queue_size = queued
    if queued:
        job.status = "queued"
        job.completed_at = None
    else:
        job.completed_at = datetime.utcnow()
        job.status = "partially_completed" if failed else "completed"
    db.commit()


def _retry_or_fail(item, config, reason):
    item.error_reason = reason[:2000]
    if item.attempts <= config.retry_limit:
        item.state = "queued"
        item.next_attempt_at = datetime.utcnow() + timedelta(seconds=min(300, 2 ** item.attempts) + random.random())
    else:
        item.state = "failed"
        item.completed_at = datetime.utcnow()
