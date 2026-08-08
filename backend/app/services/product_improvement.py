"""Guided product identity and enrichment coverage analysis.

This module intentionally separates adventurous discovery from attachment: it
can suggest close catalogue identities and researchable fields, but does not
merge formulation-specific evidence until a user confirms the variant.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    CanonicalProduct, Category, FieldValue, Formulation, ProductVariant,
    ScrapedProductObservation, SourceListing,
)
from app.services.deduplication import normalize_text
from app.services.product_identity import product_is_fragrance, trusted_product_version


IDENTITY_FIELDS = ("brand", "product_name", "format", "variant", "size", "gtin", "market")
RESEARCHABLE_FIELDS = (
    "description", "image_url", "fragrance_intelligence", "ingredients_intelligence",
    "directions", "benefits", "product_credentials", "targeted_concerns",
    "brand_origin", "country_of_manufacture", "launch_year",
)
EVIDENCE_REQUIRED_FIELDS = (
    "ingredients_intelligence", "image_url", "brand_origin", "country_of_manufacture",
    "launch_year", "dermatologically_tested", "clinically_tested", "phthalate_free",
)


def _present(value: Any) -> bool:
    return value not in (None, "", [], {}) and str(value).strip().lower() not in {
        "unknown", "not provided", "not_provided", "unverified", "none", "null"
    }


def _latest_source(db: Session, product_id: uuid.UUID) -> dict[str, Any]:
    listing = db.query(SourceListing).filter(
        SourceListing.canonical_product_id == product_id,
    ).order_by(SourceListing.created_at.desc()).first()
    return dict(listing.raw_data or {}) if listing else {}


def _find_value(raw: dict[str, Any], *names: str) -> Any:
    normalized = {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in raw.items()}
    for name in names:
        value = normalized.get(re.sub(r"[^a-z0-9]", "", name.lower()))
        if _present(value):
            return value
    return None


def product_improvement_summary(db: Session, product: CanonicalProduct) -> dict[str, Any]:
    variant = db.query(ProductVariant).filter(
        ProductVariant.canonical_product_id == product.id,
        ProductVariant.is_deleted == False,
    ).order_by(ProductVariant.created_at.asc()).first()
    current = {
        row.field_name: row
        for row in db.query(FieldValue).filter(
            FieldValue.canonical_product_id == product.id,
            FieldValue.is_current == True,
        ).all()
    }
    raw = _latest_source(db, product.id)
    category = db.query(Category).filter(Category.id == product.category_id).first() if product.category_id else None
    format_row = current.get("product_type")
    format_value = (
        format_row.value
        if format_row and format_row.source_type in {"source_data", "human_edit"}
        else _find_value(raw, "type", "format", "concentration", "product_type")
    )
    if product_is_fragrance(db, product) and not trusted_product_version(db, product):
        format_value = None
    market = _find_value(raw, "market", "country", "locale")
    identity = {
        "brand": product.brand.name if product.brand else None,
        "product_name": product.product_name,
        "format": format_value,
        "variant": variant.variant_name if variant else None,
        "size": f"{variant.size or ''}{variant.unit or ''}".strip() if variant else None,
        "gtin": variant.gtin if variant else None,
        "market": market,
    }
    missing_identity = [key for key, value in identity.items() if not _present(value)]

    formulations = db.query(Formulation).filter(
        Formulation.canonical_product_id == product.id,
        Formulation.is_deleted == False,
    ).all()
    has_inci = any(_present(row.raw_inci_text) for row in formulations)
    coverage_fields = set(current)
    description = (
        current.get("description").value if current.get("description") else None
    ) or _find_value(raw, "description", "product_description", "long_description")
    if _present(description):
        coverage_fields.add("description")
    if product.image_url:
        coverage_fields.add("image_url")
    if has_inci:
        coverage_fields.add("ingredients_intelligence")
    missing_research = [field for field in RESEARCHABLE_FIELDS if field not in coverage_fields]

    target_name = normalize_text(product.product_name)
    target_brand = normalize_text(identity["brand"] or "")
    rows = db.query(ScrapedProductObservation).filter(
        ScrapedProductObservation.source_domain == "retail-data.invalid",
    ).all()
    candidates = []
    seen = set()
    target_tokens = set(target_name.split())
    for row in rows:
        payload = row.normalized_payload or {}
        candidate_name = normalize_text(str(payload.get("product_name") or ""))
        candidate_brand = normalize_text(str(payload.get("brand") or ""))
        if not candidate_name or not candidate_brand:
            continue
        name_tokens = set(candidate_name.split())
        overlap = len(target_tokens & name_tokens) / max(1, len(target_tokens | name_tokens))
        exact_brand = candidate_brand == target_brand
        if not exact_brand or overlap < 0.45:
            continue
        key = (candidate_brand, candidate_name, str(payload.get("size") or ""), str(payload.get("product_type") or ""))
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "observation_id": str(row.id),
            "brand": payload.get("brand"),
            "product_name": payload.get("product_name"),
            "format": payload.get("product_type") or payload.get("variant_name"),
            "size": payload.get("size"),
            "gtin": payload.get("gtin") or payload.get("ean") or payload.get("upc"),
            "country": payload.get("country"),
            "source_url": payload.get("source_url") or row.source_url,
            "match_score": round(min(0.99, 0.55 + overlap * 0.4), 2),
        })
    candidates.sort(key=lambda item: item["match_score"], reverse=True)
    candidates = candidates[:8]

    ambiguous = bool((product_is_fragrance(db, product) and not trusted_product_version(db, product)) or (candidates and (
        not identity["gtin"] or len({(c.get("format"), c.get("size")) for c in candidates}) > 1
    )))
    completeness = round(100 * (len(IDENTITY_FIELDS) - len(missing_identity)) / len(IDENTITY_FIELDS))
    status = "complete" if completeness >= 85 and not ambiguous else "ambiguous" if ambiguous else "incomplete"
    return {
        "identity_status": status,
        "identity_completeness": completeness,
        "identity": identity,
        "missing_identity_fields": missing_identity,
        "knowledge_coverage": round(100 * (len(RESEARCHABLE_FIELDS) - len(missing_research)) / len(RESEARCHABLE_FIELDS)),
        "fields_recommended_for_research": missing_research,
        "evidence_required_fields": list(EVIDENCE_REQUIRED_FIELDS),
        "inference_eligible_fields": [
            "subcategory", "product_type", "texture", "application_area", "target_audience",
            "product_positioning", "sensory_description", "routine_time", "routine_step",
        ],
        "candidate_products": candidates,
        "category": category.path if category else None,
    }
