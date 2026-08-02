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
from app.services.deduplication import normalize_text


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
    *,
    product_name: str = "",
    brand: str = "",
    gtin: str = "",
    category: str = "",
    product_family: str = "",
) -> dict[str, Any]:
    """Return compact, attributable knowledge for one exact product."""
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

    retail_rows = (
        db.query(ScrapedProductObservation)
        .filter(ScrapedProductObservation.source_domain == "retail-data.invalid")
        .order_by(ScrapedProductObservation.scraped_at.desc())
        .all()
    )
    normalized_name = normalize_text(product_name)
    normalized_brand = normalize_text(brand)
    normalized_gtin = "".join(character for character in str(gtin or "") if character.isdigit())
    exact_retail = []
    taxonomy_examples = []
    taxonomy_terms = {
        normalize_text(value) for value in (category, product_family)
        if normalize_text(value)
    }
    for row in retail_rows:
        payload = row.normalized_payload or {}
        row_gtin = "".join(character for character in str(payload.get("gtin") or payload.get("ean") or payload.get("upc") or "") if character.isdigit())
        same_gtin = bool(normalized_gtin and row_gtin == normalized_gtin)
        same_identity = bool(
            normalized_name and normalized_brand
            and normalize_text(str(payload.get("product_name") or "")) == normalized_name
            and normalize_text(str(payload.get("brand") or "")) == normalized_brand
        )
        if same_gtin or same_identity:
            exact_retail.append(row)
            continue
        row_categories = {
            normalize_text(str(value))
            for value in (payload.get("category_path") or [])
            if normalize_text(str(value))
        }
        row_type = normalize_text(str(payload.get("product_type") or ""))
        if taxonomy_terms and (taxonomy_terms & row_categories or row_type in taxonomy_terms):
            taxonomy_examples.append(row)
    exact_retail = exact_retail[:MAX_OBSERVATIONS]
    taxonomy_examples = taxonomy_examples[:5]

    if not observations and not field_values and not formulations and not exact_retail and not taxonomy_examples:
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
        "retail_reference_matches": [
            {
                "match_basis": "exact barcode or exact normalized brand and product name",
                "source_name": "Retail Data",
                "source_url": item.source_url,
                "observed_at": item.scraped_at.isoformat() if item.scraped_at else None,
                "data": _trim(item.normalized_payload),
            }
            for item in exact_retail
        ],
        "retail_taxonomy_examples": [
            {
                "usage_restriction": "Taxonomy vocabulary only; never transfer claims, ingredients, benefits or compliance attributes.",
                "category_path": _trim((item.normalized_payload or {}).get("category_path")),
                "product_type": _trim((item.normalized_payload or {}).get("product_type")),
            }
            for item in taxonomy_examples
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
