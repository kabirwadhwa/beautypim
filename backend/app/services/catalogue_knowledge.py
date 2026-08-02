"""Source-backed catalogue context for product enrichment.

Exact observations remain evidence.  The anonymised retail corpus is also
used as a retrieval knowledge base: comparable products teach the model the
industry's taxonomy and merchandising patterns without becoming assertions
about the target product.
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
MAX_RETAIL_EXAMPLES = 8
MAX_FIELD_VALUES = 80
MAX_TEXT_LENGTH = 12_000

STOP_WORDS = {
    "a", "an", "and", "for", "from", "in", "of", "on", "the", "to", "with",
    "beauty", "product", "new", "care", "ml", "g", "oz",
}


def _tokens(value: Any) -> set[str]:
    """Return useful normalized retrieval terms, including joined compounds."""
    normalized = normalize_text(str(value or ""))
    parts = {
        part for part in normalized.split()
        if len(part) > 2 and part not in STOP_WORDS
    }
    if normalized:
        parts.add(normalized)
    return parts


def _payload_tokens(payload: dict[str, Any]) -> dict[str, set[str]]:
    categories = payload.get("category_path") or []
    return {
        "name": _tokens(payload.get("product_name")),
        "brand": _tokens(payload.get("brand")),
        "taxonomy": _tokens(" ".join(str(item) for item in categories))
        | _tokens(payload.get("product_type")),
        "content": _tokens(payload.get("description"))
        | _tokens(" ".join(str(item) for item in (payload.get("benefits") or [])))
        | _tokens(" ".join(str(item) for item in (payload.get("claims") or []))),
    }


def _retail_similarity(
    payload: dict[str, Any], *, name: str, brand: str, category: str,
    product_family: str, description: str,
) -> tuple[float, list[str]]:
    """Rank comparable retail products with deterministic lexical signals."""
    row = _payload_tokens(payload)
    name_terms = _tokens(name)
    brand_terms = _tokens(brand)
    taxonomy_terms = _tokens(category) | _tokens(product_family)
    description_terms = _tokens(description)
    score = 0.0
    reasons: list[str] = []

    name_overlap = name_terms & (row["name"] | row["taxonomy"])
    taxonomy_overlap = taxonomy_terms & row["taxonomy"]
    description_overlap = description_terms & (row["name"] | row["taxonomy"] | row["content"])
    brand_overlap = brand_terms & row["brand"]
    if name_overlap:
        score += min(12.0, len(name_overlap) * 4.0)
        reasons.append("name/product-type terms: " + ", ".join(sorted(name_overlap)[:5]))
    if taxonomy_overlap:
        score += min(10.0, len(taxonomy_overlap) * 5.0)
        reasons.append("taxonomy terms: " + ", ".join(sorted(taxonomy_overlap)[:5]))
    if brand_overlap:
        score += 3.0
        reasons.append("brand-family signal")
    if description_overlap:
        score += min(6.0, len(description_overlap) * 1.5)
        reasons.append("description terms: " + ", ".join(sorted(description_overlap)[:5]))
    return score, reasons


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
    description: str = "",
) -> dict[str, Any]:
    """Return exact evidence plus ranked, attributable retail knowledge."""
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
    comparable_retail = []
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
        similarity, reasons = _retail_similarity(
            payload,
            name=product_name,
            brand=brand,
            category=category,
            product_family=product_family,
            description=description,
        )
        if similarity > 0:
            comparable_retail.append((similarity, reasons, row))
    exact_retail = exact_retail[:MAX_OBSERVATIONS]
    comparable_retail.sort(key=lambda item: (item[0], item[2].scraped_at), reverse=True)
    comparable_retail = comparable_retail[:MAX_RETAIL_EXAMPLES]

    if not observations and not field_values and not formulations and not exact_retail and not comparable_retail:
        return {}

    return {
        "usage_policy": (
            "Exact-product matches are product evidence. Comparable retail examples are industry "
            "knowledge for reasonable inference: use their taxonomy, product positioning, "
            "benefit patterns, concern patterns, texture, usage and audience vocabulary. "
            "Never present a comparable product's ingredients, claims, certifications, "
            "price or compliance status as a verified fact about the target product."
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
        "retail_knowledge_examples": [
            {
                "knowledge_role": "Comparable industry example; supports inference, not direct claims.",
                "similarity_score": score,
                "similarity_basis": reasons,
                "data": _trim(item.normalized_payload),
            }
            for score, reasons, item in comparable_retail
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
