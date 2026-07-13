import json
import requests
import hashlib
import uuid
from typing import Dict, Any, Tuple, Optional
from sqlalchemy.orm import Session
from app.config import settings
from app.schemas import BeautyProductEnrichmentSchema
from app.models import EnrichmentRun

def calculate_token_count_rough(text: str) -> int:
    # Basic rough calculation: ~4 chars per token
    return len(text) // 4

def generate_deterministic_fallback(
    name: str,
    brand: str,
    description: str,
    raw_ingredients: str
) -> Dict[str, Any]:
    """Runs deterministic extraction rules. Does NOT simulate AI inferences.
    AI fields remain unknown/unprocessed.
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
            "value": "unknown",
            "claim_status": "unknown",
            "evidence": [],
            "reasoning_summary": f"No keyword '{keyword}' found in title or description.",
            "confidence": 0.0
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

    def make_unknown_concern() -> Dict[str, Any]:
        return {
            "targeting_status": "unknown",
            "evidence": [],
            "reasoning_summary": "Unprocessed. Requires AI model enrichment.",
            "confidence": 0.0
        }

    return {
        "subcategory": make_unknown_categorical(),
        "product_type": make_unknown_categorical(),
        "gender_target": make_unknown_categorical(),
        "texture": make_unknown_categorical(),
        "application_area": make_unknown_categorical(),
        "target_audience": make_unknown_categorical(),
        
        "vegan": detect_simple_claim("vegan", "vegan"),
        "cruelty_free": detect_simple_claim("cruelty-free", "cruelty_free"),
        "paraben_free": detect_simple_claim("paraben-free", "paraben_free"),
        "sulfate_free": detect_simple_claim("sulfate-free", "sulfate_free"),
        "silicone_free": detect_simple_claim("silicone-free", "silicone_free"),
        "alcohol_free": detect_simple_claim("alcohol-free", "alcohol_free"),
        "fragrance_present": detect_simple_claim("fragrance", "fragrance_present"),
        
        "hydration": make_unknown_concern(),
        "anti_ageing": make_unknown_concern(),
        "pigmentation": make_unknown_concern(),
        "acne": make_unknown_concern(),
        "redness": make_unknown_concern(),
        "sensitivity": make_unknown_concern(),
        "scalp_care": make_unknown_concern(),
        "hair_growth": make_unknown_concern(),
        "fragrance": make_unknown_concern(),
        "freshness": make_unknown_concern(),
        
        "benefits": [],
        "directions": {
            "text": None,
            "source_status": "unknown",
            "evidence": [],
            "confidence": None
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
            "fragrance_presence_status": "unknown",
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
    attempt: int = 1
) -> Tuple[Dict[str, Any], Optional[uuid.UUID]]:
    """Runs Gemini API enrichment, validates it via Pydantic, calculates token pricing,
    saves the enrichment run diagnostics log, and returns parsed JSON.
    """
    input_text = f"Title: {name}\nBrand: {brand}\nDescription: {description}\nIngredients: {raw_ingredients}"
    input_content_hash = hashlib.sha256(input_text.encode('utf-8')).hexdigest()

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
            "Strictly return JSON matching the specified JSON schema. "
            "Ensure uncertainty is captured in CategoricalField, ClaimField, ConcernField structures. "
            "Do not invent functions or claims if evidence is insufficient; set them to 'unknown' or 'not_applicable'. "
            "Provide evidence matching the raw fields strictly. Do not fabricate supporting quotes. "
            "For the pregnancy_warning_observation field: if the ingredient list contains 'retinol', you must create a factual observation indicating the presence of retinol (review_required=true, observed_items=['retinol'], review_message='Contains retinol'). However, do NOT write a medical conclusion or state that it is unsafe or prohibited for pregnancy; keep the message purely factual."
            f"\n\nJSON Schema to match:\n{json.dumps(BeautyProductEnrichmentSchema.model_json_schema())}"
        )
        
        prompt = f"Analyze the following beauty product and enrich its metadata:\n\n{input_text}"
        
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
            
            parsed_data = json.loads(candidate_text)
            parsed_data = BeautyProductEnrichmentSchema.model_validate(parsed_data).model_dump()
            
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
                    parent_enrichment_run_id=run_id, attempt=attempt + 1
                )
            fallback_data = generate_deterministic_fallback(name, brand, description, raw_ingredients)
            return fallback_data, run_id

    # 2. Fallback if Gemini key is missing
    if not settings.GEMINI_API_KEY:
        fallback_data = generate_deterministic_fallback(name, brand, description, raw_ingredients)
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
            status="failed",
            error_details="Gemini API Key not set. Deterministic fallback applied.",
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
        "Strictly return JSON matching the specified JSON schema. "
        "Ensure uncertainty is captured in CategoricalField, ClaimField, ConcernField structures. "
        "Do not invent functions or claims if evidence is insufficient; set them to 'unknown' or 'not_applicable'. "
        "Provide evidence matching the raw fields strictly. Do not fabricate supporting quotes. "
        "For the pregnancy_warning_observation field: if the ingredient list contains 'retinol', you must create a factual observation indicating the presence of retinol (review_required=true, observed_items=['retinol'], review_message='Contains retinol'). However, do NOT write a medical conclusion or state that it is unsafe or prohibited for pregnancy; keep the message purely factual."
    )

    prompt = f"Analyze the following beauty product and enrich its metadata:\n\n{input_text}"

    # Load JSON schema file or structure
    # Define payload schema config
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "subcategory": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "value_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["value", "value_status", "reasoning_summary", "confidence"]},
                    "product_type": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "value_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["value", "value_status", "reasoning_summary", "confidence"]},
                    "gender_target": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "value_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["value", "value_status", "reasoning_summary", "confidence"]},
                    "texture": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "value_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["value", "value_status", "reasoning_summary", "confidence"]},
                    "application_area": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "value_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["value", "value_status", "reasoning_summary", "confidence"]},
                    "target_audience": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "value_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["value", "value_status", "reasoning_summary", "confidence"]},
                    
                    "vegan": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "claim_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["value", "claim_status", "reasoning_summary", "confidence"]},
                    "cruelty_free": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "claim_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["value", "claim_status", "reasoning_summary", "confidence"]},
                    "paraben_free": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "claim_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["value", "claim_status", "reasoning_summary", "confidence"]},
                    "sulfate_free": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "claim_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["value", "claim_status", "reasoning_summary", "confidence"]},
                    "silicone_free": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "claim_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["value", "claim_status", "reasoning_summary", "confidence"]},
                    "alcohol_free": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "claim_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["value", "claim_status", "reasoning_summary", "confidence"]},
                    "fragrance_present": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}, "claim_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["value", "claim_status", "reasoning_summary", "confidence"]},
                    
                    "hydration": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["targeting_status", "reasoning_summary", "confidence"]},
                    "anti_ageing": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["targeting_status", "reasoning_summary", "confidence"]},
                    "pigmentation": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["targeting_status", "reasoning_summary", "confidence"]},
                    "acne": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["targeting_status", "reasoning_summary", "confidence"]},
                    "redness": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["targeting_status", "reasoning_summary", "confidence"]},
                    "sensitivity": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["targeting_status", "reasoning_summary", "confidence"]},
                    "scalp_care": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["targeting_status", "reasoning_summary", "confidence"]},
                    "hair_growth": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["targeting_status", "reasoning_summary", "confidence"]},
                    "fragrance": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["targeting_status", "reasoning_summary", "confidence"]},
                    "freshness": {"type": "OBJECT", "properties": {"targeting_status": {"type": "STRING"}, "reasoning_summary": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["targeting_status", "reasoning_summary", "confidence"]},
                    
                    "benefits": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"statement": {"type": "STRING"}, "source_type": {"type": "STRING"}, "evidence": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["statement", "source_type", "evidence", "confidence"]}},
                    "directions": {"type": "OBJECT", "properties": {"text": {"type": "STRING"}, "source_status": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["text", "source_status", "confidence"]},
                    
                    "skin_type_fit": {"type": "OBJECT", "properties": {"applicable": {"type": "BOOLEAN"}, "recommended_for": {"type": "ARRAY", "items": {"type": "STRING"}}, "not_recommended_for": {"type": "ARRAY", "items": {"type": "STRING"}}, "unknown_for": {"type": "ARRAY", "items": {"type": "STRING"}}, "confidence": {"type": "NUMBER"}}, "required": ["applicable", "recommended_for", "not_recommended_for", "unknown_for", "confidence"]},
                    "hair_type_fit": {"type": "OBJECT", "properties": {"applicable": {"type": "BOOLEAN"}, "recommended_for": {"type": "ARRAY", "items": {"type": "STRING"}}, "not_recommended_for": {"type": "ARRAY", "items": {"type": "STRING"}}, "unknown_for": {"type": "ARRAY", "items": {"type": "STRING"}}, "confidence": {"type": "NUMBER"}}, "required": ["applicable", "recommended_for", "not_recommended_for", "unknown_for", "confidence"]},
                    "fragrance_intelligence": {"type": "OBJECT", "properties": {"applicable": {"type": "BOOLEAN"}, "fragrance_presence_status": {"type": "STRING"}, "fragrance_family": {"type": "STRING"}, "top_notes": {"type": "ARRAY", "items": {"type": "STRING"}}, "middle_notes": {"type": "ARRAY", "items": {"type": "STRING"}}, "base_notes": {"type": "ARRAY", "items": {"type": "STRING"}}, "confidence": {"type": "NUMBER"}}, "required": ["applicable", "fragrance_presence_status", "fragrance_family", "top_notes", "middle_notes", "base_notes", "confidence"]},
                    
                    "pregnancy_warning_observation": {"type": "OBJECT", "properties": {"observation_domain": {"type": "STRING"}, "review_required": {"type": "BOOLEAN"}, "observation_type": {"type": "STRING"}, "observed_items": {"type": "ARRAY", "items": {"type": "STRING"}}, "review_message": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["observation_domain", "review_required", "observation_type", "observed_items", "review_message", "confidence"]},
                    "allergen_warning_observation": {"type": "OBJECT", "properties": {"observation_domain": {"type": "STRING"}, "review_required": {"type": "BOOLEAN"}, "observation_type": {"type": "STRING"}, "observed_items": {"type": "ARRAY", "items": {"type": "STRING"}}, "review_message": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["observation_domain", "review_required", "observation_type", "observed_items", "review_message", "confidence"]},
                    "sensitivity_warning_observation": {"type": "OBJECT", "properties": {"observation_domain": {"type": "STRING"}, "review_required": {"type": "BOOLEAN"}, "observation_type": {"type": "STRING"}, "observed_items": {"type": "ARRAY", "items": {"type": "STRING"}}, "review_message": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}, "required": ["observation_domain", "review_required", "observation_type", "observed_items", "review_message", "confidence"]},
                    
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
                                "confidence": {"type": "NUMBER"}
                            },
                            "required": ["ingredient_name", "normalized_inci_name", "functions", "benefits", "is_key_ingredient", "key_ingredient_status", "confidence"]
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
        parsed_data = json.loads(candidate_text)
        
        # Ensure default array properties that model might omit
        parsed_data = BeautyProductEnrichmentSchema.model_validate(parsed_data).model_dump()
        
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
                parent_enrichment_run_id=run_id, attempt=attempt + 1
            )
            
        fallback_data = generate_deterministic_fallback(name, brand, description, raw_ingredients)
        return fallback_data, run_id
