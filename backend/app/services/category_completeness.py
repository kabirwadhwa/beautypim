"""One category-aware definition of enrichment quality and research gaps.

This module is deliberately pure: API, worker, assistant, exports and PDF can
all evaluate the same product snapshot without duplicating applicability rules.
"""
from __future__ import annotations

import re
from typing import Any

UNKNOWN = {"", "unknown", "not provided", "not_provided", "none", "null", "nan", "unavailable", "std", "standard", "c", "both", "n/a"}
FACT_FIELDS = {"gtin", "size", "variant", "concentration", "inci", "top_notes", "heart_notes", "base_notes"}

CATEGORY_RULES = {
    "unknown": {},
    "beauty_accessory": {
        "purpose": "high", "compatibility": "high", "material": "optional",
        "directions": "high", "care_instructions": "medium",
        "replacement_refill_status": "medium", "durability": "optional",
        "ergonomic_characteristics": "optional",
    },
    "skincare": {
        "skin_types": "high", "texture": "medium", "finish": "medium", "inci": "high",
        "targeted_concerns": "high",
    },
    "haircare": {
        "hair_types": "high", "texture_format": "medium", "inci": "high",
        "targeted_concerns": "high",
    },
    "makeup": {
        "shade_colour": "medium", "coverage": "medium", "finish": "high",
        "texture_format": "medium", "inci": "medium",
    },
    "fragrance": {
        "concentration": "critical", "fragrance_family": "high", "top_notes": "high",
        "heart_notes": "high", "base_notes": "high", "longevity": "high",
        "sillage_projection": "high", "seasonal_fit": "medium", "occasion_fit": "medium",
        # A fragrance formulation is still a product-specific fact.  It is
        # frequently absent from retail JSON-LD, but once the concentration and
        # variant are trusted we should actively research it rather than omit it
        # from the gap plan.  ``not_found`` remains valid; the model must never
        # invent an INCI merely to improve completeness.
        "inci": "high",
    },
}
UNIVERSAL = {
    "brand": "critical", "product_name": "critical", "gtin": "high", "size": "medium",
    "category": "critical", "product_type": "high", "description": "high", "image_url": "high",
    "target_audience": "high", "product_positioning": "high", "benefits": "high",
    "directions": "high", "sensory_description": "medium", "claims": "optional",
}
WEIGHTS = {"critical": 4, "high": 3, "medium": 2, "optional": 1, "not_applicable": 0}

MAKEUP_TOOL_TERMS = (
    "makeup tool", "make-up tool", "cosmetic tool", "accessory", "accessories",
    "accessoire", "accessoires", "brush", "penselen", "puff", "poederpuff",
    "sponge", "applicator", "beauty blender", "tweezer", "lash curler", "sharpener",
)


def present(value: Any) -> bool:
    if value is None or value == [] or value == {}:
        return False
    if isinstance(value, dict):
        value = value.get("value", value.get("values", value.get("text", value)))
        if isinstance(value, dict):
            return bool(value)
    return str(value).strip().lower() not in UNKNOWN


def category_module(snapshot: dict[str, Any]) -> str:
    understanding = snapshot.get("product_understanding") or {}
    authoritative = understanding.get("category_module") or snapshot.get("category_module")
    if authoritative in CATEGORY_RULES:
        return authoritative
    text = " ".join(str(snapshot.get(key) or "") for key in ("category", "subcategory", "product_type")).lower()
    if any(term in text for term in MAKEUP_TOOL_TERMS + ("eyelash curler", "curler pad", "replacement pad", "refill pad", "tweezers", "sharpener")):
        return "beauty_accessory"
    if any(term in text for term in ("fragrance", "perfume", "parfum", "eau de", "cologne")):
        return "fragrance"
    if any(term in text for term in ("hair", "shampoo", "conditioner", "scalp", "styling")):
        return "haircare"
    if any(term in text for term in ("makeup", "foundation", "concealer", "lip", "mascara", "eyeshadow", "blush")):
        return "makeup"
    if any(term in text for term in (
        "skincare", "skin care", "serum", "moistur", "cleanser", "toner",
        "body milk", "body lotion", "shower gel", "bath & shower", "bath and shower",
        "hand cream", "hand care", "body care", "duschgel", "körperpflege",
    )):
        return "skincare"
    return "unknown"


def _is_makeup_tool(snapshot: dict[str, Any]) -> bool:
    """Return true for applicators/accessories that do not have cosmetic formula attributes."""
    understanding = snapshot.get("product_understanding") or {}
    taxonomy = understanding.get("taxonomy") if isinstance(understanding.get("taxonomy"), dict) else {}
    text = " ".join(
        str(value or "") for value in (
            snapshot.get("product_name"), snapshot.get("category"), snapshot.get("subcategory"),
            snapshot.get("product_type"), taxonomy.get("category"), taxonomy.get("subcategory"),
            taxonomy.get("product_type"),
        )
    ).lower()
    return any(term in text for term in MAKEUP_TOOL_TERMS)


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
        if _is_makeup_tool(snapshot):
            # A puff/brush/applicator is correctly classified as makeup, but
            # shade, coverage, finish, INCI and treatment concerns genuinely do
            # not apply. Requiring them made Improve Product appear broken.
            for field in CATEGORY_RULES["makeup"]:
                rules[field] = "not_applicable"
            rules["targeted_concerns"] = "not_applicable"
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
    overall = score(list(fields))
    understanding = snapshot.get("product_understanding") or {}
    identity_status = understanding.get("identity_status")
    taxonomy_status = understanding.get("taxonomy_status")
    if identity_status in {"unresolved", "conflicting"}:
        overall = min(overall, 55)
    elif identity_status == "partial":
        # Commercial prose must not conceal weak foundational identity.
        overall = min(overall, 75)
    elif module == "unknown" or taxonomy_status == "needs_review":
        # Taxonomy uncertainty must be visible without rewriting an exact,
        # already-resolved product identity as unresolved.  Keep this below a
        # resolved category's honest score so resolving taxonomy cannot appear
        # to make the dossier worse merely by activating applicable fields.
        overall = min(overall, 65)
    return {
        "category_module": module, "taxonomy_status": taxonomy_status or ("resolved" if module != "unknown" else "needs_review"), "overall_completeness": overall,
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
    understanding = snapshot.get("product_understanding") or {}
    foundational_objectives = list((understanding.get("research_plan") or {}).get("objectives") or [])
    identity_fields = {"consumer_brand", "product_family", "variant", "gtin"}
    taxonomy_fields = {"category", "subcategory", "product_type", "application_area"}
    identity_objectives = [name for name in foundational_objectives if name in identity_fields]
    taxonomy_objectives = [name for name in foundational_objectives if name in taxonomy_fields]
    if (result["category_module"] == "unknown" or not present(_value(snapshot, "brand", result["category_module"]))
            or not present(_value(snapshot, "product_name", result["category_module"]))) and understanding.get("identity_status") != "resolved" and not identity_objectives:
        identity_objectives = ["consumer_brand", "product_family", "variant"]
    if result["category_module"] == "unknown" and not taxonomy_objectives:
        taxonomy_objectives = ["category", "subcategory", "product_type", "application_area"]
    identity_plan = [{
        "field": name, "objective_type": "identity", "requires_direct_evidence": True,
        "instruction": f"Resolve {name.replace('_', ' ')} before category enrichment.",
    } for name in identity_objectives]
    taxonomy_plan = [{
        "field": name, "objective_type": "taxonomy", "requires_direct_evidence": True,
        "instruction": f"Resolve {name.replace('_', ' ')} before category enrichment.",
    } for name in taxonomy_objectives]
    identity_required = bool(identity_plan)
    taxonomy_required = not identity_required and bool(taxonomy_plan)
    active_plan = identity_plan if identity_required else taxonomy_plan if taxonomy_required else objectives
    return {
        **result,
        "phase": "identity_resolution" if identity_required else "taxonomy_resolution" if taxonomy_required else "attribute_completion",
        # This is deliberately not a flat plan: downstream work is operationally
        # blocked until Product Understanding is recalculated after phase one.
        "research_objectives": active_plan,
        "blocked_objectives": objectives if (identity_required or taxonomy_required) else [],
        "identity_resolution_required": identity_required,
        "taxonomy_resolution_required": taxonomy_required,
        "should_research": bool(active_plan),
    }


GENERIC_PHRASES = ("express your individuality", "beauty lovers", "people who like", "men who like", "women who like")
SENSORY_CLAIM_WORDS = ("fresh and clean", "refreshing scent", "invigorating scent", "luxurious scent")


def quality_gate(payload: dict[str, Any], module: str | None = None,
                 identity_text: str = "") -> tuple[dict[str, Any], list[str]]:
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
        if isinstance(claim, dict):
            affirmative = str(claim.get("value") or "").strip().lower() in {"yes", "true", "verified", "1"}
            has_direct_evidence = bool(claim.get("evidence")) and str(claim.get("status") or "").lower() in {
                "verified", "source_supported", "explicit_source",
            }
            if affirmative and not has_direct_evidence:
                claim = {**claim, "value": "Unknown", "status": "unverified", "confidence": 0.0}
                rejected.append(f"unsupported_claim:{name}")
        claims.append(claim)
    data["claims"] = claims
    concerns = data.get("targeted_concerns") or []
    wrapped = isinstance(concerns, dict)
    concern_key = "values" if wrapped and "values" in concerns else "value"
    values = concerns.get(concern_key, []) if wrapped else concerns
    values = list(values) if isinstance(values, list) else ([values] if values else [])
    if module == "fragrance":
        if values:
            rejected.append("targeted_concerns:not_applicable")
        values = []
    elif module == "makeup":
        safe = []
        for value in values:
            lowered = str(value).lower()
            if any(term in lowered for term in ("pigmentation", "hyperpigmentation", "dark spot", "acne", "redness")):
                rejected.append(f"makeup_concern_collision:{value}")
                continue
            if lowered in {"dehydration", "dryness", "dry skin"}:
                rejected.append(f"makeup_concern_collision:{value}")
                continue
            safe.append(value)
        # Colour/pigment vocabulary is makeup performance, never a skincare diagnosis.
        if not safe and re.search(r"\b(?:pigment|pigmented|colour payoff|color payoff)\b", identity_text, re.I):
            safe.append("Colour payoff")
        values = safe
    elif module == "skincare":
        # Pigment alone describes colour cosmetics; pigmentation requires treatment context.
        if not re.search(r"\b(?:dark spots?|uneven tone|hyperpigmentation|discolou?ration)\b", identity_text, re.I):
            values = [v for v in values if str(v).lower() != "pigmentation"]
    data["targeted_concerns"] = ({**concerns, concern_key: values} if wrapped else values)
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
