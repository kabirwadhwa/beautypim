"""Durable CLI importer for large internal reference workbooks."""
import argparse

from app.database import SessionLocal
from app.knowledge_corpus.adapters import RetailFeedAdapter, RichBeautyWorkbookAdapter
from app.knowledge_corpus.import_service import create_import_job, import_corpus


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--dataset", required=True, choices=["rich_beauty_reference", "retail_feed_1", "retail_feed_2"])
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    adapter = RichBeautyWorkbookAdapter() if args.dataset == "rich_beauty_reference" else RetailFeedAdapter(args.dataset)
    db = SessionLocal()
    try:
        job = create_import_job(db, args.path, adapter, args.dataset, "Retail Data")
        job = import_corpus(db, job, args.path, adapter, limit=args.limit)
        print({"job_id": str(job.id), "status": job.status, "metrics": job.metrics, "errors": job.error_summary})
    finally:
        db.close()


if __name__ == "__main__":
    main()
