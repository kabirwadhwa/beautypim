"""Inventory or deterministically repair legacy ingredient records."""
import argparse
import json

from app.database import SessionLocal
from app.services.ingredient_backfill import repair_legacy_ingredient_state


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply safe repairs; default is dry-run")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        print(json.dumps(repair_legacy_ingredient_state(db, dry_run=not args.apply), indent=2, default=str))
    finally:
        db.close()


if __name__ == "__main__":
    main()
