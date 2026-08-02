import json
import requests
import hashlib
import uuid
import re
from typing import Dict, Any, Tuple, Optional
from sqlalchemy.orm import Session
from app.config import settings
from app.schemas import BeautyProductEnrichmentSchema
from app.models import EnrichmentRun
from app.services.ingredient_knowledge import (
    build_ingredient_grounding_context,
    ground_fallback_ingredients,
    retrieve_ingredient_knowledge,
)

BALANCED_INFERENCE_GUIDANCE = (
    "Return a useful answer for every catalogue field. For classification fields such as subcategory, product type, "
    "gender target, texture and application area, make a reasonable inference "
    "when the product title or description strongly implies one. For concern targeting, skin or "
    "hair fit, benefits, directions and fragrance intelligence, infer typical values when they "
    "are reasonably supported by the product type, wording or ingredient functions. Mark these "
    "as inferred and normally use confidence between 0.55 and 0.79; use 0.80 or higher for direct "
    "source statements or exact reference matches. Populate each schema field with the best "
    "supported answer. If the source is sparse, make a transparent best-fit catalogue inference "
    "instead of returning unknown or not provided. A "
    "plausible inference must never be worded as a verified brand claim. Ethical claims, "
    "free-from claims, safety, medical conclusions and legal compliance still require explicit "
    "support. The presence of Parfum/Fragrance may support fragrance_present=yes, but ingredient "
    "absence alone does not prove a free-from claim. For an unsupported ethical or free-from "
    "claim return value=unverified and claim_status=unverified, never a guessed yes/no. "
    "For target_audience return exactly three distinct customer-profile sentences. Each must combine "
    "a concrete need, routine, preference or usage occasion with the relevant product characteristic, "
    "so it can guide advertising or in-store recommendations. Never use generic labels such as adults, "
    "beauty lovers or everyone, and never infer sensitive personal traits. "
    "The product dossier is catalogue-backed: enrich brand origin, manufacturing country, launch year, "
    "positioning, colour, finish, absorption, sensory profile, routine timing and sequence, credentials, "
    "testing claims, targeted concerns, technology, scored skin fit and ingredient statistics as structured "
    "fields. Infer merchandising and sensory values at moderate confidence where comparable retail knowledge "
    "supports them. Testing, origin, launch year, regulatory and free-from claims require explicit evidence; "
    "return unverified rather than inventing them. Never invent technology image URLs. "
)

DOSSIER_CATEGORICAL_FIELDS = (
    "brand_origin", "country_of_manufacture", "launch_year", "product_positioning",
    "colour", "finish", "absorption_profile", "sensory_description", "routine_time",
    "routine_step", "application_sequence", "regulatory_notes",
)
DOSSIER_CLAIM_FIELDS = (
    "phthalate_free", "dermatologically_tested", "clinically_tested",
    "ophthalmologically_tested",
)
DOSSIER_STRUCTURED_FIELDS = (
    "product_credentials", "targeted_concerns", "proprietary_technologies",
    "skin_type_scores", "inci_stats",
)

UNKNOWN_VALUES = {"", "unknown", "none", "null", "nan", "not provided", "not_provided"}


def normalize_null_confidences(payload: Any) -> Any:
    """Repair a common provider JSON defect before strict schema validation."""
    if isinstance(payload, dict):
        return {
            key: (0.5 if key == "confidence" and value is None else normalize_null_confidences(value))
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [normalize_null_confidences(item) for item in payload]
    return payload


def _evidence_list(value: Any) -> list[dict[str, Any]]:
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        return [{
            "source_reference": None,
            "source_field": "provider_reasoning",
            "supporting_text": value,
            "evidence_type": "provider_summary",
            "char_offsets": None,
        }]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    normalized = []
    for item in value:
        if isinstance(item, str):
            normalized.extend(_evidence_list(item))
        elif isinstance(item, dict):
            normalized.append({
                "source_reference": item.get("source_reference"),
                "source_field": str(item.get("source_field") or "provider_reasoning"),
                "supporting_text": str(item.get("supporting_text") or item.get("text") or item),
                "evidence_type": str(item.get("evidence_type") or "provider_summary"),
                "char_offsets": item.get("char_offsets"),
            })
    return normalized


def normalize_provider_shapes(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce recurring LLM JSON shape drift into the declared PIM schema."""
    categorical_fields = (
        "subcategory", "product_type", "gender_target", "texture",
        "application_area", "target_audience",
    ) + DOSSIER_CATEGORICAL_FIELDS
    claim_fields = (
        "vegan", "cruelty_free", "paraben_free", "sulfate_free",
        "silicone_free", "alcohol_free", "fragrance_present",
    ) + DOSSIER_CLAIM_FIELDS
    concern_fields = (
        "hydration", "anti_ageing", "pigmentation", "acne", "redness",
        "sensitivity", "scalp_care", "hair_growth", "fragrance", "freshness",
    )
    evidence_fields = categorical_fields + claim_fields + concern_fields + (
        "directions", "skin_type_fit", "hair_type_fit", "fragrance_intelligence",
        "pregnancy_warning_observation", "allergen_warning_observation",
        "sensitivity_warning_observation",
    )
    for field in evidence_fields:
        item = payload.get(field)
        if isinstance(item, dict):
            item["evidence"] = _evidence_list(item.get("evidence"))
    for field in categorical_fields:
        item = payload.get(field)
        if not isinstance(item, dict):
            continue
        item.setdefault("value_status", "inferred" if item.get("value") else "unknown")
        item.setdefault("reasoning_summary", "Provider inference normalized to the catalogue schema.")
        item.setdefault("confidence", 0.65 if item.get("value") else 0.5)
    for benefit in payload.get("benefits") or []:
        if not isinstance(benefit, dict):
            continue
        evidence = benefit.get("evidence")
        if isinstance(evidence, list):
            benefit["evidence"] = "; ".join(
                str(item.get("supporting_text") or item) if isinstance(item, dict) else str(item)
                for item in evidence
            )
        elif evidence is None:
            benefit["evidence"] = ""
    for ingredient in payload.get("ingredients_intelligence") or []:
        if not isinstance(ingredient, dict):
            continue
        ingredient["evidence"] = _evidence_list(ingredient.get("evidence"))
        concerns = ingredient.get("possible_concerns") or []
        ingredient["possible_concerns"] = [
            item if isinstance(item, dict) else {"statement": str(item)}
            for item in concerns
        ]
    for field in DOSSIER_STRUCTURED_FIELDS:
        item = payload.get(field)
        if isinstance(item, dict):
            item["evidence"] = _evidence_list(item.get("evidence"))
    technology = payload.get("proprietary_technologies") or {}
    for item in technology.get("items") or []:
        if isinstance(item, dict):
            item.setdefault("name", "Product technology")
            item.setdefault("description", "Technology referenced by the enrichment provider.")
            item.setdefault("related_ingredients", [])
            item.setdefault("image_url", None)
            item.setdefault("source_status", "inferred")
    scores = (payload.get("skin_type_scores") or {}).get("scores")
    if isinstance(scores, dict):
        normalized_scores = {}
        for key, value in scores.items():
            try:
                normalized_scores[str(key)] = max(0, min(5, int(value or 0)))
            except (TypeError, ValueError):
                normalized_scores[str(key)] = 0
        payload["skin_type_scores"]["scores"] = normalized_scores
    return payload


def _is_missing_field(payload: Any, key: str) -> bool:
    if not isinstance(payload, dict):
        return True
    value = payload.get(key)
    return value is None or str(value).strip().lower() in UNKNOWN_VALUES


def ensure_catalogue_coverage(
    data: Dict[str, Any],
    name: str,
    brand: str,
    description: str,
    raw_ingredients: str,
) -> Dict[str, Any]:
    """Fill safe catalogue gaps while preserving supported model output.

    Product classification and merchandising attributes can be transparently
    inferred. Ethical/free-from claims remain explicitly unverified rather than
    being fabricated.
    """
    fallback = generate_deterministic_fallback(name, brand, description, raw_ingredients)
    categorical_fields = (
        "subcategory", "product_type", "gender_target", "texture",
        "application_area",
    ) + DOSSIER_CATEGORICAL_FIELDS
    for field in categorical_fields:
        if _is_missing_field(data.get(field), "value"):
            data[field] = fallback[field]

    audience = (data.get("target_audience") or {}).get("value")
    if not isinstance(audience, list) or len([item for item in audience if str(item).strip()]) != 3:
        data["target_audience"] = fallback["target_audience"]

    for field in (
        "vegan", "cruelty_free", "paraben_free", "sulfate_free",
        "silicone_free", "alcohol_free", "fragrance_present",
    ) + DOSSIER_CLAIM_FIELDS:
        payload = data.get(field) or {}
        if _is_missing_field(payload, "value"):
            data[field] = fallback[field]

    for field in (
        "hydration", "anti_ageing", "pigmentation", "acne", "redness",
        "sensitivity", "scalp_care", "hair_growth", "fragrance", "freshness",
    ):
        if _is_missing_field(data.get(field), "targeting_status"):
            data[field] = fallback[field]

    for field in ("benefits", "directions", "skin_type_fit", "hair_type_fit", "fragrance_intelligence"):
        payload = data.get(field)
        if payload is None or payload == [] or (
            field == "directions" and _is_missing_field(payload, "text")
        ) or (
            field == "fragrance_intelligence"
            and _is_missing_field(payload, "fragrance_presence_status")
        ):
            data[field] = fallback[field]
    for field in DOSSIER_STRUCTURED_FIELDS:
        payload = data.get(field)
        incomplete = not payload
        if field in {"product_credentials", "targeted_concerns"} and isinstance(payload, dict):
            incomplete = not payload.get("values")
        elif field == "skin_type_scores" and isinstance(payload, dict):
            incomplete = not payload.get("scores")
        elif field == "inci_stats" and isinstance(payload, dict):
            incomplete = bool(raw_ingredients) and not payload.get("total_ingredients")
        if incomplete:
            data[field] = fallback[field]
    return data

def normalize_and_validate_enrichment(data: Dict[str, Any], raw_ingredients: str) -> Dict[str, Any]:
    """Normalize provider vocabulary and reject ingredient observations not in source."""
    positive = {"explicit", "inferred", "targeted", "yes", "true"}
    negative = {"not_targeted", "not targeted", "no", "false", "unknown", "not_applicable"}
    for field in (
        "hydration", "anti_ageing", "pigmentation", "acne", "redness",
        "sensitivity", "scalp_care", "hair_growth", "fragrance", "freshness",
    ):
        payload = data.get(field) or {}
        raw_status = str(payload.get("targeting_status", "unknown")).strip().lower()
        if raw_status in positive:
            payload["targeting_status"] = "explicit" if raw_status == "explicit" else "inferred"
        elif raw_status in negative:
            payload["targeting_status"] = "not_targeted" if raw_status not in {"unknown", "not_applicable"} else raw_status
        else:
            payload["targeting_status"] = "unknown"

    ingredient_text = (raw_ingredients or "").lower()
    ingredient_names = {
        re.sub(r"\s+", " ", part.strip())
        for part in re.split(r"[,;\n]", ingredient_text)
        if part.strip()
    }
    pregnancy = data.get("pregnancy_warning_observation") or {}
    observed = [str(item).strip().lower() for item in pregnancy.get("observed_items", [])]
    verified = [item for item in observed if item and item in ingredient_text]
    retinoids = ("retinol", "retinal", "retinaldehyde", "retinyl palmitate", "retinyl acetate")
    verified_retinoids = [
        name for name in retinoids
        if any(candidate == name or candidate.startswith(f"{name} (") for candidate in ingredient_names)
    ]
    if verified_retinoids:
        pregnancy.update({
            "review_required": True,
            "observation_type": "retinoid_present",
            "observed_items": verified_retinoids,
            "review_message": (
                f"Contains {', '.join(verified_retinoids)}. "
                "Factual review required; no safety conclusion is made."
            ),
            "confidence": 1.0,
        })
    elif not verified:
        pregnancy.update({
            "review_required": False,
            "observation_type": "none_observed",
            "observed_items": [],
            "review_message": "No pregnancy-related ingredient observation was verified from the supplied list.",
            "confidence": 1.0,
            "evidence": [],
        })
    else:
        pregnancy["observed_items"] = verified
    data["pregnancy_warning_observation"] = pregnancy
    return data

def calculate_token_count_rough(text: str) -> int:
    # Basic rough calculation: ~4 chars per token
    return len(text) // 4

def generate_deterministic_fallback(
    name: str,
    brand: str,
    description: str,
    raw_ingredients: str
) -> Dict[str, Any]:
    """Run balanced deterministic extraction when no model is available.

    Direct keyword matches remain explicit. Safe catalogue classifications may
    be inferred at moderate confidence so a temporary provider outage does not
    turn an otherwise useful product record into a page of unknown values.
    """
    evidence = []
    
    # 1. Ingredients splitting
    ingredients_list = []
    if raw_ingredients:
        # Split by comma or semicolon
        delims = [",", ";"]
        parts = [raw_ingredients]
        for d in delims:
            temp = []
            for p in parts:
                temp.extend(p.split(d))
            parts = temp
        
        for idx, part in enumerate(parts):
            clean_part = part.strip()
            if clean_part:
                ingredients_list.append({
                    "ingredient_name": clean_part,
                    "normalized_inci_name": None,
                    "common_name": None,
                    "source_origin": None,
                    "inci_position": idx + 1,
                    "ingredient_group": None,
                    "short_description": "Ingredient retained from the supplied INCI list.",
                    "other_utility": None,
                    "functions": [],
                    "benefits": [],
                    "possible_concerns": [],
                    "is_key_ingredient": False,
                    "key_ingredient_status": "unknown",
                    "evidence": [],
                    "confidence": 0.0
                })

    # 2. Exact Claim Detection (Deterministic search only)
    desc_lower = description.lower()
    name_lower = name.lower()
    
    def detect_simple_claim(keyword: str, field_name: str) -> Dict[str, Any]:
        if keyword in desc_lower or keyword in name_lower:
            evidence_text = f"Found keyword '{keyword}' in source data."
            return {
                "value": "yes",
                "claim_status": "explicit_brand_claim",
                "evidence": [{
                    "source_reference": None,
                    "source_field": "description" if keyword in desc_lower else "title",
                    "supporting_text": evidence_text,
                    "evidence_type": "keyword_match",
                    "char_offsets": None
                }],
                "reasoning_summary": f"Detected claim by keyword '{keyword}'.",
                "confidence": 0.95
            }
        return {
            "value": "unverified",
            "claim_status": "unverified",
            "evidence": [],
            "reasoning_summary": f"No explicit '{keyword}' claim was supplied; no yes/no claim is inferred.",
            "confidence": 1.0
        }

    # Helper for simple categorizations
    def make_unknown_categorical() -> Dict[str, Any]:
        return {
            "value": None,
            "value_status": "unknown",
            "evidence": [],
            "reasoning_summary": "Unprocessed. Requires AI model enrichment.",
            "confidence": 0.0
        }

    def make_inferred_categorical(value: str, reason: str, confidence: float = 0.62) -> Dict[str, Any]:
        return {
            "value": value,
            "value_status": "inferred",
            "evidence": [{
                "source_reference": None,
                "source_field": "title/description",
                "supporting_text": reason,
                "evidence_type": "catalogue_rule_inference",
                "char_offsets": None
            }],
            "reasoning_summary": reason,
            "confidence": confidence
        }

    def make_unknown_concern() -> Dict[str, Any]:
        return {
            "targeting_status": "unknown",
            "evidence": [],
            "reasoning_summary": "Unprocessed. Requires AI model enrichment.",
            "confidence": 0.0
        }

    source_text = f"{name} {description}".lower()

    def detect_categorical(rules: list[tuple[str, str]], field_name: str) -> Dict[str, Any]:
        for keyword, value in rules:
            if keyword in source_text:
                return {
                    "value": value,
                    "value_status": "explicit_source",
                    "evidence": [{
                        "source_reference": None,
                        "source_field": "title/description",
                        "supporting_text": f"Found '{keyword}' in source title or description.",
                        "evidence_type": "keyword_match",
                        "char_offsets": None
                    }],
                    "reasoning_summary": f"Mapped {field_name} from the explicit source keyword '{keyword}'.",
                    "confidence": 0.9
                }
        return make_inferred_categorical(
            "beauty product",
            f"No precise {field_name} signal was supplied; assigned the broad catalogue fallback.",
            0.51,
        )

    def detect_concern(keywords: list[str]) -> Dict[str, Any]:
        for keyword in keywords:
            if keyword in source_text:
                return {
                    "targeting_status": "explicit",
                    "evidence": [{
                        "source_reference": None,
                        "source_field": "title/description",
                        "supporting_text": f"Found '{keyword}' in source title or description.",
                        "evidence_type": "keyword_match",
                        "char_offsets": None
                    }],
                    "reasoning_summary": f"The source explicitly mentions '{keyword}'.",
                    "confidence": 0.9
                }
        return {
            "targeting_status": "not_targeted",
            "evidence": [],
            "reasoning_summary": "No source or product-type signal indicates this concern is targeted.",
            "confidence": 0.55
        }

    product_type = detect_categorical([
        ("cleanser", "cleanser"), ("face wash", "cleanser"), ("serum", "serum"),
        ("moisturizer", "moisturizer"), ("moisturiser", "moisturizer"),
        ("cream", "cream"), ("lotion", "lotion"), ("toner", "toner"),
        ("shampoo", "shampoo"), ("conditioner", "conditioner"),
        ("mascara", "mascara"), ("lipstick", "lipstick"), ("fragrance", "fragrance"),
        ("perfume", "fragrance"), ("eau de parfum", "fragrance"), ("foundation", "foundation"),
        ("concealer", "concealer"), ("sunscreen", "sunscreen"), ("spf", "sunscreen"),
        ("deodorant", "deodorant"), ("body wash", "body wash"), ("mask", "mask")
    ], "product type")
    texture = detect_categorical([
        ("gel", "gel"), ("cream", "cream"), ("lotion", "lotion"),
        ("oil", "oil"), ("balm", "balm"), ("foam", "foam"), ("spray", "spray")
    ], "texture")
    application_area = detect_categorical([
        ("scalp", "scalp"), ("hair", "hair"), ("eye", "eye area"),
        ("lip", "lips"), ("face", "face"), ("body", "body")
    ], "application area")

    inferred_type = product_type.get("value")
    if application_area.get("value") == "beauty product" and inferred_type:
        area_by_type = {
            "cleanser": "face", "serum": "face", "moisturizer": "face",
            "toner": "face", "foundation": "face", "concealer": "face",
            "sunscreen": "face/body", "mascara": "eye area", "lipstick": "lips",
            "shampoo": "hair/scalp", "conditioner": "hair", "deodorant": "underarms",
            "body wash": "body", "fragrance": "body",
        }
        inferred_area = area_by_type.get(inferred_type)
        if inferred_area:
            application_area = make_inferred_categorical(
                inferred_area, f"Application area inferred from product type '{inferred_type}'."
            )

    gender_target = detect_categorical([
        ("for men", "men"), ("men's", "men"), ("for women", "women"),
        ("women's", "women"), ("unisex", "unisex")
    ], "gender target")
    if gender_target.get("value") == "beauty product":
        gender_target = make_inferred_categorical(
            "unisex",
            "No gender restriction is stated; the catalogue default is unisex.",
            0.55
        )

    audience_type = inferred_type or "beauty product"
    audience_area = application_area.get("value") or "beauty routine"
    need = next((label.lower() for label, field in [
        ("hydration", detect_concern(["hydrat", "moistur"])),
        ("visible signs of ageing", detect_concern(["anti-age", "wrinkle"])),
        ("uneven tone", detect_concern(["pigmentation", "dark spot", "brightening"])),
        ("blemishes", detect_concern(["acne", "blemish", "breakout"])),
        ("sensitivity and comfort", detect_concern(["sensitive", "soothing"])),
    ] if field["targeting_status"] == "explicit"), "everyday care")
    target_audience = make_inferred_categorical(
        [
            f"Customers seeking {need} support in their {audience_area} routine.",
            f"Shoppers who prefer a {texture.get('value') or 'practical'} {audience_type} for regular use.",
            f"Customers comparing {audience_type} options and wanting a clear, easy-to-integrate routine step.",
        ],
        "Three merchandising profiles inferred from product type, application area, texture and stated needs.",
        0.62,
    )

    if texture.get("value") == "beauty product" and inferred_type in {"serum", "toner", "shampoo", "conditioner"}:
        texture_defaults = {
            "serum": "serum/liquid", "toner": "liquid",
            "shampoo": "liquid/gel", "conditioner": "cream"
        }
        texture = make_inferred_categorical(
            texture_defaults[inferred_type],
            f"Typical texture inferred from product type '{inferred_type}'.",
            0.58
        )
    elif texture.get("value") == "beauty product":
        texture = make_inferred_categorical(
            "standard formulation",
            f"Texture is not explicit; a neutral merchandising value was assigned for '{inferred_type}'.",
            0.51,
        )

    if application_area.get("value") == "beauty product":
        application_area = make_inferred_categorical(
            "face and body",
            "No narrower application area is stated; assigned a broad beauty-use area.",
            0.51,
        )

    hydration = detect_concern(["hydrat", "moistur"])
    anti_ageing = detect_concern(["anti-age", "anti age", "anti-wrinkle", "wrinkle"])
    pigmentation = detect_concern(["pigmentation", "dark spot", "brightening"])
    acne = detect_concern(["acne", "blemish", "breakout"])
    redness = detect_concern(["redness", "red skin"])
    sensitivity = detect_concern(["sensitive skin", "sensitivity", "soothing"])
    scalp_care = detect_concern(["scalp"])
    hair_growth = detect_concern(["hair growth", "thinning hair"])
    fragrance = detect_concern(["fragrance", "perfume", "parfum"])
    freshness = detect_concern(["freshness", "refreshing"])

    benefits = []
    for label, field in [
        ("Hydration support", hydration), ("Anti-ageing targeting", anti_ageing),
        ("Brightening or pigmentation targeting", pigmentation), ("Blemish targeting", acne),
        ("Soothing or sensitivity targeting", sensitivity), ("Scalp care", scalp_care),
        ("Freshness", freshness),
    ]:
        if field["targeting_status"] == "explicit":
            benefits.append({
                "statement": label,
                "source_type": "source_claim",
                "evidence": field["evidence"][0]["supporting_text"],
                "confidence": field["confidence"]
            })
    if not benefits:
        benefit_by_type = {
            "cleanser": "Helps cleanse the skin",
            "serum": "Supports a targeted daily beauty routine",
            "moisturizer": "Helps maintain skin comfort and moisture",
            "cream": "Helps condition and soften the application area",
            "lotion": "Helps condition and soften the application area",
            "toner": "Helps prepare skin for the next routine step",
            "shampoo": "Helps cleanse hair and scalp",
            "conditioner": "Helps condition and soften hair",
            "mascara": "Helps define the lashes",
            "lipstick": "Adds colour and definition to the lips",
            "foundation": "Helps create a more even-looking complexion",
            "concealer": "Helps visually reduce the appearance of uneven tone",
            "sunscreen": "Supports daily sun-care application",
            "fragrance": "Provides a personal fragrance experience",
            "deodorant": "Supports everyday freshness",
            "body wash": "Helps cleanse the body",
            "mask": "Supports a focused beauty-care step",
            "beauty product": "Supports an everyday beauty and personal-care routine",
        }
        benefits.append({
            "statement": benefit_by_type.get(inferred_type, "Supports an everyday beauty and personal-care routine"),
            "source_type": "catalogue_inference",
            "evidence": f"General benefit inferred from product type '{inferred_type}'.",
            "confidence": 0.52,
        })

    directions_by_type = {
        "cleanser": "Apply to damp skin, gently massage, then rinse.",
        "serum": "Apply a small amount to clean skin.",
        "moisturizer": "Apply to clean skin and massage until absorbed.",
        "toner": "Apply to clean skin before serum or moisturizer.",
        "shampoo": "Apply to wet hair and scalp, massage, then rinse.",
        "conditioner": "Apply to hair lengths after shampooing, then rinse.",
        "sunscreen": "Apply evenly before sun exposure and reapply as needed.",
    }
    inferred_directions = directions_by_type.get(
        inferred_type,
        "Use as directed on the product packaging for this product type.",
    )

    finish_by_type = {
        "foundation": "natural cosmetic finish", "concealer": "natural cosmetic finish",
        "lipstick": "colour finish", "serum": "lightweight finish",
        "moisturizer": "comfortable moisturised finish", "cream": "comfortable moisturised finish",
        "sunscreen": "protective skincare finish", "fragrance": "fragranced finish",
    }
    absorption_by_type = {
        "serum": "designed to layer within a skincare routine",
        "moisturizer": "designed to absorb with gentle massage",
        "cream": "designed to absorb with gentle massage",
        "lotion": "spreadable and designed for routine absorption",
        "oil": "emollient application with a conditioning after-feel",
    }
    routine_step_by_type = {
        "cleanser": "cleanse", "toner": "tone and prepare", "serum": "targeted treatment",
        "moisturizer": "moisturise", "cream": "moisturise", "sunscreen": "daytime protection",
        "shampoo": "cleanse hair", "conditioner": "condition after shampooing",
    }
    routine_step = routine_step_by_type.get(inferred_type, "product-specific routine step")
    targeted_labels = [label for label, concern in (
        ("Dryness and dehydration", hydration), ("Fine lines and visible ageing", anti_ageing),
        ("Uneven tone and pigmentation", pigmentation), ("Blemishes and congestion", acne),
        ("Redness", redness), ("Sensitivity and comfort", sensitivity),
        ("Scalp care", scalp_care), ("Hair density concerns", hair_growth),
        ("Freshness", freshness),
    ) if concern["targeting_status"] in {"explicit", "inferred"}]
    if not targeted_labels:
        targeted_labels = [f"Everyday {audience_type} maintenance"]

    ingredient_names_lower = [str(item["ingredient_name"]).lower() for item in ingredients_list]
    def count_terms(*terms: str) -> int:
        return sum(any(term in ingredient for term in terms) for ingredient in ingredient_names_lower)
    inci_stats = {
        "total_ingredients": len(ingredients_list),
        "allergen_count": count_terms("limonene", "linalool", "citral", "geraniol", "eugenol", "coumarin"),
        "fragrance_count": count_terms("parfum", "fragrance", "aroma"),
        "plant_extracts": count_terms("extract", "leaf", "root", "flower", "seed"),
        "peptides": count_terms("peptide"),
        "antioxidants": count_terms("tocopher", "ascorb", "vitamin e", "ferulic"),
        "humectants": count_terms("glycerin", "hyaluron", "propanediol", "butylene glycol"),
        "emollients_oils": count_terms("oil", "butter", "squalane", "triglyceride"),
        "preservatives": count_terms("phenoxyethanol", "benzoate", "sorbate", "paraben"),
        "evidence": [],
        "confidence": 0.8 if ingredients_list else 0.5,
    }

    return {
        "subcategory": product_type,
        "product_type": product_type,
        "gender_target": gender_target,
        "texture": texture,
        "application_area": application_area,
        "target_audience": target_audience,
        "brand_origin": make_inferred_categorical("Unverified", "Brand origin requires an explicit source.", 0.5),
        "country_of_manufacture": make_inferred_categorical("Unverified", "Manufacturing country requires an explicit source.", 0.5),
        "launch_year": make_inferred_categorical("Unverified", "Launch year requires an explicit source.", 0.5),
        "product_positioning": make_inferred_categorical(
            f"{audience_type.title()} for {need}", "Merchandising positioning inferred from product type and stated need.", 0.6
        ),
        "colour": make_inferred_categorical("Product dependent", "Colour is not explicit in the supplied source.", 0.5),
        "finish": make_inferred_categorical(
            finish_by_type.get(inferred_type, "product-appropriate finish"),
            "Finish inferred conservatively from product type.", 0.56,
        ),
        "absorption_profile": make_inferred_categorical(
            absorption_by_type.get(inferred_type, "application profile appropriate to product format"),
            "Absorption and after-feel inferred from product format.", 0.55,
        ),
        "sensory_description": make_inferred_categorical(
            f"{(texture.get('value') or 'cosmetic').title()} format with a {finish_by_type.get(inferred_type, 'product-appropriate finish')}.",
            "Sensory summary inferred from texture and product type.", 0.57,
        ),
        "routine_time": make_inferred_categorical(
            "morning and/or evening as appropriate", "Routine timing inferred from product type; packaging directions take precedence.", 0.55
        ),
        "routine_step": make_inferred_categorical(routine_step, "Routine step inferred from product type.", 0.62),
        "application_sequence": make_inferred_categorical(
            inferred_directions, "Sequence inferred from standard use of this product type.", 0.55
        ),
        "regulatory_notes": make_inferred_categorical(
            "Follow market-specific packaging directions and warnings.",
            "No product-specific regulatory statement was supplied.", 0.5,
        ),
        "product_credentials": {
            "values": [f"{audience_type.title()} category", f"{need.title()} positioning"],
            "value_status": "inferred", "evidence": [],
            "reasoning_summary": "Merchandising credentials inferred from the product profile.", "confidence": 0.58,
        },
        "targeted_concerns": {
            "values": targeted_labels, "value_status": "inferred", "evidence": [],
            "reasoning_summary": "Concern list consolidated from the structured concern fields.", "confidence": 0.62,
        },
        "proprietary_technologies": {"items": [], "evidence": [], "confidence": 0.5},
        "skin_type_scores": {
            "scores": {name: 3 for name in ("normal", "dry", "very_dry", "combination", "oily", "sensitive")},
            "evidence": [], "reasoning_summary": "Neutral fit scores pending product-specific evidence.", "confidence": 0.5,
        },
        "inci_stats": inci_stats,
        
        "vegan": detect_simple_claim("vegan", "vegan"),
        "cruelty_free": detect_simple_claim("cruelty-free", "cruelty_free"),
        "paraben_free": detect_simple_claim("paraben-free", "paraben_free"),
        "sulfate_free": detect_simple_claim("sulfate-free", "sulfate_free"),
        "silicone_free": detect_simple_claim("silicone-free", "silicone_free"),
        "alcohol_free": detect_simple_claim("alcohol-free", "alcohol_free"),
        "fragrance_present": detect_simple_claim("fragrance", "fragrance_present"),
        "phthalate_free": detect_simple_claim("phthalate-free", "phthalate_free"),
        "dermatologically_tested": detect_simple_claim("dermatologically tested", "dermatologically_tested"),
        "clinically_tested": detect_simple_claim("clinically tested", "clinically_tested"),
        "ophthalmologically_tested": detect_simple_claim("ophthalmologically tested", "ophthalmologically_tested"),
        
        "hydration": hydration,
        "anti_ageing": anti_ageing,
        "pigmentation": pigmentation,
        "acne": acne,
        "redness": redness,
        "sensitivity": sensitivity,
        "scalp_care": scalp_care,
        "hair_growth": hair_growth,
        "fragrance": fragrance,
        "freshness": freshness,
        
        "benefits": benefits,
        "directions": {
            "text": inferred_directions,
            "source_status": "inferred",
            "evidence": [{
                "source_reference": None,
                "source_field": "product_type",
                "supporting_text": f"General usage inferred from product type '{inferred_type}'.",
                "evidence_type": "catalogue_rule_inference",
                "char_offsets": None
            }],
            "confidence": 0.55
        },
        "skin_type_fit": {
            "applicable": False,
            "recommended_for": [],
            "not_recommended_for": [],
            "unknown_for": [],
            "evidence": [],
            "confidence": None
        },
        "hair_type_fit": {
            "applicable": False,
            "recommended_for": [],
            "not_recommended_for": [],
            "unknown_for": [],
            "evidence": [],
            "confidence": None
        },
        "fragrance_intelligence": {
            "applicable": False,
            "fragrance_presence_status": "not_detected_from_supplied_data",
            "fragrance_family": None,
            "top_notes": [],
            "middle_notes": [],
            "base_notes": [],
            "evidence": [],
            "confidence": None
        },
        "pregnancy_warning_observation": {
            "observation_domain": "pregnancy",
            "review_required": True if "retinol" in raw_ingredients.lower() else False,
            "observation_type": "retinol_present" if "retinol" in raw_ingredients.lower() else "none",
            "observed_items": ["retinol"] if "retinol" in raw_ingredients.lower() else [],
            "evidence": [{
                "source_reference": None,
                "source_field": "ingredients",
                "supporting_text": "Ingredients contain retinol.",
                "evidence_type": "keyword_match",
                "char_offsets": None
            }] if "retinol" in raw_ingredients.lower() else [],
            "review_message": "Contains Retinol. Factual review required for pregnancy use (no safety conclusion is made)." if "retinol" in raw_ingredients.lower() else "No pregnancy safety concerns detected.",
            "confidence": 1.0
        },
        "allergen_warning_observation": {
            "observation_domain": "allergen",
            "review_required": True,
            "observation_type": "unknown",
            "observed_items": [],
            "evidence": [],
            "review_message": "Awaiting AI enrichment run observation.",
            "confidence": 0.0
        },
        "sensitivity_warning_observation": {
            "observation_domain": "sensitivity",
            "review_required": True,
            "observation_type": "unknown",
            "observed_items": [],
            "evidence": [],
            "review_message": "Awaiting AI enrichment run observation.",
            "confidence": 0.0
        },
        "ingredients_intelligence": ingredients_list
    }

def run_ai_enrichment(
    db: Session,
    name: str,
    brand: str,
    description: str,
    raw_ingredients: str,
    import_job_id: Optional[uuid.UUID] = None,
    import_job_item_id: Optional[uuid.UUID] = None,
    source_listing_id: Optional[uuid.UUID] = None,
    canonical_product_id: Optional[uuid.UUID] = None,
    product_variant_id: Optional[uuid.UUID] = None,
    parent_enrichment_run_id: Optional[uuid.UUID] = None,
    attempt: int = 1,
    source_context: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Optional[uuid.UUID]]:
    """Runs Gemini API enrichment, validates it via Pydantic, calculates token pricing,
    saves the enrichment run diagnostics log, and returns parsed JSON.
    """
    source_context = source_context or {}
    input_text = (
        f"Title: {name}\nBrand: {brand}\nDescription: {description}\n"
        f"Ingredients: {raw_ingredients}\n"
        f"Complete supplied source record: {json.dumps(source_context, ensure_ascii=False, default=str)}"
    )
    input_content_hash = hashlib.sha256(input_text.encode('utf-8')).hexdigest()
    ingredient_knowledge = retrieve_ingredient_knowledge(db, raw_ingredients)
    grounding_context = build_ingredient_grounding_context(ingredient_knowledge)

    run_id = uuid.uuid4()
    
    # 1. OpenAI Flow if OpenAI key is present
    if settings.OPENAI_API_KEY:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
        
        system_prompt = (
            "You are an expert Cosmetic Chemist and Beauty PIM Assistant. Extract structured beauty data. "
            f"Custom enrichment policy: {settings.ENRICHMENT_CUSTOM_INSTRUCTIONS} "
            f"{BALANCED_INFERENCE_GUIDANCE}"
            "Strictly return JSON matching the specified JSON schema. "
            "Ensure uncertainty is captured in CategoricalField, ClaimField, ConcernField structures. "
            "Distinguish direct extraction from reasonable inference in the semantic status and confidence. "
            "Provide evidence matching the raw fields strictly. Do not fabricate supporting quotes. "
            "The supplied ingredient reference context contains exact glossary matches. Use it only "
            "to normalize INCI names and report declared cosmetic functions. It is informative and "
            "does not establish safety, legal compliance, product benefits, or brand claims. "
            "When _beautypim_catalogue_knowledge is supplied, it contains observations matched to "
            "this product plus ranked retail knowledge examples. Use retail_reference_matches "
            "as direct evidence only when their exact-match basis is supplied. Use retail_knowledge_examples "
            "as broad industry intelligence to infer classification, positioning, likely benefit/concern "
            "patterns, texture, usage and target audiences. Prefer patterns repeated across several close "
            "examples. Mark these outputs as inferred and explain the comparison. Never copy a comparable "
            "product's exact INCI, certification, free-from claim, price or compliance status. "
            "Use direct exact-product values as source-backed evidence, retain their source "
            "URLs in evidence or reasoning, and surface contradictions instead of resolving them "
            "by invention. Never transfer attributes from a different product. "
            "Only report a pregnancy ingredient observation when a named retinoid is explicitly present as an INCI item. Never infer retinol from product type, benefits, marketing language, or unrelated oils. Keep any observation factual and make no medical safety conclusion."
            f"\n\nJSON Schema to match:\n{json.dumps(BeautyProductEnrichmentSchema.model_json_schema())}"
        )
        
        prompt = (
            f"Analyze the following beauty product and enrich its metadata:\n\n{input_text}"
            f"\n\nExact ingredient reference context:\n{grounding_context}"
        )
        
        payload = {
            "model": settings.OPENAI_MODEL,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ]
        }
        
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=60)
            response_json = response.json()
            
            if "error" in response_json:
                raise Exception(f"OpenAI API Error: {response_json['error'].get('message', 'Unknown error')}")
            if response.status_code != 200:
                raise Exception(f"OpenAI API returned status code {response.status_code}: {response.text}")
                
            candidate_text = response_json["choices"][0]["message"]["content"]
            
            prompt_t = response_json.get("usage", {}).get("prompt_tokens", calculate_token_count_rough(prompt))
            complete_t = response_json.get("usage", {}).get("completion_tokens", calculate_token_count_rough(candidate_text))
            cost = (prompt_t * 0.00015 / 1000) + (complete_t * 0.0006 / 1000)
            
            parsed_data = normalize_provider_shapes(
                normalize_null_confidences(json.loads(candidate_text))
            )
            parsed_data = BeautyProductEnrichmentSchema.model_validate(parsed_data).model_dump()
            parsed_data = normalize_and_validate_enrichment(parsed_data, raw_ingredients)
            parsed_data = ensure_catalogue_coverage(
                parsed_data, name, brand, description, raw_ingredients
            )
            
            run_record = EnrichmentRun(
                id=run_id,
                import_job_id=import_job_id,
                import_job_item_id=import_job_item_id,
                source_listing_id=source_listing_id,
                canonical_product_id=canonical_product_id,
                product_variant_id=product_variant_id,
                parent_enrichment_run_id=parent_enrichment_run_id,
                provider="OpenAI",
                model=settings.OPENAI_MODEL,
                model_version="1.0",
                prompt_version=settings.PROMPT_VERSION,
                schema_version=settings.SCHEMA_VERSION,
                status="success",
                processing_time_ms=int(response.elapsed.total_seconds() * 1000),
                prompt_tokens=prompt_t,
                completion_tokens=complete_t,
                estimated_cost=cost,
                attempt_number=attempt,
                input_content_hash=input_content_hash,
                raw_response=candidate_text
            )
            db.add(run_record)
            db.commit()
            return parsed_data, run_id
            
        except Exception as e:
            try:
                db.rollback()
                run_record = EnrichmentRun(
                    id=run_id,
                    import_job_id=import_job_id,
                    import_job_item_id=import_job_item_id,
                    source_listing_id=source_listing_id,
                    canonical_product_id=canonical_product_id,
                    product_variant_id=product_variant_id,
                    parent_enrichment_run_id=parent_enrichment_run_id,
                    provider="OpenAI",
                    model=settings.OPENAI_MODEL,
                    model_version="1.0",
                    prompt_version=settings.PROMPT_VERSION,
                    schema_version=settings.SCHEMA_VERSION,
                    status="failed",
                    error_details=str(e),
                    attempt_number=attempt,
                    input_content_hash=input_content_hash,
                    validation_errors={"error": str(e)}
                )
                db.add(run_record)
                db.commit()
            except Exception as db_err:
                db.rollback()
                print(f"Failed to record OpenAI run: {db_err}")
                
            if attempt < 2:
                return run_ai_enrichment(
                    db, name, brand, description, raw_ingredients,
                    import_job_id, import_job_item_id, source_listing_id,
                    canonical_product_id, product_variant_id,
                    parent_enrichment_run_id=run_id, attempt=attempt + 1,
                    source_context=source_context,
                )
            fallback_data = generate_deterministic_fallback(name, brand, description, raw_ingredients)
            fallback_data = ground_fallback_ingredients(fallback_data, ingredient_knowledge)
            fallback_data = normalize_and_validate_enrichment(fallback_data, raw_ingredients)
            fallback_data = ensure_catalogue_coverage(
                fallback_data, name, brand, description, raw_ingredients
            )
            return fallback_data, run_id

    # 2. Fallback if Gemini key is missing
    if not settings.GEMINI_API_KEY:
        fallback_data = generate_deterministic_fallback(name, brand, description, raw_ingredients)
        fallback_data = ground_fallback_ingredients(fallback_data, ingredient_knowledge)
        fallback_data = normalize_and_validate_enrichment(fallback_data, raw_ingredients)
        fallback_data = ensure_catalogue_coverage(
            fallback_data, name, brand, description, raw_ingredients
        )
        run_record = EnrichmentRun(
            id=run_id,
            import_job_id=import_job_id,
            import_job_item_id=import_job_item_id,
            source_listing_id=source_listing_id,
            canonical_product_id=canonical_product_id,
            product_variant_id=product_variant_id,
            parent_enrichment_run_id=parent_enrichment_run_id,
            provider="Deterministic Fallback",
            model="None",
            model_version="None",
            prompt_version=settings.PROMPT_VERSION,
            schema_version=settings.SCHEMA_VERSION,
            status="success",
            error_details="AI API key is not configured. Deterministic enrichment was applied successfully.",
            processing_time_ms=0,
            prompt_tokens=0,
            completion_tokens=0,
            estimated_cost=0.0,
            attempt_number=attempt,
            input_content_hash=input_content_hash,
            validation_errors={"reason": "Gemini API key is missing. No AI enrichment could run."}
        )
        db.add(run_record)
        db.commit()
        return fallback_data, run_id

    # 3. Gemini Invocations Setup
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.GEMINI_MODEL}:generateContent?key={settings.GEMINI_API_KEY}"
    
    system_prompt = (
        "You are an expert Cosmetic Chemist and Beauty PIM Assistant. Extract structured beauty data. "
        f"Custom enrichment policy: {settings.ENRICHMENT_CUSTOM_INSTRUCTIONS} "
        f"{BALANCED_INFERENCE_GUIDANCE}"
        "Strictly return JSON matching the specified JSON schema. "
        "Ensure uncertainty is captured in CategoricalField, ClaimField, ConcernField structures. "
        "Distinguish direct extraction from reasonable inference in the semantic status and confidence. "
        "Provide evidence matching the raw fields strictly. Do not fabricate supporting quotes. "
        "The supplied ingredient reference context contains exact glossary matches. Use it only "
        "to normalize INCI names and report declared cosmetic functions. It is informative and "
        "does not establish safety, legal compliance, product benefits, or brand claims. "
        "When _beautypim_catalogue_knowledge is supplied, exact retail_reference_matches are direct evidence. "
        "Ranked retail_knowledge_examples are broad industry intelligence: use recurring patterns to infer "
        "classification, positioning, likely benefits/concerns, texture, usage and audiences at moderate "
        "confidence. Explain that these are comparative inferences. Never copy another product's exact INCI, "
        "certification, free-from claim, price or compliance status. "
        "Only report a pregnancy ingredient observation when a named retinoid is explicitly present as an INCI item. Never infer retinol from product type, benefits, marketing language, or unrelated oils. Keep any observation factual and make no medical safety conclusion."
    )

    prompt = (
        f"Analyze the following beauty product and enrich its metadata:\n\n{input_text}"
        f"\n\nExact ingredient reference context:\n{grounding_context}"
    )

    # Define evidence schema to reuse
    evidence_schema = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "source_reference": {"type": "STRING"},
                "source_field": {"type": "STRING"},
                "supporting_text": {"type": "STRING"},
                "evidence_type": {"type": "STRING"},
                "char_offsets": {"type": "STRING"}
            },
            "required": ["source_field", "supporting_text", "evidence_type"]
        }
    }

    # Define payload schema config
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "subcategory": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "value_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["value", "value_status", "reasoning_summary", "confidence", "evidence"]},
                    "product_type": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "value_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["value", "value_status", "reasoning_summary", "confidence", "evidence"]},
                    "gender_target": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "value_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["value", "value_status", "reasoning_summary", "confidence", "evidence"]},
                    "texture": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "value_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["value", "value_status", "reasoning_summary", "confidence", "evidence"]},
                    "application_area": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "value_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["value", "value_status", "reasoning_summary", "confidence", "evidence"]},
                    "target_audience": {"type": "OBJECT", "properties": {"value": {"type": "ARRAY", "items": {"type": "STRING"}, "minItems": 3, "maxItems": 3}, "value_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["value", "value_status", "reasoning_summary", "confidence", "evidence"]},
                    
                    "vegan": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "claim_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["value", "claim_status", "reasoning_summary", "confidence", "evidence"]},
                    "cruelty_free": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "claim_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["value", "claim_status", "reasoning_summary", "confidence", "evidence"]},
                    "paraben_free": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "claim_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["value", "claim_status", "reasoning_summary", "confidence", "evidence"]},
                    "sulfate_free": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "claim_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["value", "claim_status", "reasoning_summary", "confidence", "evidence"]},
                    "silicone_free": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "claim_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["value", "claim_status", "reasoning_summary", "confidence", "evidence"]},
                    "alcohol_free": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "claim_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["value", "claim_status", "reasoning_summary", "confidence", "evidence"]},
                    "fragrance_present": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "claim_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["value", "claim_status", "reasoning_summary", "confidence", "evidence"]},
                    
                    "hydration": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["targeting_status", "reasoning_summary", "confidence", "evidence"]},
                    "anti_ageing": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["targeting_status", "reasoning_summary", "confidence", "evidence"]},
                    "pigmentation": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["targeting_status", "reasoning_summary", "confidence", "evidence"]},
                    "acne": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["targeting_status", "reasoning_summary", "confidence", "evidence"]},
                    "redness": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["targeting_status", "reasoning_summary", "confidence", "evidence"]},
                    "sensitivity": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["targeting_status", "reasoning_summary", "confidence", "evidence"]},
                    "scalp_care": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["targeting_status", "reasoning_summary", "confidence", "evidence"]},
                    "hair_growth": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["targeting_status", "reasoning_summary", "confidence", "evidence"]},
                    "fragrance": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["targeting_status", "reasoning_summary", "confidence", "evidence"]},
                    "freshness": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["targeting_status", "reasoning_summary", "confidence", "evidence"]},
                    
                    "benefits": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"statement": {"type": "STRING"}, "source_type": {"type": "STRING"}, "evidence": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["statement", "source_type", "evidence", "confidence"]}},
                    "directions": {"type": "OBJECT", "properties": {"text": {"type": "STRING"}, "source_status": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["text", "source_status", "confidence", "evidence"]},
                    
                    "skin_type_fit": {"type": "OBJECT", "properties": {"applicable": {"type": "BOOLEAN"}, "recommended_for": {"type": "ARRAY", "items": {"type": "STRING"}}, "not_recommended_for": {"type": "ARRAY", "items": {"type": "STRING"}}, "unknown_for": {"type": "ARRAY", "items": {"type": "STRING"}}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["applicable", "recommended_for", "not_recommended_for", "unknown_for", "confidence", "evidence"]},
                    "hair_type_fit": {"type": "OBJECT", "properties": {"applicable": {"type": "BOOLEAN"}, "recommended_for": {"type": "ARRAY", "items": {"type": "STRING"}}, "not_recommended_for": {"type": "ARRAY", "items": {"type": "STRING"}}, "unknown_for": {"type": "ARRAY", "items": {"type": "STRING"}}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["applicable", "recommended_for", "not_recommended_for", "unknown_for", "confidence", "evidence"]},
                    "fragrance_intelligence": {"type": "OBJECT", "properties": {"applicable": {"type": "BOOLEAN"}, "fragrance_presence_status": {"type": "STRING"}, "fragrance_family": {"type": "STRING"}, "top_notes": {"type": "ARRAY", "items": {"type": "STRING"}}, "middle_notes": {"type": "ARRAY", "items": {"type": "STRING"}}, "base_notes": {"type": "ARRAY", "items": {"type": "STRING"}}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["applicable", "fragrance_presence_status", "fragrance_family", "top_notes", "middle_notes", "base_notes", "confidence", "evidence"]},
                    
                    "pregnancy_warning_observation": {"type": "OBJECT", "properties": {"observation_domain": {"type": "STRING"}, "review_required": {"type": "BOOLEAN"}, "observation_type": {"type": "STRING"}, "observed_items": {"type": "ARRAY", "items": {"type": "STRING"}}, "review_message": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["observation_domain", "review_required", "observation_type", "observed_items", "review_message", "confidence", "evidence"]},
                    "allergen_warning_observation": {"type": "OBJECT", "properties": {"observation_domain": {"type": "STRING"}, "review_required": {"type": "BOOLEAN"}, "observation_type": {"type": "STRING"}, "observed_items": {"type": "ARRAY", "items": {"type": "STRING"}}, "review_message": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["observation_domain", "review_required", "observation_type", "observed_items", "review_message", "confidence", "evidence"]},
                    "sensitivity_warning_observation": {"type": "OBJECT", "properties": {"observation_domain": {"type": "STRING"}, "review_required": {"type": "BOOLEAN"}, "observation_type": {"type": "STRING"}, "observed_items": {"type": "ARRAY", "items": {"type": "STRING"}}, "review_message": {"type": "STRING"}, "confidence": {"type": "NUMBER"}, "evidence": evidence_schema}, "required": ["observation_domain", "review_required", "observation_type", "observed_items", "review_message", "confidence", "evidence"]},
                    
                    "ingredients_intelligence": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "ingredient_name": {"type": "STRING"},
                                "normalized_inci_name": {"type": "STRING"},
                                "functions": {"type": "ARRAY", "items": {"type": "STRING"}},
                                "benefits": {"type": "ARRAY", "items": {"type": "STRING"}},
                                "is_key_ingredient": {"type": "BOOLEAN"},
                                "key_ingredient_status": {"type": "STRING"},
                                "confidence": {"type": "NUMBER"},
                                "evidence": evidence_schema
                            },
                            "required": ["ingredient_name", "normalized_inci_name", "functions", "benefits", "is_key_ingredient", "key_ingredient_status", "confidence", "evidence"]
                        }
                    }
                },
                "required": [
                    "subcategory", "product_type", "gender_target", "texture", "application_area", "target_audience",
                    "vegan", "cruelty_free", "paraben_free", "sulfate_free", "silicone_free", "alcohol_free", "fragrance_present",
                    "hydration", "anti_ageing", "pigmentation", "acne", "redness", "sensitivity", "scalp_care", "hair_growth", "fragrance", "freshness",
                    "benefits", "directions", "skin_type_fit", "hair_type_fit", "fragrance_intelligence",
                    "pregnancy_warning_observation", "allergen_warning_observation", "sensitivity_warning_observation", "ingredients_intelligence"
                ]
            }
        }
    }

    try:
        response = requests.post(url, json=payload, timeout=60)
        response_json = response.json()
        
        # Check for Google API error response formats
        if "error" in response_json:
            raise Exception(f"Gemini API Error: {response_json['error'].get('message', 'Unknown API error')}")
        if response.status_code != 200:
            raise Exception(f"Gemini API returned status code {response.status_code}: {response.text}")
        if "candidates" not in response_json or not response_json["candidates"]:
            raise Exception(f"Gemini API response missing candidates: {response.text}")

        # Log pricing and metrics
        candidate_text = response_json["candidates"][0]["content"]["parts"][0]["text"]
        
        # Calculate cost
        prompt_t = response_json.get("usageMetadata", {}).get("promptTokenCount", calculate_token_count_rough(prompt))
        complete_t = response_json.get("usageMetadata", {}).get("candidatesTokenCount", calculate_token_count_rough(candidate_text))
        cost = (prompt_t * 0.000075 / 1000) + (complete_t * 0.0003 / 1000)

        # Validate with Pydantic
        parsed_data = normalize_provider_shapes(
            normalize_null_confidences(json.loads(candidate_text))
        )
        
        # Ensure default array properties that model might omit
        parsed_data = BeautyProductEnrichmentSchema.model_validate(parsed_data).model_dump()
        parsed_data = normalize_and_validate_enrichment(parsed_data, raw_ingredients)
        parsed_data = ensure_catalogue_coverage(
            parsed_data, name, brand, description, raw_ingredients
        )
        
        # Save Success Run
        run_record = EnrichmentRun(
            id=run_id,
            import_job_id=import_job_id,
            import_job_item_id=import_job_item_id,
            source_listing_id=source_listing_id,
            canonical_product_id=canonical_product_id,
            product_variant_id=product_variant_id,
            parent_enrichment_run_id=parent_enrichment_run_id,
            provider="Google Gemini",
            model=settings.GEMINI_MODEL,
            model_version=settings.GEMINI_MODEL_VERSION,
            prompt_version=settings.PROMPT_VERSION,
            schema_version=settings.SCHEMA_VERSION,
            status="success",
            processing_time_ms=int(response.elapsed.total_seconds() * 1000),
            prompt_tokens=prompt_t,
            completion_tokens=complete_t,
            estimated_cost=cost,
            attempt_number=attempt,
            input_content_hash=input_content_hash,
            raw_response=candidate_text
        )
        db.add(run_record)
        db.commit()
        return parsed_data, run_id

    except Exception as e:
        # Save current failed attempt to database to preserve parent foreign key relationship
        try:
            # If transaction is in failed state, rollback to allow logging run_record
            db.rollback()
            run_record = EnrichmentRun(
                id=run_id,
                import_job_id=import_job_id,
                import_job_item_id=import_job_item_id,
                source_listing_id=source_listing_id,
                canonical_product_id=canonical_product_id,
                product_variant_id=product_variant_id,
                parent_enrichment_run_id=parent_enrichment_run_id,
                provider="Google Gemini",
                model=settings.GEMINI_MODEL,
                model_version=settings.GEMINI_MODEL_VERSION,
                prompt_version=settings.PROMPT_VERSION,
                schema_version=settings.SCHEMA_VERSION,
                status="failed",
                error_details=str(e),
                attempt_number=attempt,
                input_content_hash=input_content_hash,
                validation_errors={"error": str(e)}
            )
            db.add(run_record)
            db.commit()
        except Exception as db_err:
            db.rollback()
            print(f"Failed to record enrichment run to db: {db_err}")

        # If failure is parser error, retry once using attempt count
        if attempt < 2:
            return run_ai_enrichment(
                db, name, brand, description, raw_ingredients,
                import_job_id, import_job_item_id, source_listing_id,
                canonical_product_id, product_variant_id,
                parent_enrichment_run_id=run_id, attempt=attempt + 1,
                source_context=source_context,
            )
            
        fallback_data = generate_deterministic_fallback(name, brand, description, raw_ingredients)
        fallback_data = ground_fallback_ingredients(fallback_data, ingredient_knowledge)
        fallback_data = normalize_and_validate_enrichment(fallback_data, raw_ingredients)
        fallback_data = ensure_catalogue_coverage(
            fallback_data, name, brand, description, raw_ingredients
        )
        return fallback_data, run_id
