"""One category-aware definition of enrichment quality and research gaps.

This module is deliberately pure: API, worker, assistant, exports and PDF can
all evaluate the same product snapshot without duplicating applicability rules.
"""
from __future__ import annotations

import re
from typing import Any

UNKNOWN = {"", "unknown", "not provided", "not_provided", "none", "null", "nan", "unavailable"}
FACT_FIELDS = {"gtin", "size", "variant", "concentration", "inci", "top_notes", "heart_notes", "base_notes"}

CATEGORY_RULES = {
    "skincare": {
        "skin_types": "high", "texture": "medium", "finish": "medium", "inci": "high",
        "key_ingredients": "high", "targeted_concerns": "high",
    },
    "haircare": {
        "hair_types": "high", "texture_format": "medium", "inci": "high",
        "key_ingredients": "high", "targeted_concerns": "high",
    },
    "makeup": {
        "shade_colour": "medium", "coverage": "medium", "finish": "high",
        "texture_format": "medium", "inci": "medium",
    },
    "fragrance": {
        "concentration": "critical", "fragrance_family": "high", "top_notes": "high",
        "heart_notes": "high", "base_notes": "high", "longevity": "high",
        "sillage_projection": "high", "seasonal_fit": "medium", "occasion_fit": "medium",
    },
}
UNIVERSAL = {
    "brand": "critical", "product_name": "critical", "gtin": "high", "size": "medium",
    "category": "critical", "product_type": "high", "description": "high", "image_url": "high",
    "target_audience": "high", "product_positioning": "high", "benefits": "high",
    "directions": "high", "sensory_description": "medium", "claims": "optional",
}
WEIGHTS = {"critical": 4, "high": 3, "medium": 2, "optional": 1, "not_applicable": 0}


def present(value: Any) -> bool:
    if value is None or value == [] or value == {}:
        return False
    if isinstance(value, dict):
        value = value.get("value", value.get("values", value.get("text", value)))
        if isinstance(value, dict):
            return bool(value)
    return str(value).strip().lower() not in UNKNOWN


def category_module(snapshot: dict[str, Any]) -> str:
    text = " ".join(str(snapshot.get(key) or "") for key in ("category", "subcategory", "product_type")).lower()
    if any(term in text for term in ("fragrance", "perfume", "parfum", "eau de", "cologne")):
        return "fragrance"
    if any(term in text for term in ("hair", "shampoo", "conditioner", "scalp", "styling")):
        return "haircare"
    if any(term in text for term in ("makeup", "foundation", "concealer", "lip", "mascara", "eyeshadow", "blush")):
        return "makeup"
    return "skincare"


def _state(value: Any, metadata: dict[str, Any] | None = None) -> str:
    metadata = metadata or {}
    semantic = str(metadata.get("semantic_status") or "").lower()
    if semantic in {"conflicting", "not_found", "not_applicable", "not_researched"}:
        return semantic
    if not present(value):
        return "not_found" if metadata.get("researched") else "not_researched"
    if metadata.get("evidence") or metadata.get("source_type") in {"source_data", "human_edit", "retail_data"}:
        return "source_supported"
    return "inferred" if metadata.get("source_type") == "ai_inference" else "known"


def _value(snapshot: dict[str, Any], field: str, module: str) -> Any:
    if field == "brand": return snapshot.get("brand") or snapshot.get("brand_name")
    if field == "category": return snapshot.get("category") or snapshot.get("product_category") or snapshot.get("category_path")
    if field == "inci": return snapshot.get("inci") or snapshot.get("raw_inci_text")
    if field == "key_ingredients": return snapshot.get("key_ingredients") or snapshot.get("ingredients_intelligence")
    if field in snapshot: return snapshot.get(field)
    block = snapshot.get(module) if isinstance(snapshot.get(module), dict) else {}
    return block.get(field)


def evaluate_completeness(snapshot: dict[str, Any], metadata: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    metadata = metadata or {}
    module = category_module(snapshot)
    rules = {**UNIVERSAL, **CATEGORY_RULES[module]}
    # Concerns are valuable for skin/hair, optional for makeup, not applicable to fragrance.
    if module == "fragrance":
        rules["targeted_concerns"] = "not_applicable"
    elif module == "makeup":
        rules["targeted_concerns"] = "optional"
    fields = {}
    for name, priority in rules.items():
        value = _value(snapshot, name, module)
        state = "not_applicable" if priority == "not_applicable" else _state(value, metadata.get(name))
        fields[name] = {"priority": priority, "state": state, "value_present": present(value)}

    def score(names):
        applicable = [name for name in names if fields[name]["priority"] != "not_applicable"]
        total = sum(WEIGHTS[fields[name]["priority"]] for name in applicable)
        earned = sum(WEIGHTS[fields[name]["priority"]] for name in applicable
                     if fields[name]["state"] in {"known", "source_supported", "inferred"})
        return round(100 * earned / total) if total else 100

    identity_names = [name for name in ("brand", "product_name", "gtin", "size", "category", "product_type") if name in fields]
    content_names = [name for name in ("description", "image_url", "directions", "sensory_description") if name in fields]
    commercial_names = [name for name in ("target_audience", "product_positioning", "benefits", "targeted_concerns", "claims") if name in fields]
    category_names = list(CATEGORY_RULES[module])
    researched = [name for name, item in fields.items() if item["state"] != "not_researched" and item["priority"] != "not_applicable"]
    research_total = len([item for item in fields.values() if item["priority"] != "not_applicable"])
    evidence_names = [name for name, item in fields.items() if item["priority"] in {"critical", "high"}]
    evidence_supported = [name for name in evidence_names if fields[name]["state"] in {"known", "source_supported"}]
    missing_high = [name for name, item in fields.items()
                    if item["priority"] in {"critical", "high"} and item["state"] in {"not_found", "not_researched", "conflicting"}]
    missing_optional = [name for name, item in fields.items()
                        if item["priority"] in {"medium", "optional"} and item["state"] in {"not_found", "not_researched", "conflicting"}]
    return {
        "category_module": module, "overall_completeness": score(list(fields)),
        "identity_completeness": score(identity_names), "content_completeness": score(content_names),
        "commercial_completeness": score(commercial_names), "category_completeness": score(category_names),
        "evidence_completeness": round(100 * len(evidence_supported) / len(evidence_names)) if evidence_names else 100,
        "research_completeness": round(100 * len(researched) / research_total) if research_total else 100,
        "missing_high_priority_fields": missing_high, "missing_optional_fields": missing_optional,
        "field_states": fields,
    }


def build_gap_plan(snapshot: dict[str, Any], metadata: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    result = evaluate_completeness(snapshot, metadata)
    objectives = []
    for name in result["missing_high_priority_fields"]:
        objective_type = "fact" if name in FACT_FIELDS else "commercial"
        objectives.append({
            "field": name, "objective_type": objective_type,
            "requires_direct_evidence": name in FACT_FIELDS or name == "claims",
            "instruction": f"Find reliable product-specific evidence for {name.replace('_', ' ')}."
            if name in FACT_FIELDS or name == "claims" else
            f"Use product evidence to produce specific {name.replace('_', ' ')} intelligence.",
        })
    return {**result, "research_objectives": objectives, "should_research": bool(objectives)}


GENERIC_PHRASES = ("express your individuality", "beauty lovers", "people who like", "men who like", "women who like")
SENSORY_CLAIM_WORDS = ("fresh and clean", "refreshing scent", "invigorating scent", "luxurious scent")


def quality_gate(payload: dict[str, Any], module: str | None = None) -> tuple[dict[str, Any], list[str]]:
    """Remove unsafe/generic output without making a second model call."""
    data, rejected = dict(payload or {}), []
    module = module or category_module(data)
    audience = data.get("target_audience") or {}
    profiles = audience.get("value", []) if isinstance(audience, dict) else audience
    normalized = [re.sub(r"\W+", " ", str(item).lower()).strip() for item in profiles or []]
    if len(normalized) != 3 or len(set(normalized)) != 3 or any(any(p in item for p in GENERIC_PHRASES) for item in normalized):
        rejected.append("target_audience")
        if isinstance(audience, dict): audience["value"] = []
    claims = []
    for claim in data.get("claims") or []:
        name = str(claim.get("name") or "").lower() if isinstance(claim, dict) else str(claim).lower()
        if any(term in name for term in SENSORY_CLAIM_WORDS):
            rejected.append(f"claim:{name}")
            continue
        claims.append(claim)
    data["claims"] = claims
    if module == "fragrance":
        directions = data.get("directions") or {}
        text = str(directions.get("text") or directions.get("value") or "") if isinstance(directions, dict) else str(directions)
        if re.search(r"\b(morning|evening|routine step)\b", text, re.I):
            rejected.append("directions")
            data["directions"] = {
                "text": "Spray onto pulse points such as the wrists and neck. Reapply as desired.",
                "source_status": "inferred", "evidence": [], "confidence": 0.55,
            }
        data["targeted_concerns"] = {"values": [], "value_status": "not_applicable", "evidence": [],
                                        "reasoning_summary": "Targeted concerns are not applicable to fragrance.", "confidence": 1.0}
        data["routine_time"] = None
        data["routine_step"] = None
    return data, rejected
