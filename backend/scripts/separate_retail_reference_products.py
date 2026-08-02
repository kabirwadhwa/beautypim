"""Reversibly hide canonical products created only by a retail reference import.

Raw observations, field-level provenance and normalized payloads remain intact.
Products that also have a normal feed import or non-reference crawl are protected.
"""
import argparse
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models import (
    CanonicalProduct, CrawlJob, ImportJobItem, ProductVariant,
    ScrapedProductObservation, SourceListing,
)


def reference_only_product_ids(db):
    retail_jobs = {
        row[0] for row in db.query(CrawlJob.id).filter(
            CrawlJob.domain == "retail-data.invalid"
        ).all()
    }
    observed = {
        row[0] for row in db.query(ScrapedProductObservation.canonical_product_id).filter(
            ScrapedProductObservation.crawl_job_id.in_(retail_jobs),
            ScrapedProductObservation.canonical_product_id.isnot(None),
        ).distinct().all()
    } if retail_jobs else set()
    feed_products = {
        row[0] for row in db.query(ImportJobItem.canonical_product_id).filter(
            ImportJobItem.canonical_product_id.in_(observed)
        ).distinct().all()
    } if observed else set()
    non_reference_sources = {
        row[0] for row in db.query(SourceListing.canonical_product_id).filter(
            SourceListing.canonical_product_id.in_(observed),
            (
                SourceListing.import_job_id.isnot(None)
                | ~SourceListing.crawl_job_id.in_(retail_jobs)
            ),
        ).distinct().all()
    } if observed else set()
    return observed - feed_products - non_reference_sources, observed, feed_products | non_reference_sources


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        candidates, observed, protected = reference_only_product_ids(db)
        print({
            "retail_linked_products": len(observed),
            "protected_user_products": len(protected),
            "reference_only_products": len(candidates),
            "apply": args.apply,
        })
        if not args.apply or not candidates:
            return
        now = datetime.now(timezone.utc)
        db.query(CanonicalProduct).filter(CanonicalProduct.id.in_(candidates)).update(
            {"is_deleted": True, "deleted_at": now}, synchronize_session=False
        )
        db.query(ProductVariant).filter(ProductVariant.canonical_product_id.in_(candidates)).update(
            {"is_deleted": True, "deleted_at": now}, synchronize_session=False
        )
        db.commit()
        print({"hidden_reference_only_products": len(candidates)})
    finally:
        db.close()


if __name__ == "__main__":
    main()
