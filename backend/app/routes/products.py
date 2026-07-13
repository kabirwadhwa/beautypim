import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_
from typing import List, Optional, Dict, Any
from app.database import get_db
from app.auth import get_current_user, require_editor_or_admin, require_viewer_or_above
from app.models import (
    CanonicalProduct, ProductVariant, Brand, Category, FieldValue, 
    ValidationIssue, AuditLog, User, Formulation
)
from app.schemas import ProductOut, ProductDetailOut, ProductEdit
from app.worker import record_audit, process_item_enrichment, create_field_value_version
from pydantic import BaseModel

class BulkActionRequest(BaseModel):
    product_ids: List[uuid.UUID]
    action: str

router = APIRouter(prefix="/products", tags=["Product PIM Center"])

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

    if search:
        search_term = f"%{search.lower()}%"
        query = query.filter(
            or_(
                CanonicalProduct.product_name.lower().like(search_term),
                Brand.name.lower().like(search_term)
            )
        )

    if status_filter:
        query = query.filter(CanonicalProduct.review_status == status_filter)

    if brand_filter:
        query = query.filter(Brand.name == brand_filter)

    if issue_filter is not None:
        if issue_filter:
            # Only products with unresolved validation issues
            query = query.filter(
                CanonicalProduct.id.in_(
                    db.query(ValidationIssue.canonical_product_id).filter(ValidationIssue.resolved == False)
                )
            )
        else:
            query = query.filter(
                ~CanonicalProduct.id.in_(
                    db.query(ValidationIssue.canonical_product_id).filter(ValidationIssue.resolved == False)
                )
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
            
        out.append(ProductOut(
            id=prod.id,
            product_name=prod.product_name,
            brand_name=prod.brand.name,
            category_path=category_path,
            review_status=prod.review_status,
            is_deleted=prod.is_deleted,
            created_at=prod.created_at,
            updated_at=prod.updated_at
        ))
    return out

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

    return ProductDetailOut(
        id=prod.id,
        product_name=prod.product_name,
        brand_id=prod.brand_id,
        brand_name=prod.brand.name,
        category_id=prod.category_id,
        category_path=category_path,
        review_status=prod.review_status,
        reviewer_id=prod.reviewer_id,
        is_deleted=prod.is_deleted,
        created_at=prod.created_at,
        updated_at=prod.updated_at,
        variants=variants,
        formulations=formulations,
        field_values=fields,
        validation_issues=issues
    )

@router.put("/{product_id}", response_model=ProductDetailOut)
def edit_product_field(
    product_id: uuid.UUID,
    edit_in: ProductEdit,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin)
):
    prod = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == product_id,
        CanonicalProduct.is_deleted == False
    ).first()
    if not prod:
        raise HTTPException(status_code=404, detail="Product not found")

    # Fetch previous value for audit log
    prev_fv = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product_id,
        FieldValue.field_name == edit_in.field_name,
        FieldValue.is_current == True
    ).first()
    before_val = prev_fv.value if prev_fv else None

    # Deactivate previous active value
    db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product_id,
        FieldValue.field_name == edit_in.field_name
    ).update({"is_current": False})

    # Save new human value
    new_fv = FieldValue(
        id=uuid.uuid4(),
        canonical_product_id=product_id,
        field_name=edit_in.field_name,
        value=edit_in.value,
        source_type="human_edit",
        source_reference=f"user_edit_by:{current_user.email}",
        confidence_score=1.0,
        review_status="confirmed",
        reviewer_id=current_user.id,
        is_current=True
    )
    db.add(new_fv)
    db.flush()

    # Record Audit event
    record_audit(
        db=db,
        entity_type="CanonicalProduct",
        entity_id=product_id,
        display_label=prod.product_name,
        action="update",
        before={edit_in.field_name: before_val},
        after={edit_in.field_name: edit_in.value},
        changed={edit_in.field_name: [before_val, edit_in.value]},
        user_id=current_user.id,
        actor_type="user",
        reason=edit_in.reason
    )

    db.commit()
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
