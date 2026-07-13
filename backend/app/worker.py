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
    run_id: Optional[uuid.UUID] = None
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
        is_current=is_current
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
        create_field_value_version(
            db=db,
            canonical_product_id=item.canonical_product_id,
            product_variant_id=None,
            field_name=field,
            value=field_data.get("value"),
            source_type="ai_inference",
            source_ref=source_ref,
            confidence=field_data.get("confidence", 0.0),
            status=field_data.get("value_status", "unknown"),
            run_id=run_id
        )

    core_claims_fields = [
        "vegan", "cruelty_free", "paraben_free", "sulfate_free", 
        "silicone_free", "alcohol_free", "fragrance_present"
    ]
    for field in core_claims_fields:
        field_data = enrichment_result.get(field, {})
        create_field_value_version(
            db=db,
            canonical_product_id=item.canonical_product_id,
            product_variant_id=None,
            field_name=field,
            value=field_data.get("value"),
            source_type="ai_inference",
            source_ref=source_ref,
            confidence=field_data.get("confidence", 0.0),
            status=field_data.get("claim_status", "unknown"),
            run_id=run_id
        )

    core_concerns_fields = [
        "hydration", "anti_ageing", "pigmentation", "acne", "redness", 
        "sensitivity", "scalp_care", "hair_growth", "fragrance", "freshness"
    ]
    for field in core_concerns_fields:
        field_data = enrichment_result.get(field, {})
        create_field_value_version(
            db=db,
            canonical_product_id=item.canonical_product_id,
            product_variant_id=None,
            field_name=field,
            value=field_data.get("targeting_status") == "explicit" or field_data.get("targeting_status") == "inferred",
            source_type="ai_inference",
            source_ref=source_ref,
            confidence=field_data.get("confidence", 0.0),
            status=field_data.get("targeting_status", "unknown"),
            run_id=run_id
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

        form_ing = FormulationIngredient(
            id=uuid.uuid4(),
            formulation_id=formulation.id,
            ingredient_definition_id=definition.id,
            raw_inci_name=ing.get("ingredient_name", ""),
            position=pos + 1,
            is_key_ingredient=ing.get("is_key_ingredient", False),
            evidence_source="ai_inference",
            confidence_score=ing.get("confidence", 0.0)
        )
        db.add(form_ing)

    # Validation Checks
    # Check 1: EAN missing validation warning
    variant = db.query(ProductVariant).filter(ProductVariant.id == item.product_variant_id).first()
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

    # Check 2: Low-confidence field validation warning
    low_conf_found = False
    for field, field_data in enrichment_result.items():
        if isinstance(field_data, dict) and "confidence" in field_data and field_data["confidence"] is not None:
            if field_data["confidence"] < 0.6:
                msg = f"Enriched field '{field}' has low confidence score ({field_data['confidence']})."
                issue = ValidationIssue(
                    id=uuid.uuid4(),
                    canonical_product_id=item.canonical_product_id,
                    field_name=field,
                    severity="informational",
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

    items = db.query(ImportJobItem).filter(
        ImportJobItem.import_job_id == job_id,
        ImportJobItem.status == "pending"
    ).all()

    for item in items:
        try:
            item.status = "processing"
            db.commit()

            listing = db.query(SourceListing).filter(SourceListing.id == item.source_listing_id).first()
            raw_data = listing.raw_data
            
            raw_name = str(raw_data.get(mapping.get("product_name", "")))
            raw_brand = str(raw_data.get(mapping.get("brand", "")))
            raw_ean = str(raw_data.get(mapping.get("ean", ""))) if mapping.get("ean") in raw_data else None
            raw_size = str(raw_data.get(mapping.get("size", ""))) if mapping.get("size") in raw_data else None
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
            item.status = "failed"
            item.enrichment_status = "permanent_failed"
            item.failure_code = "processing_error"
            item.failure_message = str(e)
            job.processed_rows += 1
            db.commit()
            logger.error(f"Failed to process row {item.source_row_number}: {str(e)}")

    job.status = "completed"
    db.commit()

def recover_unfinished_jobs():
    """Recovers and processes pending/processing jobs upon application startup.
    """
    db = SessionLocal()
    try:
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
            
            # Resume Job
            run_job_worker(db, job.id)
    except Exception as e:
        logger.error(f"Worker recovery failed: {str(e)}")
    finally:
        db.close()
