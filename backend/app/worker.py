import time
import uuid
import logging
import hashlib
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import (
    ImportJob, ImportJobItem, SourceListing, CanonicalProduct, 
    ProductVariant, Brand, Category, FieldValue, Formulation, 
    FormulationIngredient, IngredientDefinition, ValidationIssue, 
    AuditLog, SourcePrice
)
from app.services.deduplication import evaluate_match, normalize_text
from app.services.enrichment import run_ai_enrichment
from app.config import settings

logger = logging.getLogger("worker")

def record_audit(
    db: Session,
    entity_type: str,
    entity_id: uuid.UUID,
    display_label: str,
    action: str,
    before: Dict[str, Any],
    after: Dict[str, Any],
    changed: Dict[str, Any],
    user_id: Optional[uuid.UUID] = None,
    actor_type: str = "system",
    reason: Optional[str] = None
):
    audit = AuditLog(
        id=uuid.uuid4(),
        entity_type=entity_type,
        entity_id=entity_id,
        entity_display_label=display_label[:255] if display_label else None,
        user_id=user_id,
        actor_type=actor_type,
        action=action, # create, update, merge, approve, reject (lowercase)
        before_snapshot=before,
        after_snapshot=after,
        changed_fields=changed,
        reason=reason
    )
    db.add(audit)
    db.flush()

def is_unknown_or_not_applicable(value: Any, status: Optional[str]) -> bool:
    val_str = str(value).strip().lower() if value is not None else ""
    status_str = str(status).strip().lower() if status is not None else ""
    return (
        val_str in ["", "unknown", "none", "nan", "null", "not_applicable"] or
        status_str in ["unknown", "none", "nan", "null", "not_applicable"]
    )

def is_conflicting(value: Any, status: Optional[str]) -> bool:
    val_str = str(value).strip().lower() if value is not None else ""
    status_str = str(status).strip().lower() if status is not None else ""
    return val_str == "conflicting" or status_str == "conflicting"

def should_create_low_confidence_warning(
    field_name: str,
    value: Any,
    status: Optional[str],
    source_type: str,
    confidence: Optional[float]
) -> bool:
    from app.config import settings
    if source_type == "source_data":
        return False
    if is_unknown_or_not_applicable(value, status):
        return False
    if is_conflicting(value, status):
        return False
    if field_name not in settings.LOW_CONFIDENCE_FIELDS:
        return False
    if confidence is not None and confidence < settings.LOW_CONFIDENCE_THRESHOLD:
        return True
    return False

def map_ai_status_to_db(ai_status: str) -> str:
    mapping = {
        "explicit_brand_claim": "confirmed",
        "explicit_retailer_claim": "confirmed",
        "ingredient_based_inference": "inferred",
        "text_based_inference": "inferred",
        "explicit_source": "confirmed",
        "normalized_source": "confirmed",
        "inferred": "inferred",
        "explicit": "confirmed",
        "not_targeted": "confirmed",
        "confirmed": "confirmed",
        "conflicting": "conflicting",
        "unknown": "unknown",
        "not_applicable": "not_applicable"
    }
    return mapping.get(ai_status, "unknown")

def create_field_value_version(
    db: Session,
    canonical_product_id: Optional[uuid.UUID],
    product_variant_id: Optional[uuid.UUID],
    field_name: str,
    value: Any,
    source_type: str,
    source_ref: str,
    confidence: float,
    status: str,
    run_id: Optional[uuid.UUID] = None,
    evidence: Optional[list] = None,
    reasoning_summary: Optional[str] = None,
    semantic_status: Optional[str] = None,
    semantic_status_type: Optional[str] = None
):
    """Saves a candidate field value.
    Ensures that we do NOT overwrite or deactivate any existing human-approved edits.
    """
    db_status = map_ai_status_to_db(status)
    # Check for existing human edit
    existing_human = None
    if canonical_product_id:
        existing_human = db.query(FieldValue).filter(
            FieldValue.canonical_product_id == canonical_product_id,
            FieldValue.field_name == field_name,
            FieldValue.source_type == "human_edit",
            FieldValue.is_current == True
        ).first()
    elif product_variant_id:
        existing_human = db.query(FieldValue).filter(
            FieldValue.product_variant_id == product_variant_id,
            FieldValue.field_name == field_name,
            FieldValue.source_type == "human_edit",
            FieldValue.is_current == True
        ).first()

    # If active human edit exists, write new AI result as non-current candidate
    is_current = True
    if existing_human:
        is_current = False
        # Create a conflicting validation issue
        if existing_human.value != value:
            msg = f"Enrichment produced conflicting candidate value '{value}' for field '{field_name}' (Current human approved: '{existing_human.value}')."
            issue = ValidationIssue(
                id=uuid.uuid4(),
                canonical_product_id=canonical_product_id,
                field_name=field_name,
                severity="warning",
                issue_type="conflicting_information",
                message=msg,
                created_by_type="system"
            )
            db.add(issue)

    # Set existing current active values to False if writing new current active
    if is_current:
        if canonical_product_id:
            db.query(FieldValue).filter(
                FieldValue.canonical_product_id == canonical_product_id,
                FieldValue.field_name == field_name
            ).update({"is_current": False})
        elif product_variant_id:
            db.query(FieldValue).filter(
                FieldValue.product_variant_id == product_variant_id,
                FieldValue.field_name == field_name
            ).update({"is_current": False})
            
    field_record = FieldValue(
        id=uuid.uuid4(),
        canonical_product_id=canonical_product_id,
        product_variant_id=product_variant_id,
        field_name=field_name,
        value=value,
        source_type=source_type,
        source_reference=source_ref,
        confidence_score=confidence,
        review_status=db_status,
        enrichment_run_id=run_id,
        is_current=is_current,
        override_reason=None,
        evidence=evidence,
        reasoning_summary=reasoning_summary,
        semantic_status=semantic_status,
        semantic_status_type=semantic_status_type
    )
    db.add(field_record)

def process_item_enrichment(db: Session, item: ImportJobItem, mapping: Dict[str, str]):
    """Runs AI/rule enrichment on a matched canonical product and variant.
    """
    listing = db.query(SourceListing).filter(SourceListing.id == item.source_listing_id).first()
    if not listing:
        raise ValueError("Source listing not found")

    raw_data = listing.raw_data
    raw_name = str(raw_data.get(mapping.get("product_name", "")))
    raw_brand = str(raw_data.get(mapping.get("brand", "")))
    raw_desc = str(raw_data.get(mapping.get("description", "")))
    raw_ingr = str(raw_data.get(mapping.get("ingredients", "")))
    val_ean = raw_data.get(mapping.get("ean", ""))
    raw_ean = None if val_ean is None or str(val_ean).strip().lower() in ["", "none", "nan", "null"] else str(val_ean).strip()
    
    val_size = raw_data.get(mapping.get("size", ""))
    raw_size = None if val_size is None or str(val_size).strip().lower() in ["", "none", "nan", "null"] else str(val_size).strip()

    # Start Enrichment Run
    item.enrichment_status = "processing"
    item.started_at = datetime.utcnow()
    db.commit()

    # Trigger LLM/Rule Engine
    enrichment_result, run_id = run_ai_enrichment(
        db=db,
        name=raw_name,
        brand=raw_brand,
        description=raw_desc,
        raw_ingredients=raw_ingr,
        import_job_id=item.import_job_id,
        import_job_item_id=item.id,
        source_listing_id=listing.id,
        canonical_product_id=item.canonical_product_id,
        product_variant_id=item.product_variant_id
    )

    source_ref = f"source_listing_id:{listing.id}"

    # Write core enriched fields
    core_categorical_fields = [
        "subcategory", "product_type", "gender_target", "texture", 
        "application_area", "target_audience"
    ]
    for field in core_categorical_fields:
        field_data = enrichment_result.get(field, {})
        status = field_data.get("value_status", "unknown")
        create_field_value_version(
            db=db,
            canonical_product_id=item.canonical_product_id,
            product_variant_id=None,
            field_name=field,
            value=field_data.get("value"),
            source_type="ai_inference",
            source_ref=source_ref,
            confidence=field_data.get("confidence", 0.0),
            status=status,
            run_id=run_id,
            evidence=field_data.get("evidence", []),
            reasoning_summary=field_data.get("reasoning_summary"),
            semantic_status=status,
            semantic_status_type="value_status"
        )

    core_claims_fields = [
        "vegan", "cruelty_free", "paraben_free", "sulfate_free", 
        "silicone_free", "alcohol_free", "fragrance_present"
    ]
    for field in core_claims_fields:
        field_data = enrichment_result.get(field, {})
        status = field_data.get("claim_status", "unknown")
        create_field_value_version(
            db=db,
            canonical_product_id=item.canonical_product_id,
            product_variant_id=None,
            field_name=field,
            value=field_data.get("value"),
            source_type="ai_inference",
            source_ref=source_ref,
            confidence=field_data.get("confidence", 0.0),
            status=status,
            run_id=run_id,
            evidence=field_data.get("evidence", []),
            reasoning_summary=field_data.get("reasoning_summary"),
            semantic_status=status,
            semantic_status_type="claim_status"
        )

    core_concerns_fields = [
        "hydration", "anti_ageing", "pigmentation", "acne", "redness", 
        "sensitivity", "scalp_care", "hair_growth", "fragrance", "freshness"
    ]
    for field in core_concerns_fields:
        field_data = enrichment_result.get(field, {})
        status = field_data.get("targeting_status", "unknown")
        create_field_value_version(
            db=db,
            canonical_product_id=item.canonical_product_id,
            product_variant_id=None,
            field_name=field,
            value=field_data.get("targeting_status") == "explicit" or field_data.get("targeting_status") == "inferred",
            source_type="ai_inference",
            source_ref=source_ref,
            confidence=field_data.get("confidence", 0.0),
            status=status,
            run_id=run_id,
            evidence=field_data.get("evidence", []),
            reasoning_summary=field_data.get("reasoning_summary"),
            semantic_status=status,
            semantic_status_type="targeting_status"
        )

    # Write warning observations
    for field in ["pregnancy_warning_observation", "allergen_warning_observation"]:
        field_data = enrichment_result.get(field, {})
        if field_data:
            create_field_value_version(
                db=db,
                canonical_product_id=item.canonical_product_id,
                product_variant_id=None,
                field_name=field,
                value=field_data,
                source_type="ai_inference",
                source_ref=source_ref,
                confidence=field_data.get("confidence", 0.0),
                status="processed",
                run_id=run_id,
                evidence=field_data.get("evidence", []),
                reasoning_summary=field_data.get("review_message"),
                semantic_status="processed",
                semantic_status_type="observation_status"
            )

    # Save formulation
    content_hash = hashlib.sha256(raw_ingr.encode('utf-8')).hexdigest()
    formulation = Formulation(
        id=uuid.uuid4(),
        canonical_product_id=item.canonical_product_id,
        product_variant_id=item.product_variant_id,
        source_listing_id=listing.id,
        raw_inci_text=raw_ingr,
        market="global",
        language="en",
        content_hash=content_hash
    )
    db.add(formulation)
    db.flush()

    # Save formulation ingredients
    ai_ingredients = enrichment_result.get("ingredients_intelligence", [])
    for pos, ing in enumerate(ai_ingredients):
        # Check if in glossary
        norm_name = normalize_text(ing.get("ingredient_name", ""))
        definition = db.query(IngredientDefinition).filter(IngredientDefinition.normalized_name == norm_name).first()
        if not definition:
            definition = IngredientDefinition(
                id=uuid.uuid4(),
                name=ing.get("ingredient_name", ""),
                normalized_name=norm_name,
                common_name=ing.get("normalized_inci_name"),
                aliases=[],
                function=", ".join(ing.get("functions", [])),
                benefits=", ".join(ing.get("benefits", []))
            )
            db.add(definition)
            db.flush()

        evidence_list = ing.get("evidence", [])
        if hasattr(evidence_list, "model_dump"):
            evidence_list = [e.model_dump() for e in evidence_list]
        elif isinstance(evidence_list, list):
            evidence_list = [e if isinstance(e, dict) else (e.model_dump() if hasattr(e, "model_dump") else dict(e)) for e in evidence_list]

        form_ing = FormulationIngredient(
            id=uuid.uuid4(),
            formulation_id=formulation.id,
            ingredient_definition_id=definition.id,
            raw_inci_name=ing.get("ingredient_name", ""),
            position=pos + 1,
            is_key_ingredient=ing.get("is_key_ingredient", False),
            evidence_source="ai_inference",
            confidence_score=ing.get("confidence", 0.0),
            evidence=evidence_list,
            key_ingredient_status=ing.get("key_ingredient_status", "unknown")
        )
        db.add(form_ing)

    # Validation Checks
    # Clean/delete existing system validation issues for this item
    if item.canonical_product_id:
        db.query(ValidationIssue).filter(
            ValidationIssue.canonical_product_id == item.canonical_product_id,
            ValidationIssue.created_by_type == "system"
        ).delete()
    if item.product_variant_id:
        db.query(ValidationIssue).filter(
            ValidationIssue.product_variant_id == item.product_variant_id,
            ValidationIssue.created_by_type == "system"
        ).delete()

    from app.services.deduplication import normalize_volume
    import re

    variant = db.query(ProductVariant).filter(ProductVariant.id == item.product_variant_id).first()
    
    # 1. EAN missing warning
    if variant and not variant.gtin:
        issue = ValidationIssue(
            id=uuid.uuid4(),
            product_variant_id=item.product_variant_id,
            field_name="gtin",
            severity="warning",
            issue_type="missing_ean",
            message="Variant has no GTIN/EAN code.",
            created_by_type="system"
        )
        db.add(issue)
        
    # 2. Invalid GTIN
    if variant and variant.gtin:
        clean_gtin = variant.gtin.strip()
        if not (clean_gtin.isdigit() and len(clean_gtin) in [8, 12, 13, 14]):
            severity = "blocking" if settings.GTIN_MANDATORY else "warning"
            issue = ValidationIssue(
                id=uuid.uuid4(),
                product_variant_id=item.product_variant_id,
                field_name="gtin",
                severity=severity,
                issue_type="invalid_gtin",
                message=f"GTIN/EAN '{variant.gtin}' is invalid. Must be 8, 12, 13, or 14 digits.",
                created_by_type="system"
            )
            db.add(issue)

    # 3. Invalid URL
    raw_url = raw_data.get(mapping.get("product_url", ""))
    if raw_url:
        url_str = str(raw_url).strip()
        if url_str and not (url_str.startswith("http://") or url_str.startswith("https://")):
            issue = ValidationIssue(
                id=uuid.uuid4(),
                canonical_product_id=item.canonical_product_id,
                field_name="product_url",
                severity="warning",
                issue_type="invalid_url",
                message=f"Product URL '{raw_url}' is invalid.",
                created_by_type="system"
            )
            db.add(issue)

    # 4. Invalid price
    raw_price = raw_data.get(mapping.get("price", ""))
    if raw_price is not None:
        price_str = str(raw_price).strip()
        if price_str:
            try:
                price_val = float(price_str)
                if price_val <= 0:
                    raise ValueError()
            except ValueError:
                issue = ValidationIssue(
                    id=uuid.uuid4(),
                    canonical_product_id=item.canonical_product_id,
                    field_name="price",
                    severity="warning",
                    issue_type="invalid_price",
                    message=f"Price '{raw_price}' is invalid. Must be a positive number.",
                    created_by_type="system"
                )
                db.add(issue)

    # 5. Invalid size
    if raw_size:
        size_val = normalize_volume(raw_size)
        if size_val is None:
            issue = ValidationIssue(
                id=uuid.uuid4(),
                product_variant_id=item.product_variant_id,
                field_name="size",
                severity="warning",
                issue_type="invalid_size",
                message=f"Size/volume '{raw_size}' is invalid or cannot be parsed.",
                created_by_type="system"
            )
            db.add(issue)

    # 6. Fragrance-free claims vs Parfum in ingredients
    claims_str = str(raw_data.get("claims", "")).lower()
    desc_str = str(raw_data.get("description", "")).lower()
    ing_str = str(raw_data.get("ingredients", "")).lower()
    
    is_fragrance_free = "fragrance-free" in claims_str or "fragrance-free" in desc_str or "fragrance free" in claims_str or "fragrance free" in desc_str
    frag_pres = enrichment_result.get("fragrance_present", {})
    if isinstance(frag_pres, dict) and frag_pres.get("value") == "no":
        is_fragrance_free = True
        
    if is_fragrance_free:
        if any(x in ing_str for x in ["parfum", "fragrance", "perfume", "aroma"]):
            severity = "blocking" if "ingredients" in settings.MANDATORY_FIELDS else "warning"
            issue = ValidationIssue(
                id=uuid.uuid4(),
                canonical_product_id=item.canonical_product_id,
                field_name="ingredients",
                severity=severity,
                issue_type="conflicting_information",
                message="Product claims to be fragrance-free, but ingredients contain fragrance components (e.g. Parfum).",
                created_by_type="system"
            )
            db.add(issue)

    # 7. Alcohol-free claims vs Alcohol Denat. in ingredients
    is_alcohol_free = "alcohol-free" in claims_str or "alcohol-free" in desc_str or "alcohol free" in claims_str or "alcohol free" in desc_str
    alc_free_field = enrichment_result.get("alcohol_free", {})
    if isinstance(alc_free_field, dict) and alc_free_field.get("value") == "yes":
        is_alcohol_free = True
        
    if is_alcohol_free:
        has_drying_alcohol = False
        if "alcohol denat" in ing_str or "sd alcohol" in ing_str or "ethanol" in ing_str or "ethyl alcohol" in ing_str:
            has_drying_alcohol = True
        else:
            for m in re.finditer(r"\b(\w+\s+)?alcohol\b", ing_str):
                prefix = m.group(1) or ""
                prefix = prefix.strip()
                if prefix not in ["cetearyl", "cetyl", "stearyl", "behenyl", "benzyl", "lanolin", "myristyl", "isopropyl"]:
                    has_drying_alcohol = True
                    break
        if has_drying_alcohol:
            severity = "blocking" if "ingredients" in settings.MANDATORY_FIELDS else "warning"
            issue = ValidationIssue(
                id=uuid.uuid4(),
                canonical_product_id=item.canonical_product_id,
                field_name="ingredients",
                severity=severity,
                issue_type="conflicting_information",
                message="Product claims to be alcohol-free, but ingredients contain drying alcohols (e.g. Alcohol Denat.).",
                created_by_type="system"
            )
            db.add(issue)

    # 8. Missing brand (BLOCKING severity if configured as mandatory)
    if not raw_brand or raw_brand.strip().lower() in ["", "unknown", "missing"]:
        severity = "blocking" if "brand" in settings.MANDATORY_FIELDS else "warning"
        issue = ValidationIssue(
            id=uuid.uuid4(),
            canonical_product_id=item.canonical_product_id,
            field_name="brand",
            severity=severity,
            issue_type="missing_brand",
            message="Product brand is missing or unknown.",
            created_by_type="system"
        )
        db.add(issue)

    # 9. Sparse row
    is_sparse = False
    if not raw_name or raw_name.strip() == "":
        is_sparse = True
    else:
        empty_count = 0
        if not raw_desc or raw_desc.strip() == "": empty_count += 1
        if not raw_ingr or raw_ingr.strip() == "": empty_count += 1
        if not raw_ean or raw_ean.strip() == "": empty_count += 1
        if not raw_size or raw_size.strip() == "": empty_count += 1
        if empty_count >= 3:
            is_sparse = True
            
    if is_sparse:
        severity = "blocking" if "product_name" in settings.MANDATORY_FIELDS else "warning"
        issue = ValidationIssue(
            id=uuid.uuid4(),
            canonical_product_id=item.canonical_product_id,
            field_name="product_name",
            severity=severity,
            issue_type="sparse_row",
            message="Product row is sparse (missing crucial metadata fields).",
            created_by_type="system"
        )
        db.add(issue)

    # 10. Missing required Category check
    prod = db.query(CanonicalProduct).filter(CanonicalProduct.id == item.canonical_product_id).first()
    if prod and prod.category_id is None:
        severity = "blocking" if settings.CATEGORY_MANDATORY else "warning"
        issue = ValidationIssue(
            id=uuid.uuid4(),
            canonical_product_id=item.canonical_product_id,
            field_name="category_id",
            severity=severity,
            issue_type="missing_category",
            message="Product category is missing.",
            created_by_type="system"
        )
        db.add(issue)

    # Check 2: Low-confidence field validation warning and conflicts check
    for field, field_data in enrichment_result.items():
        if isinstance(field_data, dict):
            val = field_data.get("value")
            status = field_data.get("value_status") or field_data.get("claim_status") or field_data.get("targeting_status")
            confidence = field_data.get("confidence")
            
            # Check for conflict
            if is_conflicting(val, status):
                severity = "blocking" if field in settings.MANDATORY_FIELDS else "warning"
                issue = ValidationIssue(
                    id=uuid.uuid4(),
                    canonical_product_id=item.canonical_product_id,
                    field_name=field,
                    severity=severity,
                    issue_type="conflicting_information",
                    message=f"Enriched field '{field}' has conflicting values.",
                    created_by_type="system"
                )
                db.add(issue)
            # Check for low confidence warning on non-unknowns
            elif should_create_low_confidence_warning(field, val, status, "ai_inference", confidence):
                msg = f"Enriched field '{field}' has low confidence score ({confidence})."
                issue = ValidationIssue(
                    id=uuid.uuid4(),
                    canonical_product_id=item.canonical_product_id,
                    field_name=field,
                    severity="warning",
                    issue_type="low_confidence_enrichment",
                    message=msg,
                    created_by_type="system"
                )
                db.add(issue)

    item.enrichment_status = "succeeded"
    item.status = "completed"
    item.completed_at = datetime.utcnow()
    db.commit()

def run_job_worker(db: Session, job_id: uuid.UUID):
    """Executes the complete processing lifecycle for a single ImportJob.
    """
    job = db.query(ImportJob).filter(ImportJob.id == job_id).first()
    if not job:
        return

    job.status = "processing"
    db.commit()

    mapping = job.column_mapping

    # First run stale job recovery to make sure no items are orphaned
    recover_stale_job_items(db)

    while True:
        # Atomic claim of a single item
        if db.bind.dialect.name == "postgresql":
            item = db.query(ImportJobItem).filter(
                ImportJobItem.import_job_id == job_id,
                ImportJobItem.status == "pending"
            ).with_for_update(skip_locked=True).first()
        else:
            item = db.query(ImportJobItem).filter(
                ImportJobItem.import_job_id == job_id,
                ImportJobItem.status == "pending"
            ).first()

        if not item:
            break

        try:
            item.status = "processing"
            item.started_at = datetime.utcnow()
            db.commit()

            listing = db.query(SourceListing).filter(SourceListing.id == item.source_listing_id).first()
            raw_data = listing.raw_data
            
            raw_name = str(raw_data.get(mapping.get("product_name", "")))
            raw_brand = str(raw_data.get(mapping.get("brand", "")))
            
            val_ean = raw_data.get(mapping.get("ean", ""))
            raw_ean = None if val_ean is None or str(val_ean).strip().lower() in ["", "none", "nan", "null"] else str(val_ean).strip()
            
            val_size = raw_data.get(mapping.get("size", ""))
            raw_size = None if val_size is None or str(val_size).strip().lower() in ["", "none", "nan", "null"] else str(val_size).strip()
            
            raw_price = raw_data.get(mapping.get("price", ""))
            
            # Step 1: Matching / Deduplication
            match_status, score, matched_canonical_id, matched_variant_id = evaluate_match(
                db=db,
                raw_name=raw_name,
                raw_brand=raw_brand,
                raw_gtin=raw_ean,
                raw_size=raw_size
            )

            item.match_status = match_status
            item.duplicate_score = score

            if match_status == "ambiguous":
                # Must halt processing for human review
                item.status = "awaiting_match_review"
                db.commit()
                continue
                
            elif match_status in ["exact_match", "deterministic_match", "candidate"]:
                # Auto-create missing variant if size is new
                if not matched_variant_id and matched_canonical_id:
                    # check if a variant with the same size/gtin already exists
                    variant = None
                    if raw_ean:
                        variant = db.query(ProductVariant).filter(
                            ProductVariant.gtin == raw_ean,
                            ProductVariant.is_deleted == False
                        ).first()
                    if not variant and raw_size:
                        # Find by equivalent size
                        from app.services.deduplication import is_size_equivalent
                        all_vars = db.query(ProductVariant).filter(
                            ProductVariant.canonical_product_id == matched_canonical_id,
                            ProductVariant.is_deleted == False
                        ).all()
                        for v in all_vars:
                            if is_size_equivalent(v.size, raw_size):
                                variant = v
                                break
                    
                    if not variant:
                        variant = ProductVariant(
                            id=uuid.uuid4(),
                            canonical_product_id=matched_canonical_id,
                            variant_name=raw_size or "Standard Size",
                            gtin=raw_ean,
                            size=raw_size
                        )
                        db.add(variant)
                        db.flush()
                    matched_variant_id = variant.id

                item.canonical_product_id = matched_canonical_id
                item.product_variant_id = matched_variant_id
                item.status = "enriching"
                db.commit()
                
            else: # new_product
                # Find or create Brand
                norm_brand = normalize_text(raw_brand)
                brand = db.query(Brand).filter(Brand.normalized_name == norm_brand).first()
                if not brand:
                    brand = Brand(
                        id=uuid.uuid4(),
                        name=raw_brand,
                        normalized_name=norm_brand
                    )
                    db.add(brand)
                    db.flush()

                # Create new Canonical Product
                canonical = CanonicalProduct(
                    id=uuid.uuid4(),
                    brand_id=brand.id,
                    product_name=raw_name,
                    normalized_name=normalize_text(raw_name),
                    review_status="imported"
                )
                db.add(canonical)
                db.flush()

                # Create new Variant
                variant = ProductVariant(
                    id=uuid.uuid4(),
                    canonical_product_id=canonical.id,
                    variant_name=raw_size,
                    gtin=raw_ean,
                    size=raw_size
                )
                db.add(variant)
                db.flush()

                # Save price to source context
                if raw_price:
                    try:
                        price_num = float(raw_price)
                        price_rec = SourcePrice(
                            id=uuid.uuid4(),
                            source_listing_id=listing.id,
                            product_variant_id=variant.id,
                            amount=price_num,
                            currency="EUR"
                        )
                        db.add(price_rec)
                    except ValueError:
                        pass

                item.canonical_product_id = canonical.id
                item.product_variant_id = variant.id
                item.status = "enriching"
                db.commit()

                record_audit(
                    db=db,
                    entity_type="CanonicalProduct",
                    entity_id=canonical.id,
                    display_label=raw_name,
                    action="create",
                    before={},
                    after={"product_name": raw_name, "brand": raw_brand},
                    changed={}
                )

            # Step 2: Enqueue for AI Enrichment
            process_item_enrichment(db, item, mapping)
            
            # Update Listing link
            listing.canonical_product_id = item.canonical_product_id
            listing.product_variant_id = item.product_variant_id
            
            job.processed_rows += 1
            db.commit()

        except Exception as e:
            db.rollback()
            try:
                # Refresh item/job reference after rollback
                item = db.query(ImportJobItem).filter(ImportJobItem.id == item.id).first()
                job = db.query(ImportJob).filter(ImportJob.id == job_id).first()
                if item and job:
                    item.status = "failed"
                    item.enrichment_status = "permanent_failed"
                    item.failure_code = "processing_error"
                    item.failure_message = str(e)
                    job.processed_rows += 1
                    db.commit()
            except Exception as inner_e:
                logger.error(f"Failed to save failure status for row: {str(inner_e)}")
            logger.error(f"Failed to process row {item.source_row_number if item else 'unknown'}: {str(e)}")

    job.status = "completed"
    db.commit()

def recover_stale_job_items(db: Session, timeout_seconds: int = 600):
    """Finds items stuck in 'processing' or 'enriching' for longer than the timeout and resets them back to 'pending'."""
    cutoff = datetime.utcnow() - timedelta(seconds=timeout_seconds)
    stale_items = db.query(ImportJobItem).filter(
        ImportJobItem.status.in_(["processing", "enriching"]),
        ImportJobItem.updated_at <= cutoff
    ).all()
    for item in stale_items:
        item.status = "pending"
        item.enrichment_status = "not_requested"
        logger.warning(f"Reset stale ImportJobItem {item.id} back to pending (timeout exceeded)")
    if stale_items:
        db.commit()

def run_job_in_background(job_id):
    db = SessionLocal()
    try:
        run_job_worker(db, job_id)
    except Exception as e:
        logger.error(f"Failed background job execution for {job_id}: {str(e)}")
    finally:
        db.close()

def recover_unfinished_jobs():
    """Recovers and processes pending/processing jobs upon application startup.
    """
    db = SessionLocal()
    try:
        recover_stale_job_items(db)
        
        # Find crashed jobs
        jobs = db.query(ImportJob).filter(
            ImportJob.status.in_(["pending", "processing"])
        ).all()
        
        for job in jobs:
            # Set items that were interrupted during execution back to pending
            db.query(ImportJobItem).filter(
                ImportJobItem.import_job_id == job.id,
                ImportJobItem.status.in_(["processing", "enriching"])
            ).update({"status": "pending"})
            db.commit()
            
            # Resume Job in a background thread with a fresh DB session
            import threading
            thread = threading.Thread(target=run_job_in_background, args=(job.id,))
            thread.start()
    except Exception as e:
        logger.error(f"Worker recovery failed: {str(e)}")
    finally:
        db.close()
