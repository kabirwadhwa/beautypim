from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING
import uuid

import ijson
from app.scraping import CRAWLER_VERSION, PARSER_VERSION
from app.scraping.adapters.retail_data_dataset import Retail DataDatasetAdapter

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from app.models import CrawlJob


def file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def records(path: Path):
    with path.open("rb") as stream:
        yield from ijson.items(stream, "item")


def analyze_retail_data_export(path: Path, sample_limit: int | None = None) -> dict:
    path = Path(path).expanduser().resolve()
    adapter = Retail DataDatasetAdapter()
    imported_at = datetime.now(timezone.utc)
    total = 0
    invalid = 0
    identifiers = set()
    repeated_identifiers = 0
    coverage = Counter()
    source_systems = Counter()
    for record in records(path):
        total += 1
        product = adapter.parse_record(record, imported_at)
        if not product.product_name or not product.brand:
            invalid += 1
        if product.gtin:
            if product.gtin in identifiers:
                repeated_identifiers += 1
            identifiers.add(product.gtin)
        for field in (
            "brand", "product_name", "description", "gtin", "category_path",
            "size", "price", "availability", "image_urls",
            "ingredient_text_raw", "benefits", "usage_instructions",
            "warnings", "skin_types",
        ):
            if getattr(product, field) not in (None, "", []):
                coverage[field] += 1
        source_systems[str(record.get("source") or "unknown")] += 1
        if sample_limit and total >= sample_limit:
            break
    return {
        "records_analyzed": total,
        "invalid_brand_or_name": invalid,
        "unique_gtins": len(identifiers),
        "repeated_gtin_rows": repeated_identifiers,
        "coverage": {
            field: {"count": count, "percentage": round(count * 100 / total, 2)}
            for field, count in sorted(coverage.items())
        } if total else {},
        "source_systems": dict(source_systems),
    }


def import_retail_data_export(
    db: "Session",
    file_path: str,
    requested_by_id=None,
    batch_size: int = 100,
    maximum_records: int | None = None,
    retain_raw_file: bool = False,
    force: bool = False,
    progress_callback=None,
) -> "CrawlJob":
    from app.models import CrawlJob, CrawlUrl, RawPageObservation
    from app.scraping.persistence import persist_product
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    digest, file_size = file_digest(path)
    existing = db.query(CrawlJob).filter(
        CrawlJob.domain == "retail-data.invalid",
        CrawlJob.status.in_(["completed", "partially_completed"]),
    ).all()
    for job in existing:
        if (job.configuration or {}).get("dataset_sha256") == digest and not force:
            return job

    imported_at = datetime.now(timezone.utc)
    config = {
        "domain": "retail-data.invalid",
        "starting_urls": ["https://retail-data.invalid/"],
        "crawl_mode": "multiple_urls",
        "dataset_import": True,
        "dataset_filename": path.name,
        "dataset_sha256": digest,
        "dataset_size_bytes": file_size,
        "adapter": "retail_data_active_products_export",
        "country": "FR",
        "locale": "fr-FR",
    }
    job = CrawlJob(
        id=uuid.uuid4(),
        domain="retail-data.invalid",
        starting_urls=config["starting_urls"],
        crawl_mode="multiple_urls",
        status="parsing",
        configuration=config,
        requested_by_id=requested_by_id,
        started_at=imported_at,
        heartbeat_at=imported_at,
        crawler_version=CRAWLER_VERSION,
    )
    crawl_url = CrawlUrl(
        id=uuid.uuid4(),
        crawl_job_id=job.id,
        url="https://retail-data.invalid/",
        normalized_url="https://retail-data.invalid/",
        depth=0,
        state="completed",
        page_type="unknown",
        classification_reasons=["structured Retail Data product export"],
        completed_at=imported_at,
    )
    db.add_all([job, crawl_url])
    db.flush()
    storage_reference = str(path)
    if retain_raw_file:
        from app.scraping.storage import LocalRawPageStorage
        storage_reference, _, _ = LocalRawPageStorage().put_file(str(path))
    raw_page = RawPageObservation(
        id=uuid.uuid4(),
        crawl_job_id=job.id,
        crawl_url_id=crawl_url.id,
        source_url="https://retail-data.invalid/",
        final_url="https://retail-data.invalid/",
        http_status=200,
        response_headers={
            "content-type": "application/json",
            "x-beautypim-source-filename": path.name,
        },
        content_hash=digest,
        storage_reference=storage_reference,
        response_size=file_size,
        parser_version=PARSER_VERSION,
    )
    db.add(raw_page)
    db.commit()
    job_id = job.id
    raw_page_id = raw_page.id

    adapter = Retail DataDatasetAdapter()
    processed = 0
    failed = 0
    errors = []
    for record in records(path):
        if maximum_records is not None and processed + failed >= maximum_records:
            break
        try:
            # A malformed record must not roll back the rest of the current batch.
            with db.begin_nested():
                product = adapter.parse_record(record, imported_at)
                if not product.product_name or not product.brand:
                    raise ValueError("Record is missing its product name or brand")
                product.raw_payload_reference = (
                    f"{storage_reference}#record={record.get('_id') or processed + 1}"
                )
                persist_product(db, job, raw_page, product, adapter)
            processed += 1
            job.products_parsed = processed
            job.products_persisted = processed
            job.product_pages_found = processed
            job.pages_discovered = processed
            job.pages_fetched = processed
            job.heartbeat_at = datetime.now(timezone.utc)
            if processed % batch_size == 0:
                db.commit()
                if progress_callback:
                    progress_callback(processed, failed)
        except Exception as exc:
            failed += 1
            if len(errors) < 20:
                errors.append(
                    f"{record.get('_id', 'unknown')}: {type(exc).__name__}: {exc}"
                )
    job = db.query(CrawlJob).filter(CrawlJob.id == job_id).one()
    job.products_failed = failed
    job.completed_at = datetime.now(timezone.utc)
    job.heartbeat_at = job.completed_at
    job.status = "partially_completed" if failed else "completed"
    job.error_summary = "\n".join(errors) if errors else None
    db.commit()
    return job


def import_summary(db: "Session", job: "CrawlJob") -> dict:
    from app.models import ScrapedProductObservation
    observations = db.query(ScrapedProductObservation).filter(
        ScrapedProductObservation.crawl_job_id == job.id
    )
    return {
        "job_id": str(job.id),
        "status": job.status,
        "products_persisted": job.products_persisted,
        "products_failed": job.products_failed,
        "matched": observations.filter(
            ScrapedProductObservation.match_status == "matched"
        ).count(),
        "drafts": observations.filter(
            ScrapedProductObservation.match_status == "unmatched"
        ).count(),
        "possible_matches": observations.filter(
            ScrapedProductObservation.match_status == "possible_match"
        ).count(),
        "conflicts": observations.filter(
            ScrapedProductObservation.match_status == "conflict"
        ).count(),
        "error_summary": job.error_summary,
    }
