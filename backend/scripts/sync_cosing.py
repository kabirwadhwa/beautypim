"""Synchronize the public European Commission CosIng ingredient glossary.

CosIng is an informative reference database, not a safety or legal determination.
Run from the backend directory: python scripts/sync_cosing.py --pages 10
"""

import argparse
import json
import os
import sys
import uuid

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal  # noqa: E402
from app.models import IngredientDefinition  # noqa: E402
from app.services.deduplication import normalize_text  # noqa: E402
from app.services.ingredient_knowledge import COSING_SOURCE_URL  # noqa: E402


SEARCH_URL = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
# Public key shipped by the European Commission CosIng web application.
PUBLIC_SEARCH_KEY = "285a77fd-1257-4271-8507-f0c6b2961203"


def first(metadata, key):
    values = metadata.get(key) or []
    return values[0] if values else None


def fetch_page(session, page_number, page_size):
    query = {
        "bool": {
            "must": [
                {"term": {"itemType": "ingredient"}},
                {"term": {"status": "Active"}},
            ]
        }
    }
    response = session.post(
        SEARCH_URL,
        params={
            "apiKey": PUBLIC_SEARCH_KEY,
            "text": "*",
            "pageSize": page_size,
            "pageNumber": page_number,
        },
        files={"query": ("query.json", json.dumps(query), "application/json")},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def upsert_page(db, payload):
    changed = 0
    for result in payload.get("results", []):
        metadata = result.get("metadata") or {}
        inci_name = first(metadata, "inciName")
        if not inci_name:
            continue
        normalized_name = normalize_text(inci_name)
        row = (
            db.query(IngredientDefinition)
            .filter(IngredientDefinition.normalized_name == normalized_name)
            .first()
        )
        if row is None:
            row = IngredientDefinition(
                id=uuid.uuid4(), name=inci_name, normalized_name=normalized_name
            )
            db.add(row)
        row.name = inci_name
        row.common_name = first(metadata, "nameOfCommonIngredientsGlossary")
        row.function = ", ".join(metadata.get("functionName") or []) or None
        row.possible_concerns = "; ".join(metadata.get("cosmeticRestriction") or []) or None
        row.source_name = "European Commission CosIng"
        row.source_url = COSING_SOURCE_URL
        row.source_record_id = first(metadata, "substanceId") or result.get("reference")
        row.regulatory_status = first(metadata, "status")
        row.cas_number = first(metadata, "casNo")
        row.ec_number = first(metadata, "ecNo")
        changed += 1
    db.commit()
    return changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=1, help="Pages to sync; 0 means all")
    parser.add_argument("--page-size", type=int, default=100)
    args = parser.parse_args()
    if args.page_size < 1 or args.page_size > 100:
        parser.error("--page-size must be between 1 and 100")

    http = requests.Session()
    db = SessionLocal()
    total_changed = 0
    try:
        page_number = 1
        while True:
            payload = fetch_page(http, page_number, args.page_size)
            total_changed += upsert_page(db, payload)
            print(f"Synced page {page_number}; {total_changed} ingredient records processed")
            if page_number * args.page_size >= payload.get("totalResults", 0):
                break
            if args.pages and page_number >= args.pages:
                break
            page_number += 1
    finally:
        db.close()


if __name__ == "__main__":
    main()
