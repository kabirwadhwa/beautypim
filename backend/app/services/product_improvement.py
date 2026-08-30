"""Guided product identity and enrichment coverage analysis.

This module intentionally separates adventurous discovery from attachment: it
can suggest close catalogue identities and researchable fields, but does not
merge formulation-specific evidence until a user confirms the variant.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import (
    CanonicalProduct, Category, FieldValue, Formulation, IngredientDefinition, ProductVariant,
    ScrapedProductObservation, SourceListing,
)
from app.services.deduplication import normalize_text
from app.services.product_identity import product_is_fragrance, trusted_product_version
from app.knowledge_corpus.retrieval import retrieve_corpus_evidence
from app.services.category_completeness import build_gap_plan


IDENTITY_FIELDS = ("brand", "product_name", "format", "variant", "size", "gtin", "market")
RESEARCHABLE_FIELDS = (
    "description", "image_url", "fragrance", "ingredients_intelligence",
    "directions", "benefits", "claims", "targeted_concerns",
    "product_positioning", "sensory_description", "warnings_considerations",
)
EVIDENCE_REQUIRED_FIELDS = (
    "ingredients_intelligence", "image_url", "claims", "warnings_considerations",
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
    from app.services.product_identity import preferred_product_variant
    variant = preferred_product_variant(db, product.id)
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
    # Exact GTIN plus brand/name/format is sufficient foundational identity.
    # Variant, size and market are valuable when the source/product exposes
    # them, but their absence must not make an un-sized tool look unresolved.
    required_identity_fields = ["brand", "product_name", "format", "gtin"]
    if _find_value(raw, "variant", "shade", "colour", "color") or (variant and variant.variant_name):
        required_identity_fields.append("variant")
    if _find_value(raw, "size", "volume", "weight") or (variant and (variant.size or variant.unit)):
        required_identity_fields.append("size")
    missing_identity = [key for key in required_identity_fields if not _present(identity.get(key))]

    from app.services.formulation_resolution import resolve_selected_formulation, formulation_ingredient_rows
    selected_formulation = resolve_selected_formulation(db, product.id, variant.id if variant else None)
    formulations = [selected_formulation] if selected_formulation else []
    ingredient_rows = formulation_ingredient_rows(db, selected_formulation)
    has_inci = bool(selected_formulation and _present(selected_formulation.raw_inci_text))
    resolved_rows = [row for row in ingredient_rows if row.ingredient_definition_id]
    definitions = {
        row.id: row for row in db.query(IngredientDefinition).filter(
            IngredientDefinition.id.in_([item.ingredient_definition_id for item in resolved_rows])
        ).all()
    } if resolved_rows else {}
    intelligent_rows = [
        row for row in resolved_rows
        if row.ingredient_definition_id in definitions and any((
            definitions[row.ingredient_definition_id].function,
            definitions[row.ingredient_definition_id].possible_concerns,
        ))
    ]
    key_rows = [row for row in ingredient_rows if row.is_key_ingredient]
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

    snapshot = {
        "brand": identity["brand"], "product_name": product.product_name,
        "gtin": identity["gtin"], "size": identity["size"],
        "category": category.path if category else "", "product_type": format_value,
        "description": description, "image_url": product.image_url,
        "inci": next((row.raw_inci_text for row in formulations if _present(row.raw_inci_text)), None),
        "key_ingredients": [row.raw_inci_name for row in key_rows],
    }
    understanding_row = current.get("product_understanding")
    if understanding_row and isinstance(understanding_row.value, dict):
        snapshot["product_understanding"] = understanding_row.value
        snapshot["category_module"] = understanding_row.value.get("category_module")
    metadata = {}
    for name, row in current.items():
        snapshot[name] = row.value
        metadata[name] = {
            "source_type": row.source_type, "semantic_status": row.semantic_status,
            "evidence": row.evidence or [], "researched": bool(row.enrichment_run_id),
        }
    gap_plan = build_gap_plan(snapshot, metadata)
    # Market observations are completion targets too, but remain separate from
    # intrinsic product attributes.  Image and aggregate reviews are safe to
    # research for an identified product family even while a fragrance's exact
    # EDT/EDP concentration remains unresolved.
    from app.services.review_aggregate import select_review_aggregate
    review = select_review_aggregate(db, product.id)
    market_observation_gaps = []
    if not product.image_url:
        market_observation_gaps.append("image_url")
    if (not review or review.get("review_quality") == "insufficient"
            or review.get("review_intelligence_strength") == "insufficient"
            or (review.get("average_rating") is None and not review.get("review_count"))):
        market_observation_gaps.append("reviews")
    research_objectives = list(gap_plan["research_objectives"])
    existing_objectives = {item.get("field") for item in research_objectives}
    for field in market_observation_gaps:
        if field not in existing_objectives:
            research_objectives.append({
                "field": field, "objective_type": "market_observation",
                "requires_direct_evidence": True,
                "instruction": f"Find exact product-family evidence for {field.replace('_', ' ')}.",
            })

    corpus = retrieve_corpus_evidence(
        db, gtin=identity["gtin"] or "", brand=identity["brand"] or "",
        product_name=product.product_name, category=category.path if category else "",
        max_comparables=8,
    )
    candidates = []
    for level, rows in (("exact_product", corpus.get("exact_matches", [])),
                        ("product_family", corpus.get("family_matches", [])),
                        ("comparable", corpus.get("comparables", []))):
        for row in rows:
            candidates.append({
                "knowledge_product_id": row.get("knowledge_product_id"),
                "knowledge_variant_id": row.get("knowledge_variant_id"),
                "brand": row.get("brand"), "product_name": row.get("product_name"),
                "format": row.get("product_type"), "size": None, "gtin": row.get("gtin"),
                "source_type": "Retail Data", "match_type": level,
                "match_score": 1.0 if level == "exact_product" else 0.8 if level == "product_family" else 0.5,
            })

    ambiguous = bool((product_is_fragrance(db, product) and not trusted_product_version(db, product)) or (candidates and (
        not identity["gtin"] or len({(c.get("format"), c.get("size")) for c in candidates}) > 1
    )))
    authoritative_identity_status = (snapshot.get("product_understanding") or {}).get("identity_status")
    if authoritative_identity_status == "resolved":
        # The Product Understanding contract is authoritative and may safely
        # resolve products without a GTIN through protected human identity.
        missing_identity = []
        ambiguous = False
    completeness = round(100 * (len(required_identity_fields) - len(missing_identity)) /
                         len(required_identity_fields))
    status = "complete" if completeness >= 85 and not ambiguous else "ambiguous" if ambiguous else "incomplete"
    result = {
        "identity_status": status,
        "identity_completeness": completeness,
        "identity": identity,
        "missing_identity_fields": missing_identity,
        "knowledge_coverage": gap_plan["overall_completeness"],
        "overall_completeness": gap_plan["overall_completeness"],
        "content_completeness": gap_plan["content_completeness"],
        "commercial_completeness": gap_plan["commercial_completeness"],
        "category_completeness": gap_plan["category_completeness"],
        "evidence_completeness": gap_plan["evidence_completeness"],
        "research_completeness": gap_plan["research_completeness"],
        "category_module": gap_plan["category_module"],
        "taxonomy_status": gap_plan.get("taxonomy_status"),
        "product_understanding": snapshot.get("product_understanding") or {},
        "field_states": gap_plan["field_states"],
        "missing_high_priority_fields": gap_plan["missing_high_priority_fields"],
        "missing_optional_fields": gap_plan["missing_optional_fields"],
        "research_objectives": research_objectives,
        "market_observation_gaps": market_observation_gaps,
        "research_phase": gap_plan["phase"],
        "identity_resolution_required": gap_plan["identity_resolution_required"],
        "taxonomy_resolution_required": gap_plan.get("taxonomy_resolution_required", False),
        "blocked_objectives": gap_plan.get("blocked_objectives", []),
        "fields_recommended_for_research": [
            item["field"] for item in gap_plan["research_objectives"] + gap_plan.get("blocked_objectives", [])
        ],
        "evidence_required_fields": list(EVIDENCE_REQUIRED_FIELDS),
        "inference_eligible_fields": [
            "subcategory", "product_type", "texture", "application_area", "target_audience",
            "product_positioning", "sensory_description", "routine_time", "routine_step",
        ],
        "candidate_products": candidates,
        "corpus_match_level": corpus.get("match_level"),
        "category": category.path if category else None,
        "ingredient_completeness": {
            "formulation_complete": has_inci,
            "total_ingredients": len(ingredient_rows),
            "resolved_ingredients": len(resolved_rows),
            "identity_resolution_coverage": round(100 * len(resolved_rows) / len(ingredient_rows)) if ingredient_rows else 0,
            "ingredients_with_trusted_intelligence": len(intelligent_rows),
            "ingredient_intelligence_coverage": round(100 * len(intelligent_rows) / len(resolved_rows)) if resolved_rows else 0,
            "key_ingredient_evidence_status": "source_supported" if key_rows else "not_published_or_not_found",
            "key_ingredient_count": len(key_rows),
        },
    }
    from app.services.identity_review import does_this_product_require_identity_review
    review = does_this_product_require_identity_review(db, product, result)
    result["identity_review"] = review
    result["identity_review_required"] = review["requires_review"]
    result["identity_review_status"] = review["review_status"]
    result["identity_review_reasons"] = review["reasons"]
    result["understanding_fingerprint"] = review["understanding_fingerprint"]
    # Candidates are presentation data only. Comparable candidates remain
    # visibly scoped and can never be confirmed as exact facts implicitly.
    for candidate in result["candidate_products"]:
        candidate.setdefault("confidence", candidate.get("match_score"))
        candidate.setdefault("evidence_summary", {
            "exact_product": "Exact product corpus evidence",
            "product_family": "Strong product-family evidence",
            "comparable": "Comparable product context — not exact identity evidence",
        }.get(candidate.get("match_type"), "Existing identity evidence"))
        candidate.setdefault("source_reference", candidate.get("source_type"))
    return result
