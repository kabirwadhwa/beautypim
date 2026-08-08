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
    "Return useful universal catalogue fields and exactly one applicable category module. For classification fields "
    "such as subcategory, product type and application area, make a reasonable inference "
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
    "Generate positioning, benefits, targeted concerns, directions and a sensory profile. Claims are an extensible "
    "list and must be verified/source-supported, unverified, conflicting or unknown; never guess a positive claim. "
    "Warnings are factual observations, not medical advice. Do not generate origin, manufacture country, launch year, "
    "gender, ingredient counts, skin scores, credentials, absorption or application sequence as separate attributes. "
    "Apply category-specific semantics. For personal fragrance prioritize concentration, fragrance family, "
    "top/heart/base notes, longevity, sillage/projection, seasonal fit, occasion fit and pulse-point usage; "
    "do not describe fragrance as skincare absorption, skin-type suitability or a cosmetic finish. "
    "For skin care prioritize skin fit, routine step, texture and concerns; for hair care prioritize hair/scalp fit; "
    "for makeup prioritize shade, coverage, finish and wear occasion. "
)

CLAIM_NAMES = (
    "vegan", "cruelty_free", "paraben_free", "sulfate_free", "silicone_free",
    "alcohol_free", "fragrance_present", "phthalate_free", "dermatologically_tested",
    "clinically_tested", "ophthalmologically_tested",
)

def consolidate_enrichment_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Convert legacy/provider shapes into the simplified v3 product contract."""
    data = dict(payload or {})
    claims = data.get("claims") if isinstance(data.get("claims"), list) else []
    if not claims:
        for name in CLAIM_NAMES:
            item = data.get(name)
            if not isinstance(item, dict):
                continue
            raw_status = str(item.get("claim_status") or "unknown").lower()
            status = "source_supported" if raw_status in {"explicit", "verified", "source_supported"} else (
                raw_status if raw_status in {"unverified", "conflicting", "unknown"} else "unverified"
            )
            claims.append({"name": name.replace("_", " ").title(), "value": item.get("value"),
                           "status": status, "evidence": _evidence_list(item.get("evidence")),
                           "reasoning_summary": item.get("reasoning_summary") or "",
                           "confidence": item.get("confidence") or 0.5})
    data["claims"] = claims

    warnings = data.get("warnings_considerations") if isinstance(data.get("warnings_considerations"), list) else []
    if not warnings:
        for old, warning_type in (("pregnancy_warning_observation", "pregnancy"),
                                  ("allergen_warning_observation", "allergen"),
                                  ("sensitivity_warning_observation", "sensitivity")):
            item = data.get(old)
            if isinstance(item, dict) and item.get("review_required"):
                warnings.append({"type": warning_type, "observation": item.get("review_message") or "Review required.",
                                 "evidence": _evidence_list(item.get("evidence")),
                                 "source_status": "source_supported" if item.get("observed_items") else "unverified",
                                 "confidence": item.get("confidence") or 0.5})
        regulatory = (data.get("regulatory_notes") or {}).get("value") if isinstance(data.get("regulatory_notes"), dict) else None
        if regulatory and str(regulatory).lower() not in UNKNOWN_VALUES:
            warnings.append({"type": "regulatory", "observation": regulatory, "evidence": [],
                             "source_status": "unverified", "confidence": 0.5})
    data["warnings_considerations"] = warnings

    ptype = str((data.get("product_type") or {}).get("value") or "").lower()
    subcat = str((data.get("subcategory") or {}).get("value") or "").lower()
    category_text = f"{ptype} {subcat}"
    ingredients = data.get("ingredients_intelligence") or []
    simplified_ingredients = []
    for item in ingredients:
        if isinstance(item, dict):
            simplified_ingredients.append({key: item.get(key) for key in (
                "ingredient_name", "inci_position", "short_description", "functions", "benefits",
                "possible_concerns", "is_key_ingredient", "key_ingredient_status"
            ) if key in item})
    data["ingredients_intelligence"] = simplified_ingredients
    is_fragrance = any(term in category_text for term in ("fragrance", "perfume", "parfum", "eau de"))
    is_hair = any(term in category_text for term in ("hair", "shampoo", "conditioner", "scalp"))
    is_makeup = any(term in category_text for term in ("makeup", "lipstick", "foundation", "concealer", "mascara"))
    if is_fragrance:
        old = data.get("fragrance_intelligence") or {}
        data["fragrance"] = data.get("fragrance") if isinstance(data.get("fragrance"), dict) and "concentration" in data.get("fragrance", {}) else {
            "concentration": old.get("concentration"), "fragrance_family": old.get("fragrance_family"),
            "top_notes": old.get("top_notes") or [], "heart_notes": old.get("heart_notes") or old.get("middle_notes") or [],
            "base_notes": old.get("base_notes") or [], "longevity": old.get("longevity_profile"),
            "sillage_projection": old.get("sillage_projection"), "seasonal_fit": old.get("seasonal_fit") or [],
            "occasion_fit": old.get("occasion_fit") or [], "evidence": _evidence_list(old.get("evidence")),
            "confidence": old.get("confidence") or 0.5}
        data.update(skincare=None, haircare=None, makeup=None)
    elif is_hair:
        data["haircare"] = data.get("haircare") or {"hair_types": data.get("hair_type_fit") or {"applicable": True},
            "texture_format": data.get("texture"), "key_ingredients": [i for i in simplified_ingredients if i.get("is_key_ingredient")]}
        data.update(skincare=None, makeup=None, fragrance=None)
    elif is_makeup:
        data["makeup"] = data.get("makeup") or {"shade_colour": data.get("colour"), "coverage": data.get("coverage"),
            "finish": data.get("finish"), "texture_format": data.get("texture")}
        data.update(skincare=None, haircare=None, fragrance=None)
    else:
        data["skincare"] = data.get("skincare") or {"skin_types": data.get("skin_type_fit") or {"applicable": True},
            "texture": data.get("texture"), "finish": data.get("finish"),
            "key_ingredients": [i for i in simplified_ingredients if i.get("is_key_ingredient")]}
        data.update(haircare=None, makeup=None, fragrance=None)
    return data

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
    """Coerce provider JSON drift into the current simplified schema."""
    for field in ("subcategory", "product_type", "application_area", "target_audience",
                  "product_positioning", "sensory_description", "routine_time", "routine_step"):
        item = payload.get(field)
        if isinstance(item, dict):
            item["evidence"] = _evidence_list(item.get("evidence"))
            item.setdefault("value_status", "inferred" if item.get("value") else "unknown")
            item.setdefault("reasoning_summary", "Provider output normalized to the catalogue schema.")
            item.setdefault("confidence", 0.65 if item.get("value") else 0.5)
    for item in payload.get("claims") or []:
        if isinstance(item, dict):
            item["evidence"] = _evidence_list(item.get("evidence"))
    for item in payload.get("warnings_considerations") or []:
        if isinstance(item, dict):
            item["evidence"] = _evidence_list(item.get("evidence"))
    for benefit in payload.get("benefits") or []:
        if isinstance(benefit, dict) and isinstance(benefit.get("evidence"), list):
            benefit["evidence"] = "; ".join(str(x.get("supporting_text") or x) if isinstance(x, dict) else str(x)
                                              for x in benefit["evidence"])
    return consolidate_enrichment_payload(payload)

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
    """Fill only safe gaps in the simplified, category-aware contract."""
    data = consolidate_enrichment_payload(data)
    model_type = str((data.get("product_type") or {}).get("value") or "")
    model_subcategory = str((data.get("subcategory") or {}).get("value") or "")
    fallback = generate_deterministic_fallback(name, brand, " ".join(filter(None, (description, model_type, model_subcategory))), raw_ingredients)
    for field in ("subcategory", "product_type", "application_area", "product_positioning", "sensory_description"):
        if _is_missing_field(data.get(field), "value"):
            data[field] = fallback[field]
    audience = (data.get("target_audience") or {}).get("value")
    if not isinstance(audience, list) or len([x for x in audience if str(x).strip()]) != 3:
        data["target_audience"] = fallback["target_audience"]
    for field in ("benefits", "directions", "targeted_concerns", "claims", "warnings_considerations",
                  "skincare", "haircare", "makeup", "fragrance", "ingredients_intelligence"):
        if data.get(field) in (None, []):
            data[field] = fallback.get(field)
    return consolidate_enrichment_payload(data)


def prepare_provider_payload(
    payload: Dict[str, Any], name: str, brand: str, description: str, raw_ingredients: str,
) -> Dict[str, Any]:
    """Merge partial model output over a complete safe schema before validation."""
    fallback = generate_deterministic_fallback(name, brand, description, raw_ingredients)
    merged = {**fallback, **payload}
    return ensure_catalogue_coverage(consolidate_enrichment_payload(merged), name, brand, description, raw_ingredients)

def normalize_and_validate_enrichment(data: Dict[str, Any], raw_ingredients: str) -> Dict[str, Any]:
    """Normalize provider vocabulary and keep warnings factual and source-bound."""
    data = consolidate_enrichment_payload(data)
    ingredient_text = (raw_ingredients or "").lower()
    retinoids = ("retinol", "retinal", "retinaldehyde", "retinyl palmitate", "retinyl acetate")
    verified = [name for name in retinoids if re.search(rf"(?:^|[,;])\s*{re.escape(name)}(?:\s*\(|\s*[,;]|$)", ingredient_text)]
    warnings = [w for w in data.get("warnings_considerations", []) if w.get("type") != "pregnancy"]
    if verified:
        warnings.append({"type": "pregnancy", "observation": f"Contains {', '.join(verified)}; factual review required. No medical conclusion is made.",
                         "evidence": [], "source_status": "source_supported", "confidence": 1.0})
    data["warnings_considerations"] = warnings
    return data

def calculate_token_count_rough(text: str) -> int:
    # Basic rough calculation: ~4 chars per token
    return len(text) // 4

def generate_deterministic_fallback(name: str, brand: str, description: str, raw_ingredients: str) -> Dict[str, Any]:
    """Balanced, category-aware fallback using only the current product model."""
    source = f"{name} {description}".lower()

    def categorical(value: str, reason: str, confidence: float = 0.6) -> Dict[str, Any]:
        return {"value": value, "value_status": "inferred", "evidence": [],
                "reasoning_summary": reason, "confidence": confidence}

    type_rules = (
        ("eau de parfum", "Eau de Parfum"), ("eau de toilette", "Eau de Toilette"),
        ("parfum", "Parfum"), ("perfume", "Fragrance"), ("shampoo", "Shampoo"),
        ("conditioner", "Conditioner"), ("foundation", "Foundation"), ("concealer", "Concealer"),
        ("mascara", "Mascara"), ("lipstick", "Lipstick"), ("cleanser", "Cleanser"),
        ("serum", "Serum"), ("moistur", "Moisturiser"), ("cream", "Cream"),
        ("toner", "Toner"), ("sunscreen", "Sunscreen"), ("spf", "Sunscreen"),
    )
    product_type = next((label for token, label in type_rules if token in source), "Beauty Product")
    lower_type = product_type.lower()
    is_fragrance = any(x in lower_type for x in ("fragrance", "parfum", "eau de"))
    is_hair = any(x in lower_type for x in ("hair", "shampoo", "conditioner"))
    is_makeup = any(x in lower_type for x in ("foundation", "concealer", "mascara", "lipstick", "makeup"))
    category = "Fragrance" if is_fragrance else "Haircare" if is_hair else "Makeup" if is_makeup else "Skincare"
    area = "body/pulse points" if is_fragrance else "hair and scalp" if is_hair else ("face, eyes or lips" if is_makeup else "face and skin")
    texture = next((label for token, label in (("gel", "Gel"), ("cream", "Cream"), ("oil", "Oil"),
                                                ("balm", "Balm"), ("spray", "Spray"), ("foam", "Foam"))
                    if token in source), "Product-appropriate format")
    concern_rules = (("hydrat", "Dehydration"), ("moistur", "Dryness"), ("wrinkle", "Fine lines"),
                     ("anti-age", "Visible ageing"), ("bright", "Dullness"), ("pigment", "Pigmentation"),
                     ("acne", "Acne"), ("blemish", "Blemishes"), ("sensitive", "Sensitivity"),
                     ("redness", "Redness"), ("scalp", "Scalp comfort"), ("thinning", "Hair thinning"))
    concerns = []
    for token, label in concern_rules:
        if token in source and label not in concerns:
            concerns.append(label)
    if not concerns:
        concerns = ["Personal scent expression"] if is_fragrance else [f"Everyday {category.lower()} care"]
    audience = [
        f"Shoppers looking for {concerns[0].lower()} support from an easy-to-understand {lower_type}.",
        f"Customers who prefer a {texture.lower()} product suited to an everyday {area} routine.",
        f"Beauty shoppers comparing {lower_type} options and wanting clear benefits and straightforward use.",
    ]
    benefit = ("Creates a personal fragrance signature" if is_fragrance else
               "Helps cleanse and care for hair and scalp" if is_hair else
               "Supports colour, definition or complexion enhancement" if is_makeup else
               "Supports an effective everyday skincare routine")
    direction_map = {"fragrance": "Apply sparingly to pulse points; do not rub after application.",
                     "haircare": "Apply as directed to hair or scalp and rinse when the format requires it.",
                     "makeup": "Apply to the intended area and build or blend to the desired result.",
                     "skincare": "Apply to clean skin in the product-appropriate step; follow pack directions."}
    ingredients = []
    for position, part in enumerate(re.split(r"[,;]", raw_ingredients or ""), 1):
        ingredient = part.strip()
        if ingredient:
            ingredients.append({"ingredient_name": ingredient, "inci_position": position,
                                "short_description": None, "functions": [], "benefits": [],
                                "possible_concerns": [], "is_key_ingredient": False,
                                "key_ingredient_status": "unreviewed"})
    claim_tokens = {
        "Vegan": "vegan", "Cruelty Free": "cruelty-free", "Paraben Free": "paraben-free",
        "Sulfate Free": "sulfate-free", "Silicone Free": "silicone-free", "Alcohol Free": "alcohol-free",
        "Phthalate Free": "phthalate-free", "Dermatologically Tested": "dermatologically tested",
        "Clinically Tested": "clinically tested", "Ophthalmologically Tested": "ophthalmologically tested",
    }
    claims = []
    for label, token in claim_tokens.items():
        supported = token in source
        claims.append({"name": label, "value": "Yes" if supported else None,
                       "status": "source_supported" if supported else "unverified", "evidence": [],
                       "reasoning_summary": "Explicit source wording detected." if supported else "No supporting source statement supplied.",
                       "confidence": 0.9 if supported else 0.5})
    common = {
        "subcategory": categorical(product_type, "Subcategory inferred from the supplied identity."),
        "product_type": categorical(product_type, "Product type inferred from title and description."),
        "application_area": categorical(area, "Application area inferred from product type."),
        "target_audience": {"value": audience, "value_status": "inferred", "evidence": [],
                            "reasoning_summary": "Three distinct commercial profiles inferred from product evidence.", "confidence": 0.62},
        "product_positioning": categorical(f"{product_type} for {concerns[0].lower()}", "Merchandising positioning inferred from the product profile."),
        "sensory_description": categorical(f"{texture} format with a product-appropriate application experience.", "Sensory wording inferred from format."),
        "routine_time": categorical("As appropriate for the product and pack directions", "Controlled routine timing."),
        "routine_step": categorical("Product-specific routine step", "Controlled routine step."),
        "targeted_concerns": {"values": concerns, "value_status": "inferred", "evidence": [],
                              "reasoning_summary": "Consolidated concerns inferred from supplied wording.", "confidence": 0.62},
        "claims": claims,
        "benefits": [{"statement": benefit, "source_type": "catalogue_inference",
                      "evidence": f"Inferred from product type {product_type}.", "confidence": 0.55}],
        "directions": {"text": direction_map[category.lower()], "source_status": "inferred", "evidence": [], "confidence": 0.55},
        "warnings_considerations": [], "ingredients_intelligence": ingredients,
        "skincare": None, "haircare": None, "makeup": None, "fragrance": None,
    }
    if is_fragrance:
        concentration = next((label for token, label in (("eau de parfum", "Eau de Parfum"),
                            ("eau de toilette", "Eau de Toilette"), ("parfum", "Parfum")) if token in source), None)
        common["fragrance"] = {"concentration": concentration, "fragrance_family": None, "top_notes": [],
            "heart_notes": [], "base_notes": [], "longevity": None, "sillage_projection": None,
            "seasonal_fit": [], "occasion_fit": [], "evidence": [], "confidence": 0.6}
    elif is_hair:
        common["haircare"] = {"hair_types": {"applicable": True, "recommended_for": [], "not_recommended_for": [],
            "unknown_for": ["all hair types pending evidence"], "evidence": [], "confidence": 0.5},
            "texture_format": categorical(texture, "Format inferred from source."), "key_ingredients": []}
    elif is_makeup:
        common["makeup"] = {"shade_colour": None, "coverage": None,
            "finish": categorical("Product-appropriate finish", "Finish requires product evidence."),
            "texture_format": categorical(texture, "Format inferred from source.")}
    else:
        common["skincare"] = {"skin_types": {"applicable": True, "recommended_for": [], "not_recommended_for": [],
            "unknown_for": ["all skin types pending evidence"], "evidence": [], "confidence": 0.5},
            "texture": categorical(texture, "Texture inferred from source."),
            "finish": categorical("Product-appropriate finish", "Finish requires product evidence."), "key_ingredients": []}
    return common

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
            parsed_data = prepare_provider_payload(
                parsed_data, name, brand, description, raw_ingredients
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

    # Gemini receives the same simplified contract as OpenAI. JSON mode keeps the
    # provider response machine-readable; Pydantic remains the authoritative validator.
    system_prompt += f"\n\nJSON Schema to match:\n{json.dumps(BeautyProductEnrichmentSchema.model_json_schema())}"
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
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
        parsed_data = prepare_provider_payload(
            parsed_data, name, brand, description, raw_ingredients
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
