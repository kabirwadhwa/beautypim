import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, func
from typing import List, Optional, Dict, Any
from app.database import get_db
from app.auth import get_current_user, require_editor_or_admin, require_viewer_or_above
from app.models import (
    CanonicalProduct, ProductVariant, Brand, Category, FieldValue, 
    ValidationIssue, AuditLog, User, Formulation, ImportJob, ImportJobItem
)
from app.schemas import (
    ProductOut, ProductDetailOut, ProductEdit, FieldEnrichmentMetadataOut,
    FieldValueOut, EnrichmentMetadataSchema, KeyIngredientOut, DynamicConcernOut,
    EDITABLE_FIELDS_REGISTRY, ProductCategoryUpdate
)
from app.worker import record_audit, process_item_enrichment, create_field_value_version
from pydantic import BaseModel

from app.limiter import rate_limit
from app.config import settings

class BulkActionRequest(BaseModel):
    product_ids: List[uuid.UUID]
    action: str

router = APIRouter(prefix="/products", tags=["Product PIM Center"])

def product_internal_code(product_id: uuid.UUID) -> str:
    return f"ICN-{product_id.hex.upper()}"

@router.get("", response_model=List[ProductOut])
def list_products(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    search: Optional[str] = None,
    status_filter: Optional[str] = None,
    brand_filter: Optional[str] = None,
    issue_filter: Optional[bool] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_viewer_or_above)
):
    query = db.query(CanonicalProduct).join(Brand).filter(CanonicalProduct.is_deleted == False)

    if search and search.strip():
        raw_search = search.strip()
        search_term = f"%{raw_search.lower()}%"
        search_conditions = [
            func.lower(CanonicalProduct.product_name).like(search_term),
            func.lower(Brand.name).like(search_term),
            CanonicalProduct.id.in_(
                db.query(ProductVariant.canonical_product_id).filter(
                    func.lower(ProductVariant.gtin).like(search_term),
                    ProductVariant.is_deleted == False,
                )
            ),
        ]
        normalized_icn = raw_search.upper().removeprefix("ICN-").replace("-", "")
        if len(normalized_icn) == 32:
            try:
                search_conditions.append(CanonicalProduct.id == uuid.UUID(hex=normalized_icn))
            except ValueError:
                pass
        query = query.filter(or_(*search_conditions))

    if status_filter:
        query = query.filter(CanonicalProduct.review_status == status_filter)

    if brand_filter:
        query = query.filter(Brand.name == brand_filter)

    if issue_filter is not None:
        canonical_issue_ids = db.query(ValidationIssue.canonical_product_id).filter(
            ValidationIssue.resolved == False,
            ValidationIssue.canonical_product_id.isnot(None),
        )
        variant_issue_ids = (
            db.query(ProductVariant.canonical_product_id)
            .join(ValidationIssue, ValidationIssue.product_variant_id == ProductVariant.id)
            .filter(
                ValidationIssue.resolved == False,
                ProductVariant.is_deleted == False,
            )
        )
        if issue_filter:
            query = query.filter(or_(
                CanonicalProduct.id.in_(canonical_issue_ids),
                CanonicalProduct.id.in_(variant_issue_ids),
            ))
        else:
            query = query.filter(
                ~CanonicalProduct.id.in_(canonical_issue_ids),
                ~CanonicalProduct.id.in_(variant_issue_ids),
            )

    offset = (page - 1) * limit
    products = query.order_by(CanonicalProduct.created_at.desc()).offset(offset).limit(limit).all()

    # Format output items with Brand and Category titles
    out = []
    for prod in products:
        category_path = None
        if prod.category_id:
            cat = db.query(Category).filter(Category.id == prod.category_id).first()
            category_path = cat.path if cat else None
            
        variant = db.query(ProductVariant).filter(
            ProductVariant.canonical_product_id == prod.id,
            ProductVariant.is_deleted == False,
        ).order_by(ProductVariant.created_at.asc()).first()
        issues = (
            db.query(ValidationIssue)
            .outerjoin(ProductVariant, ValidationIssue.product_variant_id == ProductVariant.id)
            .filter(
                ValidationIssue.resolved == False,
                or_(
                    ValidationIssue.canonical_product_id == prod.id,
                    ProductVariant.canonical_product_id == prod.id,
                ),
            )
            .all()
        )
        severity_rank = {"blocking": 3, "error": 2, "warning": 1, "info": 0}
        highest_severity = max(
            (issue.severity for issue in issues),
            key=lambda value: severity_rank.get(value, 0),
            default=None,
        )
        out.append(ProductOut(
            id=prod.id,
            internal_code=product_internal_code(prod.id),
            product_name=prod.product_name,
            brand_name=prod.brand.name,
            category_path=category_path,
            gtin=variant.gtin if variant else None,
            review_status=prod.review_status,
            validation_issue_count=len(issues),
            highest_issue_severity=highest_severity,
            is_deleted=prod.is_deleted,
            created_at=prod.created_at,
            updated_at=prod.updated_at
        ))
    return out

@router.post("/{product_id}/re-enrich", response_model=ProductDetailOut)
def re_enrich_product(
    product_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin),
):
    """Re-run enrichment from the most recent source record for this product."""
    product = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == product_id,
        CanonicalProduct.is_deleted == False,
    ).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    item = db.query(ImportJobItem).filter(
        ImportJobItem.canonical_product_id == product_id,
        ImportJobItem.source_listing_id.isnot(None),
    ).order_by(ImportJobItem.created_at.desc()).first()
    if not item:
        raise HTTPException(
            status_code=409,
            detail="No source record is available for re-enrichment.",
        )
    job = db.query(ImportJob).filter(ImportJob.id == item.import_job_id).first()
    if not job:
        raise HTTPException(status_code=409, detail="The source import job is unavailable.")

    try:
        process_item_enrichment(db, item, job.column_mapping or {})
        record_audit(
            db=db,
            entity_type="CanonicalProduct",
            entity_id=product.id,
            display_label=product.product_name,
            action="re_enrich",
            before={"enrichment_status": "existing"},
            after={"enrichment_status": "completed"},
            changed={"enrichment": ["existing", "regenerated"]},
            user_id=current_user.id,
            actor_type="user",
            reason="Manual re-enrichment from the latest source record.",
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Re-enrichment failed: {exc}")

    return get_product_detail(product_id, db, current_user)

@router.get("/{product_id}", response_model=ProductDetailOut)
def get_product_detail(
    product_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_viewer_or_above)
):
    prod = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == product_id,
        CanonicalProduct.is_deleted == False
    ).first()
    if not prod:
        raise HTTPException(status_code=404, detail="Product not found")

    brand_name = prod.brand.name if prod.brand else None

    # Fetch Category Path
    category_path = None
    if prod.category_id:
        cat = db.query(Category).filter(Category.id == prod.category_id).first()
        category_path = cat.path if cat else None

    # Fetch Variants
    variants = db.query(ProductVariant).filter(
        ProductVariant.canonical_product_id == product_id,
        ProductVariant.is_deleted == False
    ).all()

    # Fetch Formulations
    formulations = db.query(Formulation).filter(
        Formulation.canonical_product_id == product_id,
        Formulation.is_deleted == False
    ).all()

    # Fetch field values
    fields = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product_id
    ).all()

    # Fetch Validation Issues
    issues = db.query(ValidationIssue).filter(
        ValidationIssue.canonical_product_id == product_id
    ).all()

    # Expose per-field enrichment metadata & map schemas
    # Cache enrichment runs by run ID to avoid redundant db queries
    run_cache = {}
    from app.models import EnrichmentRun
    import json
    
    fields_out = []
    for fv in fields:
        meta_out = None
        if fv.enrichment_run_id:
            if fv.enrichment_run_id not in run_cache:
                run = db.query(EnrichmentRun).filter(EnrichmentRun.id == fv.enrichment_run_id).first()
                run_cache[fv.enrichment_run_id] = run
            run_rec = run_cache[fv.enrichment_run_id]
            if run_rec:
                meta_out = FieldEnrichmentMetadataOut(
                    enrichment_run_id=run_rec.id,
                    provider=run_rec.provider,
                    model=run_rec.model,
                    model_version=run_rec.model_version,
                    prompt_version=run_rec.prompt_version,
                    schema_version=run_rec.schema_version,
                    created_at=run_rec.created_at
                )
        
        # Prepare evidence list gracefully
        ev_list = []
        if fv.evidence:
            if isinstance(fv.evidence, str):
                try:
                    ev_list = json.loads(fv.evidence)
                except Exception:
                    ev_list = []
            elif isinstance(fv.evidence, list):
                ev_list = fv.evidence
        
        fields_out.append(FieldValueOut(
            id=fv.id,
            field_name=fv.field_name,
            value=fv.value,
            source_type=fv.source_type,
            source_reference=fv.source_reference,
            confidence_score=float(fv.confidence_score) if fv.confidence_score is not None else None,
            review_status=fv.review_status,
            reviewer_id=fv.reviewer_id,
            enrichment_run_id=fv.enrichment_run_id,
            is_current=fv.is_current,
            created_at=fv.created_at,
            updated_at=fv.updated_at,
            override_reason=fv.override_reason,
            evidence=ev_list,
            reasoning_summary=fv.reasoning_summary,
            semantic_status=fv.semantic_status,
            semantic_status_type=fv.semantic_status_type,
            enrichment_run=meta_out
        ))

    # Fetch global enrichment metadata from the latest run
    latest_run = db.query(EnrichmentRun).filter(
        EnrichmentRun.canonical_product_id == product_id
    ).order_by(EnrichmentRun.created_at.desc()).first()
    
    global_meta = None
    if latest_run:
        global_meta = EnrichmentMetadataSchema(
            provider=latest_run.provider,
            model=latest_run.model,
            prompt_version=latest_run.prompt_version,
            schema_version=latest_run.schema_version,
            status=latest_run.status,
            tokens=(latest_run.prompt_tokens or 0) + (latest_run.completion_tokens or 0),
            processing_time_ms=latest_run.processing_time_ms,
            created_at=latest_run.created_at
        )

    # Key Ingredients list construction from persisted FormulationIngredient table
    key_ingredients_out = []
    from app.models import FormulationIngredient, IngredientDefinition
    for f in formulations:
        f_ings = db.query(FormulationIngredient).filter(
            FormulationIngredient.formulation_id == f.id
        ).all()
        for fi in f_ings:
            defn = db.query(IngredientDefinition).filter(
                IngredientDefinition.id == fi.ingredient_definition_id
            ).first()
            if defn:
                funcs = [fn.strip() for fn in defn.function.split(",")] if defn.function else []
                bens = [bn.strip() for bn in defn.benefits.split(",")] if defn.benefits else []
                
                # Ingredient source mapping: lowercase controlled source values
                mapped_source = "unknown"
                if fi.evidence_source:
                    src_lower = str(fi.evidence_source).strip().lower()
                    if src_lower in ["source_data", "ai_inference", "human_edit"]:
                        mapped_source = src_lower
                        
                # evidence list parsing
                fi_ev = []
                if fi.evidence:
                    if isinstance(fi.evidence, str):
                        try:
                            fi_ev = json.loads(fi.evidence)
                        except Exception:
                            fi_ev = []
                    elif isinstance(fi.evidence, list):
                        fi_ev = fi.evidence

                key_ingredients_out.append(KeyIngredientOut(
                    name=fi.raw_inci_name,
                    normalized_inci_name=defn.common_name or defn.name,
                    functions=[fn for fn in funcs if fn],
                    benefits=[bn for bn in bens if bn],
                    is_key_ingredient=fi.is_key_ingredient,
                    key_ingredient_status=fi.key_ingredient_status,
                    source_type=mapped_source,
                    evidence=fi_ev,
                    confidence=float(fi.confidence_score) if fi.confidence_score is not None else None,
                    formulation_reference=f.id
                ))

    # Dynamic Concern targeting list from persisted FieldValues
    concern_fields = [
        "hydration", "anti_ageing", "pigmentation", "acne", "redness", 
        "sensitivity", "scalp_care", "hair_growth", "fragrance", "freshness"
    ]
    concerns_out = []
    for fv in fields:
        if fv.is_current and fv.field_name in concern_fields:
            # evidence list parsing
            fv_ev = []
            if fv.evidence:
                if isinstance(fv.evidence, str):
                    try:
                        fv_ev = json.loads(fv.evidence)
                    except Exception:
                        fv_ev = []
                elif isinstance(fv.evidence, list):
                    fv_ev = fv.evidence
            
            # Map semantic status
            targeting_val = fv.semantic_status or "unknown"
            
            concerns_out.append(DynamicConcernOut(
                concern_name=fv.field_name,
                targeting_status=targeting_val,
                evidence=fv_ev,
                confidence=float(fv.confidence_score) if fv.confidence_score is not None else None,
                source=fv.source_type
            ))

    return ProductDetailOut(
        id=prod.id,
        internal_code=product_internal_code(prod.id),
        product_name=prod.product_name,
        brand_id=prod.brand_id,
        brand_name=brand_name,
        category_id=prod.category_id,
        category_path=category_path,
        gtin=variants[0].gtin if variants else None,
        review_status=prod.review_status,
        validation_issue_count=len([issue for issue in issues if not issue.resolved]),
        highest_issue_severity=max(
            (issue.severity for issue in issues if not issue.resolved),
            key=lambda value: {"blocking": 3, "error": 2, "warning": 1, "info": 0}.get(value, 0),
            default=None,
        ),
        reviewer_id=prod.reviewer_id,
        is_deleted=prod.is_deleted,
        created_at=prod.created_at,
        updated_at=prod.updated_at,
        variants=variants,
        formulations=formulations,
        field_values=fields_out,
        validation_issues=issues,
        enrichment_metadata=global_meta,
        key_ingredients=key_ingredients_out,
        dynamic_concerns=concerns_out
    )

@router.put("/{product_id}/category", response_model=ProductDetailOut)
def update_product_category(
    product_id: uuid.UUID,
    payload: ProductCategoryUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin),
):
    product = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == product_id,
        CanonicalProduct.is_deleted == False,
    ).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found.")
    if payload.category_id and not db.query(Category).filter(Category.id == payload.category_id).first():
        raise HTTPException(status_code=404, detail="Category not found.")
    before = str(product.category_id) if product.category_id else None
    product.category_id = payload.category_id
    record_audit(
        db,
        entity_type="canonical_product",
        entity_id=product.id,
        action="category_updated",
        changed={"category_id": [before, str(payload.category_id) if payload.category_id else None]},
        user_id=current_user.id,
        actor_type="user",
    )
    db.commit()
    return get_product_detail(product_id, db, current_user)

@router.put("/{product_id}", response_model=ProductDetailOut, dependencies=[Depends(rate_limit("edit_product", "30/minute"))])
def edit_product_field(
    product_id: uuid.UUID,
    edit_in: ProductEdit,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin)
):
    from sqlalchemy.exc import IntegrityError
    
    # 1. Reject overrides on soft-deleted products
    prod = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == product_id
    ).first()
    if not prod or prod.is_deleted:
        raise HTTPException(status_code=404, detail="Product not found or deleted.")

    # 2. Check if user is editor or admin
    if current_user.role not in ["editor", "admin"]:
        raise HTTPException(status_code=403, detail="Viewer role is not allowed to override values.")

    # 3. Validation: Field name registry check
    if edit_in.field_name not in EDITABLE_FIELDS_REGISTRY:
        raise HTTPException(status_code=400, detail=f"Field '{edit_in.field_name}' is not editable or unrecognized.")

    # 4. Validation: Type check value
    expected_type = EDITABLE_FIELDS_REGISTRY[edit_in.field_name]
    if not isinstance(edit_in.value, expected_type):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid value type for '{edit_in.field_name}'. Expected {expected_type.__name__}."
        )

    # 5. Validation: Override reason blank check
    if not edit_in.reason or not edit_in.reason.strip():
        raise HTTPException(status_code=400, detail="Override reason must not be blank.")

    # 6. Validation: Override reason length check
    if len(edit_in.reason) > settings.MAX_OVERRIDE_REASON_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Override reason exceeds maximum length of {settings.MAX_OVERRIDE_REASON_LENGTH} characters."
        )

    # ACID lock and update block
    try:
        # Acquire lock to prevent concurrent races
        db.query(CanonicalProduct).filter(CanonicalProduct.id == product_id).with_for_update().first()

        # Fetch previous current value
        prev_fv = db.query(FieldValue).filter(
            FieldValue.canonical_product_id == product_id,
            FieldValue.field_name == edit_in.field_name,
            FieldValue.is_current == True
        ).first()

        before_val = prev_fv.value if prev_fv else None

        # Validation: Unchanged value check
        if prev_fv and prev_fv.value == edit_in.value:
            raise HTTPException(status_code=400, detail="New value must be different from current value.")

        # Deactivate previous active value
        if prev_fv:
            prev_fv.is_current = False
            # Use flush to verify deactivation
            db.flush()

        # Save new human value version
        new_fv = FieldValue(
            id=uuid.uuid4(),
            canonical_product_id=product_id,
            field_name=edit_in.field_name,
            value=edit_in.value,
            source_type="human_edit",
            source_reference=f"user:{current_user.id}",
            confidence_score=None, # Human edits do not have AI confidence scores
            review_status="confirmed",
            reviewer_id=current_user.id,
            override_reason=edit_in.reason,
            is_current=True
        )
        db.add(new_fv)
        db.flush()

        # Record Audit event (flushes to verify constraints)
        record_audit(
            db=db,
            entity_type="FieldValue",
            entity_id=new_fv.id,
            display_label=edit_in.field_name,
            action="override",
            before={"value": before_val},
            after={"value": edit_in.value},
            changed={edit_in.field_name: [before_val, edit_in.value]},
            user_id=current_user.id,
            actor_type="user",
            reason=edit_in.reason
        )

        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Concurrent override conflict occurred. Please retry."
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to override field: {str(e)}")

    return get_product_detail(product_id, db, current_user)

@router.post("/{product_id}/approve", response_model=ProductDetailOut)
def approve_product(
    product_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin)
):
    prod = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == product_id,
        CanonicalProduct.is_deleted == False
    ).first()
    if not prod:
        raise HTTPException(status_code=404, detail="Product not found")

    # Enforce Check: Blocking validation issue must prevent approval
    blocking_issue = db.query(ValidationIssue).filter(
        ValidationIssue.canonical_product_id == product_id,
        ValidationIssue.severity == "blocking",
        ValidationIssue.resolved == False
    ).first()
    
    if blocking_issue:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot approve product. Active blocking validation issue exists: '{blocking_issue.message}'"
        )

    before_status = prod.review_status
    prod.review_status = "approved"
    prod.reviewer_id = current_user.id
    prod.updated_at = datetime.utcnow()

    # Record audit log
    record_audit(
        db=db,
        entity_type="CanonicalProduct",
        entity_id=product_id,
        display_label=prod.product_name,
        action="approve",
        before={"status": before_status},
        after={"status": "approved"},
        changed={"status": [before_status, "approved"]},
        user_id=current_user.id,
        actor_type="user"
    )

    db.commit()
    return get_product_detail(product_id, db, current_user)

@router.post("/{product_id}/reject", response_model=ProductDetailOut)
def reject_product(
    product_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin)
):
    prod = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == product_id,
        CanonicalProduct.is_deleted == False
    ).first()
    if not prod:
        raise HTTPException(status_code=404, detail="Product not found")

    before_status = prod.review_status
    prod.review_status = "rejected"
    prod.reviewer_id = current_user.id
    prod.updated_at = datetime.utcnow()

    record_audit(
        db=db,
        entity_type="CanonicalProduct",
        entity_id=product_id,
        display_label=prod.product_name,
        action="reject",
        before={"status": before_status},
        after={"status": "rejected"},
        changed={"status": [before_status, "rejected"]},
        user_id=current_user.id,
        actor_type="user"
    )

    db.commit()
    return get_product_detail(product_id, db, current_user)

@router.post("/bulk-action", status_code=status.HTTP_200_OK)
def bulk_product_action(
    req: BulkActionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin)
):
    product_ids = req.product_ids
    action = req.action

    if action not in ["approve", "reject"]:
        raise HTTPException(status_code=400, detail="Invalid action name")

    success_count = 0
    errors = []

    for pid in product_ids:
        try:
            if action == "approve":
                approve_product(pid, db, current_user)
            elif action == "reject":
                reject_product(pid, db, current_user)
            success_count += 1
        except HTTPException as e:
            errors.append({"product_id": str(pid), "error": e.detail})
        except Exception as e:
            errors.append({"product_id": str(pid), "error": str(e)})

    return {
        "action": action,
        "success_count": success_count,
        "failed_count": len(product_ids) - success_count,
        "errors": errors
    }
