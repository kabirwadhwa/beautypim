import argparse
import json

from app.scraping.dataset_import import (
    analyze_retail_export, import_retail_export, import_summary,
)


def main():
    parser = argparse.ArgumentParser(
        description="Analyze or import a structured Retail Data product export."
    )
    parser.add_argument("file")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sample", type=int)
    parser.add_argument("--maximum-records", type=int)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--retain-raw-file", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        print(json.dumps(
            analyze_retail_export(args.file, sample_limit=args.sample),
            indent=2, ensure_ascii=False,
        ))
        return

    from app.database import SessionLocal
    db = SessionLocal()
    try:
        job = import_retail_export(
            db,
            args.file,
            batch_size=args.batch_size,
            maximum_records=args.maximum_records,
            retain_raw_file=args.retain_raw_file,
            force=args.force,
            progress_callback=lambda processed, failed: print(
                f"Imported {processed} products; {failed} failed", flush=True
            ),
        )
        print(json.dumps(import_summary(db, job), indent=2, ensure_ascii=False))
    finally:
        db.close()


if __name__ == "__main__":
    main()
