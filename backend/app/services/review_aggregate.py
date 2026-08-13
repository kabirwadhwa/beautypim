"""Single canonical selector for exact-product review evidence.

Every presentation and synthesis surface consumes this result; none may pick
an arbitrary first observation independently.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.models import ImportJob, ScrapedProductObservation, SourceListing


def _integer(value: Any) -> int:
    try:
        return int(float(str(value or 0).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def select_review_aggregate(db, product_id) -> dict[str, Any] | None:
    rows = db.query(ScrapedProductObservation).filter(
        ScrapedProductObservation.canonical_product_id == product_id,
        ScrapedProductObservation.match_status.in_(["matched", "conflict"]),
    ).order_by(ScrapedProductObservation.scraped_at.desc()).limit(100).all()
    candidates = []
    for row in rows:
        payload = row.normalized_payload or {}
        summary = payload.get("review_summary") if isinstance(payload.get("review_summary"), dict) else {}
        rating = summary.get("average_rating", payload.get("rating"))
        count = summary.get("review_count", payload.get("review_count"))
        if rating in (None, "") and count in (None, "") and not summary:
            continue
        # Attached variant evidence outranks product-level evidence. Conflicts
        # remain visible but rank below accepted exact matches.
        scope = "exact_variant" if row.product_variant_id else "exact_product"
        score = (
            2 if row.match_status == "matched" else 1,
            2 if scope == "exact_variant" else 1,
            1 if summary.get("ai_summary_lines") or summary.get("ai_summary_text") else 0,
            _integer(summary.get("review_sample_count")),
            _integer(count),
            row.scraped_at,
        )
        candidates.append((score, row, payload, summary, rating, count, scope))
    # Explicit customer-feed aggregates are exact-product evidence too. They
    # participate in the same ranking instead of being selected by a UI loop.
    listings = db.query(SourceListing).filter(
        SourceListing.canonical_product_id == product_id, SourceListing.is_deleted == False,
    ).order_by(SourceListing.created_at.desc()).limit(30).all()
    for listing in listings:
        job = db.query(ImportJob).filter(ImportJob.id == listing.import_job_id).first() if listing.import_job_id else None
        raw, mapping = listing.raw_data or {}, (job.column_mapping or {}) if job else {}
        def mapped(name):
            column = mapping.get(name)
            return raw.get(column) if column else raw.get(name)
        rating, count, summary = mapped("rating"), mapped("review_count"), mapped("review_summary")
        if rating in (None, "") and count in (None, "") and not summary:
            continue
        summary = summary if isinstance(summary, dict) else ({"summary": summary} if summary else {})
        result = {
            "average_rating": float(rating) if rating not in (None, "") else None,
            "review_count": _integer(count) if count not in (None, "") else None,
            "review_summary": summary or None, "source": listing.retailer or (job.source_name if job else None),
            "source_domain": urlparse(listing.source_url).hostname if listing.source_url else None,
            "observation_date": listing.created_at, "match_scope": "exact_product",
            "evidence_reference": f"source_listing:{listing.id}", "observation_id": None,
        }
        candidates.append(((2, 1, 1 if summary else 0, 0, _integer(count), listing.created_at), None, result, {}, rating, count, "exact_product"))
    if not candidates:
        return None
    _, row, payload, summary, rating, count, scope = max(candidates, key=lambda item: item[0])
    if row is None:
        return payload
    return {
        "average_rating": float(rating) if rating not in (None, "") else None,
        "review_count": _integer(count) if count not in (None, "") else None,
        "review_summary": summary or None,
        "source": row.source_name,
        "source_domain": row.source_domain or (urlparse(row.source_url).hostname if row.source_url else None),
        "observation_date": row.scraped_at,
        "match_scope": scope,
        "evidence_reference": f"scraped_product_observation:{row.id}",
        "observation_id": row.id,
    }
