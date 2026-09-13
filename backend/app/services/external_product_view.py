"""Least-privilege product projection for external catalogue readers."""
from __future__ import annotations

from typing import Any


def _public_evidence(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    allowed = {
        "source_url", "url", "source_domain", "source_name", "source_title",
        "supporting_text", "evidence_excerpt", "observed_at", "match_scope",
    }
    return [
        {key: value for key, value in item.items() if key in allowed}
        for item in items if isinstance(item, dict)
    ]


def _source_label(source_type: str | None) -> str:
    if source_type in {"human_edit", "source_data", "retail_data", "verified_evidence"}:
        return "verified"
    if source_type in {"ai_inference", "ai_enrichment"}:
        return "inferred"
    return "canonical"


def _safe_review(review: Any) -> dict[str, Any] | None:
    if not isinstance(review, dict):
        return None
    allowed = {
        "average_rating", "review_count", "review_source_count", "review_sample_count",
        "aggregate_strength", "evidence_strength", "review_intelligence_strength",
        "review_quality", "business_display_rating", "review_summary", "review_limitations",
        "limitations", "source", "source_domain", "sources", "observation_date",
    }
    return {key: value for key, value in review.items() if key in allowed}


def _safe_completeness(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    allowed = {
        "identity_status", "identity_completeness", "identity", "missing_identity_fields",
        "knowledge_coverage", "overall_completeness", "content_completeness",
        "commercial_completeness", "category_completeness", "evidence_completeness",
        "research_completeness", "category_module", "taxonomy_status", "field_states",
        "missing_high_priority_fields", "missing_optional_fields", "category",
        "ingredient_completeness",
    }
    return {key: value for key, value in value.items() if key in allowed}


def _safe_understanding(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result = {
        key: value.get(key) for key in (
            "identity_status", "taxonomy_status", "category_module", "match_type",
        ) if value.get(key) is not None
    }
    for section in ("identity", "taxonomy"):
        source = value.get(section)
        if isinstance(source, dict):
            result[section] = {
                key: {field: decision.get(field) for field in ("value", "status") if decision.get(field) is not None}
                for key, decision in source.items() if isinstance(decision, dict)
            }
    return result or None


def external_product_detail(detail_model: Any) -> dict[str, Any]:
    """Return product business data without staff workflow or infrastructure metadata."""
    data = detail_model.model_dump(mode="json") if hasattr(detail_model, "model_dump") else dict(detail_model)
    top_level = {
        "id", "product_id", "product_variant_id", "internal_code", "product_name",
        "brand_name", "category_path", "product_category", "subcategory", "product_type",
        "gtin", "sku", "variant_name", "size", "unit", "variant_count", "image_url",
        "description", "review_status",
    }
    output = {key: data.get(key) for key in top_level}
    output["variants"] = [
        {key: row.get(key) for key in ("id", "variant_name", "gtin", "size", "unit")}
        for row in data.get("variants") or []
    ]
    output["formulations"] = [
        {key: row.get(key) for key in ("raw_inci_text", "market", "language", "effective_date")}
        for row in data.get("formulations") or []
    ]
    output["field_values"] = [
        {
            "id": f"field:{row.get('field_name')}", "field_name": row.get("field_name"),
            "value": row.get("value"), "source_type": _source_label(row.get("source_type")),
            "review_status": row.get("review_status"), "is_current": True,
            "semantic_status": row.get("semantic_status"),
        }
        for row in data.get("field_values") or []
        if row.get("is_current") and row.get("review_status") != "conflicting"
        and row.get("field_name") not in {"product_understanding", "identity_review_state", "schema_org"}
    ]
    output["source_attributes"] = [
        {key: row.get(key) for key in ("key", "label", "value", "source_header")}
        for row in data.get("source_attributes") or []
    ]
    output["ingredients"] = [
        {key: row.get(key) for key in (
            "name", "canonical_name", "position", "resolution_status", "resolution_method",
            "functions", "general_benefits", "caution_notes", "glossary_source", "glossary_source_url",
        )}
        for row in data.get("ingredients") or []
    ]
    output["key_ingredients"] = [
        {
            **{key: row.get(key) for key in (
                "name", "position", "functions", "benefits", "caution_notes",
                "is_key_ingredient", "key_ingredient_status",
            )},
            "evidence": _public_evidence(row.get("evidence")),
        }
        for row in data.get("key_ingredients") or []
    ]
    output["dynamic_concerns"] = [
        {key: row.get(key) for key in ("concern_name", "targeting_status", "confidence", "source")}
        for row in data.get("dynamic_concerns") or []
    ]
    output["market_observations"] = [
        {key: row.get(key) for key in (
            "source_name", "source_domain", "market", "price", "promotional_price", "currency",
            "availability", "rating", "review_count", "review_summary", "source_url", "observed_at",
        )}
        for row in data.get("market_observations") or []
    ]
    output["review_aggregate"] = _safe_review(data.get("review_aggregate"))
    output["product_understanding"] = _safe_understanding(data.get("product_understanding"))
    output["completeness"] = _safe_completeness(data.get("completeness"))
    # Staff-only collections are deliberately present as empty arrays so the
    # existing Product Detail renderer remains stable without exposing queues.
    output["validation_issues"] = []
    output["identity_review"] = None
    return output
