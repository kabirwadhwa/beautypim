"""Source-backed catalogue context for product enrichment.

This deliberately retrieves only observations already matched to the same
canonical product.  It is grounding data, not permission for the model to
borrow attributes from merely similar products.
"""
from __future__ import annotations

from typing import Any, Optional
import uuid

from sqlalchemy.orm import Session

from app.models import (
    FieldValue,
    Formulation,
    ScrapedProductObservation,
)


MAX_OBSERVATIONS = 3
MAX_FIELD_VALUES = 80
MAX_TEXT_LENGTH = 12_000


def _trim(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_TEXT_LENGTH:
        return value[:MAX_TEXT_LENGTH] + "…"
    if isinstance(value, list):
        return [_trim(item) for item in value[:100]]
    if isinstance(value, dict):
        return {str(key): _trim(item) for key, item in list(value.items())[:100]}
    return value


def build_catalogue_knowledge_context(
    db: Session,
    canonical_product_id: Optional[uuid.UUID],
) -> dict[str, Any]:
    """Return compact, attributable knowledge for one exact product."""
    if not canonical_product_id:
        return {}

    observations = (
        db.query(ScrapedProductObservation)
        .filter(
            ScrapedProductObservation.canonical_product_id == canonical_product_id
        )
        .order_by(ScrapedProductObservation.scraped_at.desc())
        .limit(MAX_OBSERVATIONS)
        .all()
    )
    field_values = (
        db.query(FieldValue)
        .filter(
            FieldValue.canonical_product_id == canonical_product_id,
            FieldValue.is_current == True,
            FieldValue.source_type.in_(["source_data", "human_edit"]),
        )
        .order_by(FieldValue.updated_at.desc())
        .limit(MAX_FIELD_VALUES)
        .all()
    )
    formulations = (
        db.query(Formulation)
        .filter(
            Formulation.canonical_product_id == canonical_product_id,
            Formulation.is_deleted == False,
        )
        .order_by(Formulation.created_at.desc())
        .limit(3)
        .all()
    )

    if not observations and not field_values and not formulations:
        return {}

    return {
        "usage_policy": (
            "Exact-product catalogue evidence only. Prefer direct observed values "
            "over inference, preserve contradictions, and cite the supplied source "
            "URL in reasoning/evidence. Do not treat this as evidence for another product."
        ),
        "observations": [
            {
                "source_name": item.source_name,
                "source_domain": item.source_domain,
                "source_url": item.source_url,
                "observed_at": item.scraped_at.isoformat() if item.scraped_at else None,
                "match_status": item.match_status,
                "data": _trim(item.normalized_payload),
            }
            for item in observations
        ],
        "accepted_or_current_fields": [
            {
                "field_name": item.field_name,
                "value": _trim(item.value),
                "review_status": item.review_status,
                "source_type": item.source_type,
                "source_reference": item.source_reference,
            }
            for item in field_values
        ],
        "formulations": [
            {
                "raw_inci_text": _trim(item.raw_inci_text),
                "market": item.market,
                "language": item.language,
                "source_reference": item.source_reference,
                "observed_at": item.created_at.isoformat() if item.created_at else None,
            }
            for item in formulations
        ],
    }
