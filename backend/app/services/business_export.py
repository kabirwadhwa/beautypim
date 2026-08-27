"""Stable, complete business-facing product export contract.

This module intentionally contains no enrichment logic.  It projects the same
canonical product dossier used by the Product Detail page/PDF into a flat,
readable export while keeping lists and structured values intact for JSON.
"""
from __future__ import annotations

from typing import Any

from app.models import ScrapedProductObservation


BUSINESS_EXPORT_COLUMNS = (
    # Identity and workflow
    "product_id", "internal_code", "review_status", "tags", "brand", "product_name",
    "product_family", "format", "gtin_ean_upc", "sku", "variant", "size", "unit",
    "all_variants", "variant_count", "category", "subcategory", "product_type",
    "application_area", "category_module", "identity_status", "taxonomy_status",
    # Canonical commercial content
    "description", "article_description", "product_usp", "product_positioning", "target_audience_profile_1",
    "target_audience_profile_2", "target_audience_profile_3", "target_audience_profiles",
    "benefits", "targeted_concerns", "directions_how_to_use", "routine_time",
    "routine_step", "sensory_description", "claims", "warnings_considerations",
    # Media and current market observation
    "primary_image_url", "additional_image_urls", "product_source_url", "source_name",
    "source_domain", "market_country", "language_locale", "current_price", "promotional_price", "currency",
    "availability", "market_observation_date", "market_observations",
    # Formulation and ingredient intelligence
    "raw_inci", "all_formulations", "formulation_market", "formulation_language", "formulation_effective_date",
    "ordered_ingredients", "key_ingredients", "ingredient_functions",
    "ingredient_benefits_utilities", "ingredient_caution_notes", "ingredient_intelligence",
    # Skincare
    "skincare_skin_types", "skincare_texture", "skincare_finish", "skincare_key_ingredients",
    # Haircare
    "haircare_hair_types", "haircare_texture_format", "haircare_key_ingredients",
    # Makeup
    "makeup_shade_colour", "makeup_coverage", "makeup_finish", "makeup_texture_format",
    # Fragrance
    "fragrance_concentration", "fragrance_family", "fragrance_top_notes",
    "fragrance_heart_notes", "fragrance_base_notes", "fragrance_longevity",
    "fragrance_sillage_projection", "fragrance_seasonal_fit", "fragrance_occasion_fit",
    # Beauty tools/accessories
    "accessory_purpose", "accessory_compatibility", "accessory_material",
    "accessory_care_instructions", "accessory_replacement_refill_status",
    "accessory_durability", "accessory_ergonomic_characteristics",
    # Canonical Review Intelligence
    "average_rating", "represented_review_count", "review_source_count",
    "actual_review_sample_count", "review_aggregate_strength",
    "review_intelligence_strength", "review_quality", "review_source",
    "review_source_domain", "review_observation_date", "review_positive_themes",
    "review_negative_themes", "review_mixed_themes", "full_ai_review_summary",
    "review_limitations", "review_sources", "customer_review_summary",
    "customer_taxonomy_bgb_subgroup", "customer_taxonomy_bgb_typegroup", "additional_imported_attributes",
    # Business-useful completeness and quality state
    "overall_completeness", "identity_completeness", "content_completeness",
    "commercial_completeness", "category_completeness", "evidence_completeness",
    "research_completeness", "missing_high_priority_fields", "missing_optional_fields",
    "validation_issue_count", "highest_validation_severity", "created_at", "updated_at",
)


# Contract used by tests: every current ProductDetailOut member must be either
# represented by one or more business columns or explicitly internal-only.
PRODUCT_DETAIL_EXPORT_COVERAGE = {
    "id": {"product_id"}, "internal_code": {"internal_code"},
    "product_name": {"product_name"}, "brand_name": {"brand"},
    "category_path": {"category", "subcategory"}, "product_category": {"category"},
    "subcategory": {"subcategory"}, "product_type": {"product_type"},
    "gtin": {"gtin_ean_upc"}, "variant_count": {"variant_count"},
    "image_url": {"primary_image_url"}, "review_status": {"review_status"},
    "validation_issue_count": {"validation_issue_count"},
    "highest_issue_severity": {"highest_validation_severity"}, "tags": {"tags"},
    "identity_review_status": {"identity_status"}, "created_at": {"created_at"},
    "updated_at": {"updated_at"}, "description": {"description"},
    "variants": {"all_variants", "variant", "size", "unit", "gtin_ean_upc"},
    "formulations": {"raw_inci", "formulation_market", "formulation_language", "formulation_effective_date"},
    "key_ingredients": {"ordered_ingredients", "key_ingredients", "ingredient_functions", "ingredient_benefits_utilities", "ingredient_caution_notes"},
    "dynamic_concerns": {"targeted_concerns"}, "market_observations": {"market_observations"},
    "review_aggregate": {"average_rating", "represented_review_count", "full_ai_review_summary"},
    "product_understanding": {"product_family", "sku", "identity_status", "taxonomy_status", "category_module"},
    "completeness": {"overall_completeness", "missing_high_priority_fields"},
    "source_attributes": {"additional_imported_attributes"},
}
INTERNAL_PRODUCT_DETAIL_FIELDS = {
    "brand_id", "category_id", "reviewer_id", "is_deleted", "field_values",
    "validation_issues", "enrichment_metadata", "corpus_evidence", "improvement_result",
    "identity_review",
}


FIELD_VALUE_EXPORT_COVERAGE = {
    "subcategory", "product_type", "application_area", "target_audience",
    "product_usp", "product_positioning", "benefits", "targeted_concerns", "directions",
    "sensory_description", "routine_time", "routine_step", "claims",
    "warnings_considerations", "skincare", "haircare", "makeup", "fragrance",
    "ingredients_intelligence", "product_family", "purpose", "compatibility", "material",
    "care_instructions", "replacement_refill_status", "durability", "ergonomic_characteristics",
    "availability", "rating", "review_count", "review_summary", "description",
    "article_description", "customer_review_summary", "bgb_subgroup", "bgb_typegroup",
}
INTERNAL_FIELD_VALUE_FIELDS = {"schema_org", "product_understanding"}


CATEGORY_FIELD_EXPORT_MAP = {
    "skincare": {
        "skin_types": "skincare_skin_types", "texture": "skincare_texture",
        "finish": "skincare_finish", "inci": "raw_inci", "key_ingredients": "skincare_key_ingredients",
        "targeted_concerns": "targeted_concerns",
    },
    "haircare": {
        "hair_types": "haircare_hair_types", "texture_format": "haircare_texture_format",
        "inci": "raw_inci", "key_ingredients": "haircare_key_ingredients",
        "targeted_concerns": "targeted_concerns",
    },
    "makeup": {
        "shade_colour": "makeup_shade_colour", "coverage": "makeup_coverage",
        "finish": "makeup_finish", "texture_format": "makeup_texture_format", "inci": "raw_inci",
    },
    "fragrance": {
        "concentration": "fragrance_concentration", "fragrance_family": "fragrance_family",
        "top_notes": "fragrance_top_notes", "heart_notes": "fragrance_heart_notes",
        "base_notes": "fragrance_base_notes", "longevity": "fragrance_longevity",
        "sillage_projection": "fragrance_sillage_projection", "seasonal_fit": "fragrance_seasonal_fit",
        "occasion_fit": "fragrance_occasion_fit", "inci": "raw_inci",
    },
    "beauty_accessory": {
        "purpose": "accessory_purpose", "compatibility": "accessory_compatibility",
        "material": "accessory_material", "directions": "directions_how_to_use",
        "care_instructions": "accessory_care_instructions",
        "replacement_refill_status": "accessory_replacement_refill_status",
        "durability": "accessory_durability",
        "ergonomic_characteristics": "accessory_ergonomic_characteristics",
    },
}


def _field_values(detail: dict[str, Any], include_inferred: bool) -> dict[str, Any]:
    result: dict[str, Any] = {}
    priority = {"human_edit": 4, "source_data": 3, "retail_data": 3, "deterministic_rule": 2, "ai_inference": 1}
    rows = sorted(detail.get("field_values") or [], key=lambda row: str(row.get("updated_at") or ""))
    for row in rows:
        if not row.get("is_current") or row.get("review_status") == "conflicting":
            continue
        if row.get("source_type") == "ai_inference" and not include_inferred:
            continue
        name = row.get("field_name")
        if name and priority.get(row.get("source_type"), 0) >= priority.get(result.get(f"__source__{name}"), -1):
            result[name] = row.get("value")
            result[f"__source__{name}"] = row.get("source_type")
    return result


def _identity_value(understanding: dict[str, Any], key: str) -> Any:
    item = (understanding.get("identity") or {}).get(key)
    return item.get("value") if isinstance(item, dict) else item


def _unwrap(value: Any) -> Any:
    """Remove enrichment metadata wrappers without flattening real structures."""
    if isinstance(value, dict) and "value" in value and any(
        key in value for key in ("value_status", "confidence", "evidence", "reasoning_summary")
    ):
        return value.get("value")
    return value


def _list(value: Any) -> list[Any]:
    if value in (None, "", "UNKNOWN", "NOT_APPLICABLE"):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("values"), list):
        return value["values"]
    if isinstance(value, dict) and isinstance(value.get("value"), list):
        return value["value"]
    return [value]


def _current_market(observations: list[dict[str, Any]]) -> dict[str, Any]:
    useful = [row for row in observations if any(row.get(key) not in (None, "") for key in ("price", "promotional_price", "availability"))]
    if not useful:
        useful = [row for row in observations if row.get("source_url")]
    return max(useful, key=lambda row: str(row.get("observed_at") or ""), default={})


def _extra_images(db, product_id, primary: str | None) -> list[str]:
    urls: list[str] = []
    for observation in db.query(ScrapedProductObservation).filter(
        ScrapedProductObservation.canonical_product_id == product_id,
    ).order_by(ScrapedProductObservation.scraped_at.desc()).limit(100):
        payload = observation.normalized_payload or {}
        candidates = payload.get("image_urls") or ([payload.get("image_url")] if payload.get("image_url") else [])
        for url in candidates if isinstance(candidates, list) else [candidates]:
            if isinstance(url, str) and url and url != primary and url not in urls:
                urls.append(url)
    return urls


def build_business_row(db, detail_model, include_inferred: bool) -> dict[str, Any]:
    detail = detail_model.model_dump(mode="json") if hasattr(detail_model, "model_dump") else dict(detail_model)
    fields = _field_values(detail, include_inferred)
    understanding = detail.get("product_understanding") or fields.get("product_understanding") or {}
    completeness = detail.get("completeness") or {}
    module = understanding.get("category_module") or completeness.get("category_module") or "unknown"
    variants = detail.get("variants") or []
    first_variant = variants[0] if variants else {}
    category_path = [part.strip() for part in str(detail.get("category_path") or "").split(">") if part.strip()]
    targets = _list(fields.get("target_audience"))[:3]
    while len(targets) < 3:
        targets.append("")
    formulations = detail.get("formulations") or []
    formulation = formulations[0] if formulations else {}
    ingredients = detail.get("key_ingredients") or []
    market_rows = detail.get("market_observations") or []
    market = _current_market(market_rows)
    review = detail.get("review_aggregate") or {}
    review_summary = review.get("review_summary") if isinstance(review.get("review_summary"), dict) else {}

    row: dict[str, Any] = {column: "" for column in BUSINESS_EXPORT_COLUMNS}
    row.update({
        "product_id": str(detail.get("id") or ""), "internal_code": detail.get("internal_code"),
        "review_status": detail.get("review_status"), "tags": detail.get("tags") or [],
        "brand": detail.get("brand_name"), "product_name": detail.get("product_name"),
        "product_family": _unwrap(fields.get("product_family")) or _identity_value(understanding, "product_family"),
        "format": _identity_value(understanding, "format"),
        "gtin_ean_upc": first_variant.get("gtin") or detail.get("gtin"),
        "sku": _identity_value(understanding, "sku"), "variant": first_variant.get("variant_name") or _identity_value(understanding, "variant"),
        "size": first_variant.get("size"), "unit": first_variant.get("unit"), "all_variants": variants,
        "variant_count": detail.get("variant_count"), "category": detail.get("product_category") or (category_path[0] if category_path else None),
        "subcategory": _unwrap(fields.get("subcategory")) or detail.get("subcategory"),
        "product_type": _unwrap(fields.get("product_type")) or detail.get("product_type"),
        "application_area": _unwrap(fields.get("application_area")), "category_module": module,
        "identity_status": understanding.get("identity_status") or detail.get("identity_review_status"),
        "taxonomy_status": understanding.get("taxonomy_status") or completeness.get("taxonomy_status"),
        "description": detail.get("description"), "article_description": _unwrap(fields.get("article_description")),
        "product_usp": _unwrap(fields.get("product_usp")),
        "product_positioning": _unwrap(fields.get("product_positioning")),
        "target_audience_profile_1": targets[0], "target_audience_profile_2": targets[1],
        "target_audience_profile_3": targets[2], "target_audience_profiles": [item for item in targets if item],
        "benefits": _unwrap(fields.get("benefits")), "targeted_concerns": _unwrap(fields.get("targeted_concerns")),
        "directions_how_to_use": _unwrap(fields.get("directions")), "routine_time": _unwrap(fields.get("routine_time")),
        "routine_step": _unwrap(fields.get("routine_step")), "sensory_description": _unwrap(fields.get("sensory_description")),
        "claims": fields.get("claims"), "warnings_considerations": fields.get("warnings_considerations"),
        "primary_image_url": detail.get("image_url"),
        "additional_image_urls": _extra_images(db, detail.get("id"), detail.get("image_url")),
        "product_source_url": market.get("source_url"), "source_name": market.get("source_name"),
        "source_domain": market.get("source_domain"), "market_country": market.get("market"),
        "language_locale": formulation.get("language") or market.get("market"),
        "current_price": market.get("price"), "promotional_price": market.get("promotional_price"),
        "currency": market.get("currency"), "availability": market.get("availability") or fields.get("availability"),
        "market_observation_date": market.get("observed_at"), "market_observations": market_rows,
        "raw_inci": formulation.get("raw_inci_text"), "all_formulations": formulations,
        "formulation_market": formulation.get("market"),
        "formulation_language": formulation.get("language"), "formulation_effective_date": formulation.get("effective_date"),
        "ordered_ingredients": [{"name": item.get("name"), "position": item.get("position")} for item in ingredients],
        "key_ingredients": [item for item in ingredients if item.get("is_key_ingredient")],
        "ingredient_functions": [{"ingredient": item.get("name"), "functions": item.get("functions") or []} for item in ingredients if item.get("functions")],
        "ingredient_benefits_utilities": [{"ingredient": item.get("name"), "benefits": item.get("benefits") or []} for item in ingredients if item.get("benefits")],
        "ingredient_caution_notes": [{"ingredient": item.get("name"), "cautions": item.get("caution_notes") or []} for item in ingredients if item.get("caution_notes")],
        "ingredient_intelligence": fields.get("ingredients_intelligence"),
        "average_rating": review.get("average_rating") if review.get("business_display_rating", True) else None,
        "represented_review_count": review.get("represented_review_count") or review.get("review_count"),
        "review_source_count": review.get("review_source_count") or review.get("source_count"),
        "actual_review_sample_count": review.get("review_sample_count") or 0,
        "review_aggregate_strength": review.get("aggregate_strength") or review.get("review_quality"),
        "review_intelligence_strength": review.get("review_intelligence_strength") or review.get("evidence_strength"),
        "review_quality": review.get("review_quality"), "review_source": review.get("source"),
        "review_source_domain": review.get("source_domain"), "review_observation_date": review.get("observation_date"),
        "review_positive_themes": review_summary.get("positive_themes") or [],
        "review_negative_themes": review_summary.get("negative_themes") or [],
        "review_mixed_themes": review_summary.get("mixed_themes") or [],
        "full_ai_review_summary": review_summary.get("ai_summary_text") or review_summary.get("summary"),
        "review_limitations": review_summary.get("evidence_limitation"),
        "review_sources": review.get("sources") or review.get("source_breakdown") or [],
        "customer_review_summary": _unwrap(fields.get("customer_review_summary")),
        "customer_taxonomy_bgb_subgroup": _unwrap(fields.get("bgb_subgroup")),
        "customer_taxonomy_bgb_typegroup": _unwrap(fields.get("bgb_typegroup")),
        "overall_completeness": completeness.get("overall_completeness"),
        "identity_completeness": completeness.get("identity_completeness"),
        "content_completeness": completeness.get("content_completeness"),
        "commercial_completeness": completeness.get("commercial_completeness"),
        "category_completeness": completeness.get("category_completeness"),
        "evidence_completeness": completeness.get("evidence_completeness"),
        "research_completeness": completeness.get("research_completeness"),
        "missing_high_priority_fields": completeness.get("missing_high_priority_fields") or [],
        "missing_optional_fields": completeness.get("missing_optional_fields") or [],
        "validation_issue_count": detail.get("validation_issue_count"),
        "highest_validation_severity": detail.get("highest_issue_severity"),
        "created_at": detail.get("created_at"), "updated_at": detail.get("updated_at"),
    })

    additional: dict[str, Any] = {}
    for attribute in detail.get("source_attributes") or []:
        key = str(attribute.get("key") or "")
        if not key.startswith("source_attr."):
            continue
        label = str(attribute.get("label") or attribute.get("source_header") or key)
        export_key = f"imported_attribute:{label}"
        if export_key in row:
            export_key = f"{export_key} [{key.rsplit('.', 1)[-1]}]"
        row[export_key] = attribute.get("value")
        additional[label] = attribute.get("value")
    row["additional_imported_attributes"] = additional

    for category, mapping in CATEGORY_FIELD_EXPORT_MAP.items():
        block = fields.get(category) if isinstance(fields.get(category), dict) else {}
        for field_name, column in mapping.items():
            if field_name in {"inci", "directions", "targeted_concerns"}:
                continue
            row[column] = _unwrap(block.get(field_name)) if category == module else "NOT_APPLICABLE"
    # Accessory fields may be persisted top-level by current/legacy workflows.
    if module == "beauty_accessory":
        for field_name, column in CATEGORY_FIELD_EXPORT_MAP["beauty_accessory"].items():
            if field_name != "directions" and row.get(column) in ("", None):
                row[column] = fields.get(field_name, "")
    # A stable business table uses blank cells for unavailable values; category
    # fields that genuinely do not apply retain the explicit marker above.
    for column in BUSINESS_EXPORT_COLUMNS:
        if row[column] is None:
            row[column] = ""
    return row
