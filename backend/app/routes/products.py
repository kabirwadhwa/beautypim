import uuid
import re
from urllib.parse import urlparse
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, func
from typing import List, Optional, Dict, Any
from app.database import get_db
from app.auth import get_current_user, require_editor_or_admin, require_viewer_or_above
from app.models import (
    CanonicalProduct, ProductVariant, Brand, Category, FieldValue, 
    ValidationIssue, AuditLog, User, Formulation, ImportJob, ImportJobItem, SourceListing,
    ScrapedProductObservation, CrawlJob, SourcePrice, ProductTag
)
from app.schemas import (
    ProductOut, ProductDetailOut, ProductEdit, FieldEnrichmentMetadataOut,
    FieldValueOut, EnrichmentMetadataSchema, KeyIngredientOut, DynamicConcernOut,
    EDITABLE_FIELDS_REGISTRY, ProductCategoryUpdate, ProductClassificationUpdate, ProductImageUpdate,
    ProductTagsUpdate,
)
from app.worker import record_audit, process_item_enrichment, create_field_value_version
from pydantic import BaseModel, Field

from app.limiter import rate_limit
from app.config import settings
from app.services.deduplication import normalize_text

class BulkActionRequest(BaseModel):
    product_ids: List[uuid.UUID] = Field(default_factory=list)
    product_variant_ids: List[uuid.UUID] = Field(default_factory=list)
    action: str
    category: Optional[str] = None
    subcategory: Optional[str] = None
    tags: List[str] = Field(default_factory=list, max_length=20)


class BulkImproveRequest(BaseModel):
    product_ids: List[uuid.UUID] = Field(default_factory=list)
    product_variant_ids: List[uuid.UUID] = Field(default_factory=list)
    mode: str = "missing_only"


class BulkImproveStatusRequest(BaseModel):
    research_job_ids: List[uuid.UUID]


class ProductImproveRequest(BaseModel):
    mode: str = "missing_only"
    fields: List[str] = Field(default_factory=list)


class ProductIdentityUpdateRequest(BaseModel):
    brand: Optional[str] = None
    product_name: Optional[str] = None
    product_family: Optional[str] = None
    format: Optional[str] = None
    variant: Optional[str] = None
    size: Optional[str] = None
    unit: Optional[str] = None
    gtin: Optional[str] = None
    market: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    product_type: Optional[str] = None
    application_area: Optional[str] = None
    category_module: Optional[str] = None


class IdentityReviewConfirmRequest(BaseModel):
    identity: ProductIdentityUpdateRequest
    action: str = "confirm_and_continue"
    understanding_fingerprint: Optional[str] = None
    resume_context: Dict[str, Any] = Field(default_factory=dict)


class IdentityReviewSkipRequest(BaseModel):
    understanding_fingerprint: Optional[str] = None
    resume_context: Dict[str, Any] = Field(default_factory=dict)


class ProductResearchRequest(BaseModel):
    urls: List[str]
    use_browser_rendering: bool = False
    refresh_interval_hours: Optional[int] = None


class ProductSourceDiscoveryRequest(BaseModel):
    approved_domains: List[str] = []

router = APIRouter(prefix="/products", tags=["Product PIM Center"])


def _normalize_tags(tags: List[str]) -> list[tuple[str, str]]:
    normalized: dict[str, str] = {}
    for raw in tags:
        name = " ".join(str(raw or "").strip().split())
        if not name:
            continue
        if len(name) > 50:
            raise HTTPException(422, detail="Tags must be 50 characters or fewer.")
        key = name.casefold()
        normalized.setdefault(key, name)
    if len(normalized) > 20:
        raise HTTPException(422, detail="A product can have at most 20 tags.")
    return list(normalized.items())


def _tag_names(db: Session, product_id: uuid.UUID) -> list[str]:
    return [row.name for row in db.query(ProductTag).filter(
        ProductTag.canonical_product_id == product_id,
    ).order_by(ProductTag.normalized_name).all()]


def _replace_product_tags(
    db: Session, product: CanonicalProduct, tags: List[str], current_user: User,
) -> list[str]:
    desired = _normalize_tags(tags)
    before = _tag_names(db, product.id)
    db.query(ProductTag).filter(ProductTag.canonical_product_id == product.id).delete(
        synchronize_session=False,
    )
    for normalized_name, name in desired:
        db.add(ProductTag(
            id=uuid.uuid4(), canonical_product_id=product.id, name=name,
            normalized_name=normalized_name, created_by_id=current_user.id,
        ))
    after = [name for _, name in desired]
    record_audit(
        db=db, entity_type="CanonicalProduct", entity_id=product.id,
        display_label=product.product_name, action="tags_updated",
        before={"tags": before}, after={"tags": after},
        changed={"tags": [before, after]}, user_id=current_user.id,
        actor_type="user", reason="Updated product tags.",
    )
    return after


def _product_expected_format(db: Session, product: CanonicalProduct) -> str:
    values = {
        row.field_name: str(row.value or "")
        for row in db.query(FieldValue).filter(
            FieldValue.canonical_product_id == product.id,
            FieldValue.field_name.in_(["product_type", "subcategory"]),
            FieldValue.is_current == True,
        ).all()
    }
    from app.services.product_identity import product_version_label, trusted_product_version
    category = db.query(Category).filter(Category.id == product.category_id).first() if product.category_id else None
    trusted_version = trusted_product_version(db, product)
    controlled = {
        key: value for key, value in values.items()
        if not product_version_label(value) or product_version_label(value) == trusted_version
    }
    category_path = category.path if category else ""
    if product_version_label(category_path) and product_version_label(category_path) != trusted_version:
        category_path = category.name if category and not product_version_label(category.name) else "Fragrance"
    return " ".join(filter(None, (
        controlled.get("product_type"), controlled.get("subcategory"), category_path,
    )))

def product_internal_code(product_id: uuid.UUID) -> str:
    return f"ICN-{product_id.hex.upper()}"


def variant_sku(db: Session, variant: ProductVariant | None) -> Any:
    """Resolve one variant's SKU without borrowing a sibling's source row."""
    if not variant:
        return None
    direct = db.query(FieldValue).filter(
        FieldValue.product_variant_id == variant.id, FieldValue.field_name == "sku",
        FieldValue.is_current == True,
    ).order_by(FieldValue.created_at.desc()).first()
    if direct:
        return direct.value
    item = db.query(ImportJobItem).filter(
        ImportJobItem.product_variant_id == variant.id,
        ImportJobItem.source_listing_id.isnot(None),
    ).order_by(ImportJobItem.created_at.desc()).first()
    if not item:
        return None
    listing = db.query(SourceListing).filter(SourceListing.id == item.source_listing_id).first()
    job = db.query(ImportJob).filter(ImportJob.id == item.import_job_id).first()
    raw = (listing.raw_data or {}) if listing else {}
    mapped = (job.column_mapping or {}).get("sku") if job else None
    normalized = {re.sub(r"[^a-z0-9]", "", str(key).lower()): value for key, value in raw.items()}
    for candidate in (mapped, "SKU Number", "SKU", "Supplier SKU"):
        if candidate:
            value = normalized.get(re.sub(r"[^a-z0-9]", "", str(candidate).lower()))
            if value not in (None, ""):
                return value
    return None


def _refresh_product_understanding(db: Session, product: CanonicalProduct) -> dict:
    """Synchronously refresh the contract after a foundational human change."""
    from app.services.product_identity import preferred_product_variant
    from app.services.product_understanding import resolve_product_understanding
    listing = db.query(SourceListing).filter(
        SourceListing.canonical_product_id == product.id, SourceListing.is_deleted == False,
    ).order_by(SourceListing.created_at.desc()).first()
    job = db.query(ImportJob).filter(ImportJob.id == listing.import_job_id).first() if listing and listing.import_job_id else None
    contract = resolve_product_understanding(
        db, raw_data=(listing.raw_data or {}) if listing else {}, mapping=(job.column_mapping or {}) if job else {},
        product=product, variant=preferred_product_variant(db, product.id),
    )
    create_field_value_version(
        db, product.id, None, "product_understanding", contract, "deterministic_rule",
        "foundational-change", float(contract.get("confidence") or 0),
        "conflicting" if contract.get("conflicts") else "confirmed" if contract.get("identity_status") == "resolved" else "inferred",
        None, contract.get("evidence") or [], "Recalculated after foundational identity/taxonomy change.",
        contract.get("identity_status"), "product_understanding",
    )
    return contract


def _refresh_identity_review_gate(db: Session, product: CanonicalProduct) -> tuple[dict, dict]:
    from app.services.product_improvement import product_improvement_summary
    from app.services.identity_review import does_this_product_require_identity_review, synchronize_blocking_issue
    quality = product_improvement_summary(db, product)
    decision = does_this_product_require_identity_review(db, product, quality)
    synchronize_blocking_issue(db, product, decision)
    return quality, decision


def _clean_classification(value: str, label: str) -> str:
    cleaned = " ".join(value.split()).strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail=f"{label} is required.")
    return cleaned.title()


def _set_product_classification(
    db: Session,
    product: CanonicalProduct,
    category: str,
    subcategory: str,
    user: User,
    *,
    refresh_understanding: bool = True,
):
    category_name = _clean_classification(category, "Category")
    subcategory_name = _clean_classification(subcategory, "Subcategory")
    root = db.query(Category).filter(func.lower(Category.path) == category_name.lower()).first()
    if not root:
        root = Category(id=uuid.uuid4(), name=category_name, level=0, path=category_name)
        db.add(root)
        db.flush()
    child_path = f"{root.path} > {subcategory_name}"
    child = db.query(Category).filter(func.lower(Category.path) == child_path.lower()).first()
    if not child:
        child = Category(id=uuid.uuid4(), name=subcategory_name, parent_id=root.id, level=1, path=child_path)
        db.add(child)
        db.flush()
    before_category = str(product.category_id) if product.category_id else None
    product.category_id = child.id
    current_category = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product.id, FieldValue.field_name == "category",
        FieldValue.is_current == True,
    ).first()
    if current_category and current_category.value != category_name:
        current_category.is_current = False
    if not current_category or current_category.value != category_name:
        create_field_value_version(
            db, product.id, None, "category", category_name, "human_edit",
            f"user:{user.id}", 1.0, "confirmed", None, [],
            "Category manually assigned.", "confirmed", "taxonomy",
        )
    previous = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product.id,
        FieldValue.product_variant_id.is_(None),
        FieldValue.field_name == "subcategory",
        FieldValue.is_current == True,
    ).first()
    before_subcategory = previous.value if previous else None
    if previous and previous.value != subcategory_name:
        previous.is_current = False
    if not previous or previous.value != subcategory_name:
        create_field_value_version(
            db, product.id, None, "subcategory", subcategory_name, "human_edit",
            f"user:{user.id}", None, "confirmed", None, [],
            "Category and subcategory manually assigned.", "confirmed", "value_status",
        )
    record_audit(
        db, entity_type="canonical_product", entity_id=product.id,
        display_label=product.product_name, action="update",
        before={"category_id": before_category, "subcategory": before_subcategory},
        after={"category_id": str(child.id), "category": category_name, "subcategory": subcategory_name},
        changed={"category": [before_category, category_name], "subcategory": [before_subcategory, subcategory_name]},
        user_id=user.id, actor_type="user",
    )
    if refresh_understanding:
        _refresh_product_understanding(db, product)


@router.get("/metrics")
def product_metrics(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_viewer_or_above),
):
    total_products = db.query(func.count(CanonicalProduct.id)).filter(
        CanonicalProduct.is_deleted == False,
    ).scalar() or 0
    unresolved_issues = db.query(func.count(ValidationIssue.id)).filter(
        ValidationIssue.resolved == False,
    ).scalar() or 0
    return {
        "total_products": total_products,
        "unresolved_issues": unresolved_issues,
    }


@router.get("/identity-review-queue")
def identity_review_queue(
    page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db), _: User = Depends(require_viewer_or_above),
):
    """Efficient persisted queue; opening it never starts AI or web research."""
    issue_query = db.query(ValidationIssue).filter(
        ValidationIssue.issue_type == "foundational_identity_unresolved",
        ValidationIssue.resolved == False,
        ValidationIssue.canonical_product_id.isnot(None),
    )
    total = issue_query.count()
    issues = issue_query.order_by(ValidationIssue.created_at.asc()).offset((page - 1) * limit).limit(limit).all()
    product_ids = [row.canonical_product_id for row in issues]
    products = {row.id: row for row in db.query(CanonicalProduct).filter(
        CanonicalProduct.id.in_(product_ids), CanonicalProduct.is_deleted == False,
    ).all()} if product_ids else {}
    contracts = {
        row.canonical_product_id: dict(row.value or {})
        for row in db.query(FieldValue).filter(
            FieldValue.canonical_product_id.in_(product_ids),
            FieldValue.field_name == "product_understanding", FieldValue.is_current == True,
        ).all()
    } if product_ids else {}
    review_states = {
        row.canonical_product_id: dict(row.value or {})
        for row in db.query(FieldValue).filter(
            FieldValue.canonical_product_id.in_(product_ids),
            FieldValue.field_name == "identity_review_state", FieldValue.is_current == True,
        ).all()
        if isinstance(row.value, dict)
    } if product_ids else {}
    variants = {
        row.canonical_product_id: row for row in db.query(ProductVariant).filter(
            ProductVariant.canonical_product_id.in_(product_ids), ProductVariant.is_deleted == False,
        ).order_by(ProductVariant.created_at.asc()).all()
    } if product_ids else {}
    latest_jobs: dict[str, CrawlJob] = {}
    if product_ids:
        wanted = {str(value) for value in product_ids}
        for job in db.query(CrawlJob).filter(
            CrawlJob.domain == "product-research.internal",
        ).order_by(CrawlJob.created_at.desc()).limit(max(500, len(product_ids) * 10)).all():
            research_product_id = str((job.configuration or {}).get("research_product_id") or "")
            if research_product_id in wanted and research_product_id not in latest_jobs:
                latest_jobs[research_product_id] = job
    rows = []
    for issue in issues:
        product = products.get(issue.canonical_product_id)
        if not product:
            continue
        contract = contracts.get(product.id, {})
        identity = contract.get("identity") or {}
        taxonomy = contract.get("taxonomy") or {}
        variant = variants.get(product.id)
        blocked_job = latest_jobs.get(str(product.id))
        rows.append({
            "product_id": str(product.id), "product_name": product.product_name,
            "source_product_name": (contract.get("source_interpretation") or {}).get("source_product_family") or product.product_name,
            "brand": product.brand.name if product.brand else None,
            "gtin": variant.gtin if variant else None,
            "reason": issue.message.removeprefix("Identity confirmation required: ").strip(),
            "review_status": review_states.get(product.id, {}).get("status") or "NEEDS_REVIEW",
            "identity_status": contract.get("identity_status"),
            "match_type": contract.get("match_type"), "confidence": contract.get("confidence"),
            "understanding_fingerprint": contract.get("foundational_fingerprint"),
            "blocked_research_job_id": str(blocked_job.id) if blocked_job else None,
            "resume_context": {
                "mode": (blocked_job.configuration or {}).get("requested_mode", "missing_only") if blocked_job else "missing_only",
                "fields": (blocked_job.configuration or {}).get("selected_fields", []) if blocked_job else [],
                "blocked_research_job_id": str(blocked_job.id) if blocked_job else None,
            },
            "suggested_identity": {
                "brand": (identity.get("consumer_brand") or {}).get("value"),
                "product_family": (identity.get("product_family") or {}).get("value"),
                "variant": (identity.get("variant") or {}).get("value"),
                "category": (taxonomy.get("category") or {}).get("value"),
                "subcategory": (taxonomy.get("subcategory") or {}).get("value"),
                "product_type": (taxonomy.get("product_type") or {}).get("value"),
                "category_module": contract.get("category_module"),
            },
        })
    return {"total": total, "page": page, "limit": limit, "items": rows}

@router.get("", response_model=List[ProductOut])
def list_products(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    search: Optional[str] = None,
    status_filter: Optional[str] = None,
    brand_filter: Optional[str] = None,
    issue_filter: Optional[bool] = None,
    import_job_id: Optional[uuid.UUID] = None,
    response: Response = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_viewer_or_above)
):
    # The grid is a variant ledger: canonical identity is shared, but each
    # persisted ProductVariant/EAN is one independently selectable row.
    query = db.query(ProductVariant, CanonicalProduct).select_from(CanonicalProduct).outerjoin(
        ProductVariant, ProductVariant.canonical_product_id == CanonicalProduct.id,
    ).join(Brand, CanonicalProduct.brand_id == Brand.id).filter(
        CanonicalProduct.is_deleted == False,
        or_(ProductVariant.id.is_(None), ProductVariant.is_deleted == False),
    )

    if import_job_id:
        # Import membership is provenance-based, never inferred from product
        # creation time or filename. A pre-existing variant touched by a later
        # import therefore remains visible under every import it participated in.
        item_variant_ids = db.query(ImportJobItem.product_variant_id).filter(
            ImportJobItem.import_job_id == import_job_id,
            ImportJobItem.product_variant_id.isnot(None),
        )
        listing_variant_ids = db.query(SourceListing.product_variant_id).filter(
            SourceListing.import_job_id == import_job_id,
            SourceListing.product_variant_id.isnot(None),
            SourceListing.is_deleted == False,
        )
        query = query.filter(or_(
            ProductVariant.id.in_(item_variant_ids),
            ProductVariant.id.in_(listing_variant_ids),
        ))

    if search and search.strip():
        raw_search = search.strip()
        search_term = f"%{raw_search.lower()}%"
        search_conditions = [
            func.lower(CanonicalProduct.product_name).like(search_term),
            func.lower(Brand.name).like(search_term),
            func.lower(ProductVariant.gtin).like(search_term),
            func.lower(ProductVariant.variant_name).like(search_term),
        ]
        normalized_icn = raw_search.upper().removeprefix("ICN-").replace("-", "")
        if len(normalized_icn) == 32:
            try:
                search_conditions.append(CanonicalProduct.id == uuid.UUID(hex=normalized_icn))
            except ValueError:
                pass
        query = query.filter(or_(*search_conditions))

    if status_filter == "needs_identity_review":
        query = query.filter(CanonicalProduct.id.in_(db.query(ValidationIssue.canonical_product_id).filter(
            ValidationIssue.issue_type == "foundational_identity_unresolved",
            ValidationIssue.resolved == False,
        )))
    elif status_filter:
        query = query.filter(CanonicalProduct.review_status == status_filter)

    if brand_filter:
        query = query.filter(Brand.name == brand_filter)

    if issue_filter is not None:
        canonical_issue_ids = db.query(ValidationIssue.canonical_product_id).filter(
            ValidationIssue.resolved == False,
            ValidationIssue.canonical_product_id.isnot(None),
        )
        variant_issue_ids = db.query(ValidationIssue.product_variant_id).filter(
            ValidationIssue.resolved == False,
            ValidationIssue.product_variant_id.isnot(None),
        )
        if issue_filter:
            query = query.filter(or_(
                CanonicalProduct.id.in_(canonical_issue_ids),
                ProductVariant.id.in_(variant_issue_ids),
            ))
        else:
            query = query.filter(
                ~CanonicalProduct.id.in_(canonical_issue_ids),
                ~ProductVariant.id.in_(variant_issue_ids),
            )

    total = query.count()
    if response is not None:
        response.headers["X-Total-Count"] = str(total)
        response.headers["X-Page"] = str(page)
        response.headers["X-Page-Limit"] = str(limit)
    offset = (page - 1) * limit
    rows = query.order_by(CanonicalProduct.created_at.desc(), ProductVariant.created_at.asc()).offset(offset).limit(limit).all()
    product_ids = list(dict.fromkeys(product.id for _, product in rows))
    variant_counts = dict(
        db.query(ProductVariant.canonical_product_id, func.count(ProductVariant.id))
        .filter(
            ProductVariant.canonical_product_id.in_(product_ids),
            ProductVariant.is_deleted == False,
        )
        .group_by(ProductVariant.canonical_product_id)
        .all()
    ) if product_ids else {}
    tags_by_product: dict[uuid.UUID, list[str]] = {product_id: [] for product_id in product_ids}
    identity_review_states = {
        row.canonical_product_id: dict(row.value or {}).get("status")
        for row in db.query(FieldValue).filter(
            FieldValue.canonical_product_id.in_(product_ids),
            FieldValue.field_name == "identity_review_state", FieldValue.is_current == True,
        ).all()
        if isinstance(row.value, dict)
    } if product_ids else {}
    if product_ids:
        for product_id, tag_name in db.query(
            ProductTag.canonical_product_id, ProductTag.name,
        ).filter(ProductTag.canonical_product_id.in_(product_ids)).order_by(
            ProductTag.normalized_name,
        ).all():
            tags_by_product.setdefault(product_id, []).append(tag_name)

    # Format output items with Brand and Category titles
    out = []
    for variant, prod in rows:
        category_path = None
        if prod.category_id:
            cat = db.query(Category).filter(Category.id == prod.category_id).first()
            category_path = cat.path if cat else None
        current_classifications = {
            field.field_name: field.value
            for field in db.query(FieldValue).filter(
                FieldValue.canonical_product_id == prod.id,
                FieldValue.product_variant_id.is_(None),
                FieldValue.field_name.in_(["subcategory", "product_type"]),
                FieldValue.is_current == True,
            ).all()
        }
        category_parts = [part.strip() for part in (category_path or "").split(">") if part.strip()]
            
        issues = (
            db.query(ValidationIssue)
            .outerjoin(ProductVariant, ValidationIssue.product_variant_id == ProductVariant.id)
            .filter(
                ValidationIssue.resolved == False,
                or_(
                    ValidationIssue.canonical_product_id == prod.id,
                    ValidationIssue.product_variant_id == (variant.id if variant else None),
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
        sku_value = variant_sku(db, variant)
        out.append(ProductOut(
            id=prod.id,
            product_id=prod.id,
            product_variant_id=variant.id if variant else None,
            internal_code=product_internal_code(prod.id),
            product_name=prod.product_name,
            brand_name=prod.brand.name,
            category_path=category_path,
            product_category=category_parts[0] if category_parts else None,
            subcategory=current_classifications.get("subcategory") or (
                category_parts[-1] if len(category_parts) > 1 else None
            ),
            product_type=current_classifications.get("product_type"),
            gtin=variant.gtin if variant else None,
            sku=str(sku_value) if sku_value is not None else None,
            variant_name=variant.variant_name if variant else None,
            size=variant.size if variant else None,
            unit=variant.unit if variant else None,
            variant_count=variant_counts.get(prod.id, 0),
            image_url=prod.image_url,
            review_status=prod.review_status,
            validation_issue_count=len(issues),
            highest_issue_severity=highest_severity,
            tags=tags_by_product.get(prod.id, []),
            identity_review_status=((identity_review_states.get(prod.id) or "NEEDS_REVIEW") if any(
                issue.issue_type == "foundational_identity_unresolved" for issue in issues
            ) else None),
            is_deleted=prod.is_deleted,
            created_at=prod.created_at,
            updated_at=prod.updated_at
        ))
    return out

@router.post("/{product_id}/re-enrich", response_model=ProductDetailOut)
def re_enrich_product(
    product_id: uuid.UUID,
    product_variant_id: Optional[uuid.UUID] = Query(None),
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
        *([ImportJobItem.product_variant_id == product_variant_id] if product_variant_id else []),
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
        # Re-enrichment must use the same evidence-gap orchestration as Improve
        # Product.  Previously it stopped after the catalogue/LLM pass, so an
        # exact, trusted EDT could remain without INCI forever even though the
        # missing formulation was researchable.
        from app.services.product_improvement import product_improvement_summary
        quality = product_improvement_summary(db, product)
        objectives = [entry["field"] for entry in quality.get("research_objectives") or []]
        research_job = None
        if objectives and (settings.OPENAI_API_KEY or settings.BRAVE_SEARCH_API_KEY):
            research_job = _enqueue_product_research(
                db, product, item, ProductImproveRequest(mode="missing_only"), current_user, objectives,
            )
        record_audit(
            db=db,
            entity_type="CanonicalProduct",
            entity_id=product.id,
            display_label=product.product_name,
            action="update",
            before={"enrichment_status": "existing"},
            after={
                "enrichment_status": "completed",
                "research_job_id": str(research_job.id) if research_job else None,
                "research_objectives": objectives,
            },
            changed={"enrichment": ["existing", "regenerated"], "research_objectives": objectives},
            user_id=current_user.id,
            actor_type="user",
            reason="Manual re-enrichment from the latest source record.",
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Re-enrichment failed: {exc}")

    detail = get_product_detail(product_id, db, current_user)
    if research_job:
        detail.improvement_result = {
            **_research_job_payload(research_job),
            "message": "Re-enrichment completed; exact-product evidence research is continuing in the background.",
            "still_unavailable": objectives,
        }
    return detail


@router.get("/{product_id}/improvement")
def product_improvement(
    product_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_viewer_or_above),
):
    product = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == product_id, CanonicalProduct.is_deleted == False,
    ).first()
    if not product:
        raise HTTPException(404, "Product not found")
    from app.services.product_improvement import product_improvement_summary
    return product_improvement_summary(db, product)


def _automatic_product_research(
    db: Session, product: CanonicalProduct, user: User,
    candidates: list[dict] | None = None,
    research_objectives: list[str] | None = None,
    research_variant_id: uuid.UUID | None = None,
    identity_only: bool = False,
    research_log_context: dict | None = None,
) -> dict:
    """Discover and ingest a small exact-product evidence set before enrichment.

    This is deliberately bounded: up to three exact product-page candidates
    across distinct domains and no category traversal. Failure to access one source does not
    prevent the normal enrichment fallback from running.
    """
    from app.services.web_discovery import discover_product_sources
    from app.services.image_urls import normalize_public_image_url
    from app.scraping.runner import run_crawl_job
    from app.services.product_research_logging import product_research_log

    log_context = dict(research_log_context or {})

    from app.services.product_identity import preferred_product_variant
    variant = db.query(ProductVariant).filter(
        ProductVariant.id == research_variant_id,
        ProductVariant.canonical_product_id == product.id,
        ProductVariant.is_deleted == False,
    ).first() if research_variant_id else None
    variant = variant or preferred_product_variant(db, product.id)
    expected_gtin = re.sub(r"\D", "", variant.gtin or "") if variant else ""
    expected_format = _product_expected_format(db, product)
    from app.services.product_identity import product_is_fragrance, trusted_product_version
    safe_market_only = bool(product_is_fragrance(db, product) and not trusted_product_version(db, product))
    if candidates is None:
        candidates = discover_product_sources(
            brand=product.brand.name if product.brand else "",
            product_name=product.product_name,
            product_format=expected_format,
            gtin=variant.gtin if variant and variant.gtin else "",
            approved_domains=[],
        )

    brand_token = re.sub(r"[^a-z0-9]", "", (product.brand.name if product.brand else "").lower())
    product_tokens = [
        token for token in re.findall(r"[a-z0-9]+", product.product_name.lower())
        if len(token) > 3
    ]

    def candidate_score(candidate):
        domain = str(candidate.get("domain") or "").replace("-", "").replace(".", "")
        title = str(candidate.get("title") or "").lower()
        url = str(candidate.get("url") or "").lower()
        score = 5 if brand_token and brand_token in domain else 0
        score += sum(1 for token in product_tokens if token in title or token in url)
        score += 2 if candidate.get("image_url") else 0
        score -= 5 if any(part in url for part in ("/search", "/blog", "/article", "?q=")) else 0
        score -= 3 if any(part in url for part in ("/c/", "/category", "/collection")) else 0
        return score

    from app.services.product_identity import research_identity_compatible
    understanding_row = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product.id,
        FieldValue.field_name == "product_understanding",
        FieldValue.is_current == True,
    ).order_by(FieldValue.created_at.desc()).first()
    understanding = understanding_row.value if understanding_row and isinstance(understanding_row.value, dict) else {}
    level_b_allowed = understanding.get("identity_status") == "resolved"

    def candidate_has_exact_identity(candidate: dict) -> bool:
        text = " ".join(
            str(candidate.get(key) or "") for key in ("title", "url", "snippet")
        ).lower()
        compact = re.sub(r"[^a-z0-9]", "", text)
        if expected_gtin and expected_gtin in re.sub(r"\D", "", text):
            return True
        if not expected_gtin:
            return True
        # Level B authorizes discovery/crawling of an exact resolved product
        # page when the retailer does not expose GTIN in search results.  The
        # downstream evidence-scope gate still prevents this from authorizing
        # formulation, claims, concentration or other unsafe exact facts.
        name_tokens = [
            token for token in re.findall(r"[a-z0-9]+", product.product_name.lower())
            if len(token) > 3
        ]
        return bool(
            level_b_allowed
            and brand_token and brand_token in compact
            and name_tokens and sum(token in text for token in name_tokens) >= min(2, len(name_tokens))
        )

    candidate_rejections = []
    compatible_candidates = []
    for candidate in candidates:
        identity_compatible = research_identity_compatible(
            expected_format,
            " ".join(str(candidate.get(key) or "") for key in ("title", "url", "snippet")),
            product.product_name,
        )
        exact_candidate = candidate_has_exact_identity(candidate)
        if identity_compatible and exact_candidate:
            compatible_candidates.append(candidate)
            product_research_log(
                "candidate_accepted", **log_context, url=candidate.get("url"),
                domain=candidate.get("domain") or (urlparse(str(candidate.get("url") or "")).hostname or ""),
                reason="identity_and_format_compatible",
            )
        else:
            rejection = {
                "url": candidate.get("url"),
                "reason": "format_conflict" if not identity_compatible else "insufficient_exact_identity",
            }
            candidate_rejections.append(rejection)
            product_research_log(
                "candidate_rejected", **log_context, url=rejection["url"],
                domain=candidate.get("domain") or (urlparse(str(rejection["url"] or "")).hostname or ""),
                rejection_reason=rejection["reason"],
            )
    candidates = compatible_candidates
    candidates = sorted(candidates, key=candidate_score, reverse=True)
    image_before_run = bool(product.image_url)
    # An image result is tied to its product-page source. Prefer the strongest
    # identity match, not merely the provider's first arbitrary result.
    if not product.image_url:
        for candidate in candidates:
            image = normalize_public_image_url(candidate.get("image_url"))
            if image:
                product.image_url = image
                break
    selected = []
    seen_domains = set()
    objective_set = set(research_objectives or [])
    review_objective = bool({"reviews", "review_summary", "rating", "review_count"} & objective_set)
    source_limit = (
        int(settings.WEB_RESEARCH_REVIEW_DOMAIN_LIMIT)
        if review_objective else int(settings.WEB_RESEARCH_GENERAL_DOMAIN_LIMIT)
    )
    for candidate in candidates:
        url = str(candidate.get("url") or "").strip()
        domain = (urlparse(url).hostname or "").lower()
        if not url or not domain or domain in seen_domains:
            continue
        seen_domains.add(domain)
        selected.append((url, domain))
        if len(selected) == source_limit:
            break

    completed = 0
    errors = []
    review_texts_extracted = 0
    review_sample_rejections: dict[str, int] = {}

    def has_review_evidence() -> bool:
        observations = db.query(ScrapedProductObservation.normalized_payload).filter(
            ScrapedProductObservation.canonical_product_id == product.id,
        ).order_by(ScrapedProductObservation.scraped_at.desc()).limit(50).all()
        for (payload,) in observations:
            payload = payload or {}
            summary = payload.get("review_summary") or {}
            if payload.get("rating") is not None or payload.get("review_count") is not None:
                return True
            if summary.get("average_rating") is not None or summary.get("review_count") is not None:
                return True
        return False

    def review_evidence_signature() -> tuple:
        observation_ids = []
        rows = db.query(ScrapedProductObservation).filter(
            ScrapedProductObservation.canonical_product_id == product.id,
        ).order_by(ScrapedProductObservation.scraped_at.desc()).limit(100).all()
        for row in rows:
            payload = row.normalized_payload or {}
            summary = payload.get("review_summary") or {}
            if (payload.get("rating") is not None or payload.get("review_count") is not None
                    or summary.get("average_rating") is not None or summary.get("review_count") is not None):
                observation_ids.append(str(row.id))
        field_ids = [str(row.id) for row in db.query(FieldValue).filter(
            FieldValue.canonical_product_id == product.id,
            FieldValue.field_name.in_(["rating", "review_count"]), FieldValue.is_current == True,
        ).all()]
        return tuple(sorted(observation_ids + field_ids))

    def has_official_evidence() -> bool:
        if not brand_token:
            return False
        rows = db.query(ScrapedProductObservation.source_domain).filter(
            ScrapedProductObservation.canonical_product_id == product.id,
        ).all()
        return any(brand_token in str(domain or "").replace("-", "").replace(".", "") for (domain,) in rows)

    def has_formulation_evidence() -> bool:
        return db.query(Formulation).filter(
            Formulation.canonical_product_id == product.id,
            Formulation.is_deleted == False,
            func.length(func.trim(Formulation.raw_inci_text)) > 0,
        ).first() is not None

    def has_variant_identity() -> bool:
        row = preferred_product_variant(db, product.id)
        return bool(row and (row.gtin or row.size or row.variant_name))

    review_signature_before = review_evidence_signature()
    from app.services.review_aggregate import select_review_aggregate
    for url, domain in selected:
        product_research_log("crawl_attempt", **log_context, url=url, domain=domain)
        configuration = {
            "domain": domain, "starting_urls": [url], "crawl_mode": "single_url",
            "maximum_crawl_depth": 1 if review_objective else 0,
            "maximum_pages": int(settings.WEB_RESEARCH_REVIEW_PAGES_PER_SOURCE) if review_objective else 1,
            "maximum_product_pages": int(settings.WEB_RESEARCH_REVIEW_PAGES_PER_SOURCE) if review_objective else 1,
            "maximum_runtime_seconds": 90 if review_objective else 45,
            "maximum_discovered_urls": 20 if review_objective else 1,
            "use_sitemap": False, "use_category_discovery": False,
            "use_browser_rendering": bool(
                {"reviews", "review_summary", "rating", "review_count"}
                & set(research_objectives or [])
            ), "respect_robots_txt": True,
            "allow_subdomains": False, "request_delay_seconds": 0.25,
            "per_domain_concurrency": 1, "retry_limit": 1,
            "request_timeout_seconds": 20, "maximum_response_bytes": 8000000,
            "maximum_redirects": 5, "browser_page_limit": 3 if review_objective else 1,
            "country": None, "locale": None, "rescrape_interval_hours": None,
            "recrawl_strategy": "crawl_once", "allowed_url_patterns": [],
            "denied_url_patterns": [], "include_editorial": False,
        }
        job = CrawlJob(
            id=uuid.uuid4(), domain=domain, starting_urls=[url],
            crawl_mode="single_url", status="queued",
            configuration={
                **configuration, "research_product_id": str(product.id),
                "research_variant_id": str(variant.id) if variant else None,
                "research_expected_format": expected_format,
                "research_product_name": product.product_name,
                # An unresolved EDT/EDP distinction must block formulation and
                # exact-claim attachment, not safe family-level image/reviews.
                "research_safe_fields_only": safe_market_only,
                "research_identity_only": identity_only,
            },
            requested_by_id=user.id,
        )
        db.add(job)
        db.commit()
        try:
            run_crawl_job(db, job.id)
            db.refresh(job)
            # Count only actual sanitized text records persisted by this crawl.
            # Declared widget/sample counts are intentionally ignored.
            run_observations = db.query(ScrapedProductObservation).filter(
                ScrapedProductObservation.crawl_job_id == job.id,
            ).all()
            run_extracted = 0
            run_accepted = 0
            run_rejected: dict[str, int] = {}
            run_strategies: set[str] = set()
            for run_observation in run_observations:
                run_payload = run_observation.normalized_payload or {}
                run_summary = run_payload.get("review_summary") or {}
                run_extracted += int(run_summary.get("raw_review_candidate_count") or 0)
                run_strategies.update(str(value) for value in (run_summary.get("review_extraction_strategies") or []))
                run_samples = run_payload.get("review_samples")
                if not isinstance(run_samples, list):
                    run_samples = run_summary.get("review_samples")
                if isinstance(run_samples, list):
                    accepted_samples = len([
                        sample for sample in run_samples
                        if isinstance(sample, dict) and str(sample.get("text") or "").strip()
                    ])
                    run_accepted += accepted_samples
                    review_texts_extracted += int(run_summary.get("raw_review_candidate_count") or accepted_samples)
                for rejection in run_summary.get("review_sample_rejections") or []:
                    if not isinstance(rejection, dict):
                        continue
                    reason = str(rejection.get("reason") or "unspecified")
                    count = int(rejection.get("count") or 1)
                    review_sample_rejections[reason] = review_sample_rejections.get(reason, 0) + count
                    run_rejected[reason] = run_rejected.get(reason, 0) + count
            if run_extracted and not run_accepted and not run_rejected:
                run_rejected["raw_candidates_not_persisted"] = run_extracted
                review_sample_rejections["raw_candidates_not_persisted"] = (
                    review_sample_rejections.get("raw_candidates_not_persisted", 0) + run_extracted
                )
            product_research_log(
                "review_extraction", **log_context, domain=domain,
                crawl_job_id=str(job.id), raw_candidates=run_extracted, extracted=run_extracted,
                accepted=run_accepted, rejected=sum(run_rejected.values()),
                rejection_reasons=run_rejected, persisted=run_accepted,
                extraction_strategies=sorted(run_strategies),
            )
            product_research_log(
                "crawl_result", **log_context, url=url, domain=domain,
                crawl_job_id=str(job.id), status=job.status,
                pages_fetched=int(job.pages_fetched or 0), products_persisted=int(job.products_persisted or 0),
                error=job.error_summary,
            )
            if job.products_persisted:
                completed += 1
                db.refresh(product)
                # Official pages commonly provide imagery but no customer-review
                # aggregate. Continue across distinct sources until both evidence
                # needs are met, while retaining every source independently.
                review_intelligence = select_review_aggregate(db, product.id) or {}
                review_goal_met = (
                    not review_objective
                    or int(review_intelligence.get("review_sample_count") or 0) >= int(settings.WEB_RESEARCH_REVIEW_SAMPLE_TARGET)
                )
                if (
                    product.image_url and review_goal_met
                    and (has_official_evidence() or has_formulation_evidence())
                    and has_variant_identity()
                ):
                    break
            elif job.error_summary:
                errors.append(f"{domain}: {job.error_summary}")
        except Exception as exc:
            db.rollback()
            errors.append(f"{domain}: {exc}")
            product_research_log(
                "crawl_failure", level=__import__("logging").ERROR, **log_context,
                url=url, domain=domain, error_type=type(exc).__name__, error=str(exc),
            )
    db.commit()
    review_signature_after = review_evidence_signature()
    canonical_review = select_review_aggregate(db, product.id) or {}
    product_research_log(
        "review_result", **log_context,
        average_rating=canonical_review.get("average_rating"),
        review_count=canonical_review.get("review_count"),
        review_source_count=canonical_review.get("source_count"),
        actual_review_text_count=int(canonical_review.get("review_sample_count") or 0),
        extracted=review_texts_extracted,
        rejected=sum(review_sample_rejections.values()),
        rejection_reasons=review_sample_rejections,
        persisted=int(canonical_review.get("review_sample_count") or 0),
        sources_with_review_text=len({
            str(sample.get("source_domain") or sample.get("source_url") or "")
            for sample in ((canonical_review.get("review_summary") or {}).get("review_samples") or [])
            if isinstance(sample, dict) and str(sample.get("text") or "").strip()
        } - {""}),
    )
    return {
        "candidates": len(candidates), "sources_ingested": completed,
        "accepted_candidates": [url for url, _ in selected],
        "rejected_candidates": candidate_rejections,
        "image_found": bool(product.image_url), "review_evidence_found": has_review_evidence(),
        "image_added_this_run": bool(product.image_url) and not image_before_run,
        "review_evidence_found_this_run": review_signature_after != review_signature_before,
        "official_evidence_found": has_official_evidence(),
        "formulation_evidence_found": has_formulation_evidence(),
        "variant_identity_found": has_variant_identity(),
        "queries_attempted": len((research_objectives or [])),
        "candidate_domains_found": len(seen_domains),
        "pages_attempted": len(selected),
        "sources_blocked": len(errors),
        "review_texts_extracted": review_texts_extracted,
        "review_samples_collected": int(canonical_review.get("review_sample_count") or 0),
        "review_texts_persisted": int(canonical_review.get("review_sample_count") or 0),
        "actual_review_text_count": int(canonical_review.get("review_sample_count") or 0),
        "sources_with_review_text": len({
            str(sample.get("source_domain") or sample.get("source_url") or "")
            for sample in ((canonical_review.get("review_summary") or {}).get("review_samples") or [])
            if isinstance(sample, dict) and str(sample.get("text") or "").strip()
        } - {""}),
        "review_sample_rejections": review_sample_rejections,
        "errors": errors,
    }


def _enqueue_product_research(
    db: Session, product: CanonicalProduct, item: ImportJobItem,
    request: ProductImproveRequest, user: User, research_objectives: list[str] | None = None,
    initial_discovery: dict | None = None,
    research_priority: int = 100,
) -> CrawlJob:
    from app.services.product_improvement import product_improvement_summary
    active = db.query(CrawlJob).filter(
        CrawlJob.domain == "product-research.internal",
        CrawlJob.status.in_(["queued", "discovering", "crawling", "parsing"]),
    ).order_by(CrawlJob.created_at.desc()).all()
    for job in active:
        config = job.configuration or {}
        if (str(config.get("research_product_id")) == str(product.id)
                and str(config.get("research_variant_id") or "") == str(item.product_variant_id or "")):
            return job
    from app.services.product_research_worker import _research_snapshot
    job = CrawlJob(
        id=uuid.uuid4(), domain="product-research.internal", starting_urls=[],
        crawl_mode="single_url", status="queued", requested_by_id=user.id,
        configuration={
            "product_research_job": True,
            "research_product_id": str(product.id),
            "research_item_id": str(item.id),
            "research_variant_id": str(item.product_variant_id) if item.product_variant_id else None,
            "requested_mode": request.mode,
            "selected_fields": request.fields,
            "research_objectives": research_objectives or [],
            "research_priority": research_priority,
            "research_phase": product_improvement_summary(db, product).get("research_phase"),
            "before_metrics": _research_snapshot(db, product),
            "discovery": initial_discovery,
            "result": None,
        },
    )
    db.add(job)
    db.flush()
    return job


def _research_job_payload(job: CrawlJob) -> dict:
    configuration = job.configuration or {}
    result = configuration.get("result") or {}
    return {
        "research_job_id": str(job.id),
        "research_status": job.status,
        "research_pending": job.status in {"queued", "discovering", "crawling", "parsing"},
        "business_outcome": result.get("business_outcome"),
        "business_status": result.get("business_status"),
        "result": result or None,
        "error": job.error_summary,
    }


@router.get("/{product_id}/research-status")
def product_research_status(
    product_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer_or_above),
):
    jobs = db.query(CrawlJob).filter(
        CrawlJob.domain == "product-research.internal",
    ).order_by(CrawlJob.created_at.desc()).limit(100).all()
    for job in jobs:
        if str((job.configuration or {}).get("research_product_id")) == str(product_id):
            return _research_job_payload(job)
    return {"research_job_id": None, "research_status": "not_started", "research_pending": False,
            "result": None, "error": None}


@router.post("/{product_id}/improve", response_model=ProductDetailOut)
def improve_product(
    product_id: uuid.UUID,
    request: ProductImproveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin),
):
    if request.mode not in {"missing_only", "selected", "full"}:
        raise HTTPException(422, "Mode must be missing_only, selected or full")
    if request.mode == "selected" and not request.fields:
        raise HTTPException(422, "Select at least one field")
    product = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == product_id, CanonicalProduct.is_deleted == False,
    ).first()
    if not product:
        raise HTTPException(404, "Product not found")
    item = db.query(ImportJobItem).filter(
        ImportJobItem.canonical_product_id == product_id,
        ImportJobItem.source_listing_id.isnot(None),
    ).order_by(ImportJobItem.created_at.desc()).first()
    if not item:
        raise HTTPException(409, "No source record is available for enrichment")
    job = db.query(ImportJob).filter(ImportJob.id == item.import_job_id).first()
    try:
        # Improve Product is a complete workflow: research first, then let the
        # enrichment model consume exact-source observations. Search/crawl
        # failures remain non-fatal so imported data can still be improved.
        from app.services.product_improvement import product_improvement_summary
        before_quality = product_improvement_summary(db, product)
        from app.services.identity_review import synchronize_blocking_issue
        synchronize_blocking_issue(db, product, before_quality.get("identity_review") or {})
        research_summary = None
        from app.knowledge_corpus.retrieval import evidence_is_sufficient, retrieve_corpus_evidence
        from app.services.product_identity import preferred_product_variant
        variant = preferred_product_variant(db, product.id)
        category = db.query(Category).filter(Category.id == product.category_id).first() if product.category_id else None
        corpus_result = retrieve_corpus_evidence(
            db, gtin=variant.gtin if variant else "", brand=product.brand.name if product.brand else "",
            product_name=product.product_name, category=category.path if category else "",
        )
        requested = set(request.fields or []) if request.mode == "selected" else None
        research_objectives = [
            item["field"] for item in before_quality.get("research_objectives") or []
        ]
        meaningful_gaps = research_objectives
        if not meaningful_gaps:
            research_summary = {
                "web_search_skipped": True, "reason": "No meaningful evidence gaps found.",
                "sources_ingested": 0, "errors": [],
            }
        elif evidence_is_sufficient(corpus_result, requested) and not before_quality.get("market_observation_gaps"):
            research_summary = {
                "web_search_skipped": True, "reason": "Exact internal retail evidence already covers the requested product fields.",
                "corpus_match_level": corpus_result.get("match_level"), "sources_ingested": 0, "errors": [],
            }
        elif settings.OPENAI_API_KEY or settings.BRAVE_SEARCH_API_KEY:
            research_job = _enqueue_product_research(
                db, product, item, request, current_user, research_objectives,
            )
            research_summary = {
                **_research_job_payload(research_job),
                "sources_ingested": 0, "errors": [],
                "message": "Catalogue enrichment completed. Image and review research is continuing in the background.",
            }
        if not before_quality.get("identity_review_required"):
            process_item_enrichment(
                db, item, job.column_mapping or {}, mode=request.mode,
                selected_fields=request.fields,
            )
            try:
                from app.services.review_summarization import summarize_product_reviews
                summarize_product_reviews(db, product.id)
            except Exception:
                # Review synthesis is additive and must never turn otherwise useful
                # product enrichment into a failed customer action.
                pass
        elif research_summary is None:
            review_dimension = ((before_quality.get("identity_review") or {}).get("review_dimension") or "identity")
            research_summary = {
                "identity_required": review_dimension == "identity", "taxonomy_required": review_dimension == "taxonomy",
                "research_pending": False,
                "business_outcome": "needs_taxonomy_resolution" if review_dimension == "taxonomy" else "needs_identity_resolution",
                "message": ("Taxonomy confirmation is required before category-specific enrichment can continue."
                            if review_dimension == "taxonomy" else
                            "Identity confirmation is required before product-specific enrichment can continue."),
            }
        record_audit(
            db, "CanonicalProduct", product.id, product.product_name, "update",
            {"enrichment": "existing"},
            {"enrichment": request.mode, "fields": request.fields, "research": research_summary},
            {"enrichment": request.mode, "research": research_summary}, current_user.id, "user",
            "Guided Improve Product workflow",
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(500, f"Product improvement failed: {exc}") from exc
    detail = get_product_detail(product_id, db, current_user)
    after_quality = detail.completeness or {}
    detail.improvement_result = {
        **(research_summary or {}),
        "before_completeness": before_quality.get("overall_completeness"),
        "after_completeness": after_quality.get("overall_completeness"),
        "added_fields": sorted(set(before_quality.get("missing_high_priority_fields") or []) -
                               set(after_quality.get("missing_high_priority_fields") or [])),
        "still_unavailable": after_quality.get("missing_high_priority_fields") or [],
        "conflicting": [name for name, state in (after_quality.get("field_states") or {}).items()
                        if state.get("state") == "conflicting"],
        "identity_required": bool(after_quality.get("identity_review_required")),
        "identity_review": after_quality.get("identity_review"),
    }
    return detail


def _apply_product_identity(
    product_id: uuid.UUID,
    request: ProductIdentityUpdateRequest,
    db: Session,
    current_user: User,
):
    product = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == product_id, CanonicalProduct.is_deleted == False,
    ).first()
    if not product:
        raise HTTPException(404, "Product not found")
    if request.category_module and request.category_module.strip().lower() not in {
        "skincare", "haircare", "makeup", "fragrance", "unknown",
    }:
        raise HTTPException(422, "Category module must be skincare, haircare, makeup, fragrance or unknown")
    if request.brand and request.brand.strip():
        brand_name = request.brand.strip()
        normalized_brand = normalize_text(brand_name)
        brand = db.query(Brand).filter(Brand.normalized_name == normalized_brand).first()
        if not brand:
            brand = Brand(id=uuid.uuid4(), name=brand_name, normalized_name=normalized_brand)
            db.add(brand); db.flush()
        product.brand_id = brand.id
        create_field_value_version(db, product.id, None, "brand", brand_name, "human_edit",
            f"user:{current_user.id}", 1.0, "confirmed", None, [],
            "Consumer brand confirmed by a human editor.", "confirmed", "identity")
    if request.product_name and request.product_name.strip():
        product.product_name = request.product_name.strip()
        product.normalized_name = normalize_text(product.product_name)
        create_field_value_version(db, product.id, None, "product_name", product.product_name, "human_edit",
            f"user:{current_user.id}", 1.0, "confirmed", None, [],
            "Product name confirmed by a human editor.", "confirmed", "identity")
    if request.product_family and request.product_family.strip():
        create_field_value_version(db, product.id, None, "product_family", request.product_family.strip(), "human_edit",
            f"user:{current_user.id}", 1.0, "confirmed", None, [],
            "Product family confirmed by a human editor.", "confirmed", "identity")
    from app.services.product_identity import preferred_product_variant
    variant = preferred_product_variant(db, product_id)
    if not variant:
        variant = ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id)
        db.add(variant)
        # FieldValue has a direct FK to ProductVariant. Persist the newly created
        # variant before adding human-confirmed GTIN/variant evidence so
        # PostgreSQL cannot order the evidence INSERT ahead of its parent row.
        db.flush()
    if request.gtin:
        digits = re.sub(r"\D", "", request.gtin)
        if len(digits) not in {8, 12, 13, 14}:
            raise HTTPException(422, "GTIN must contain 8, 12, 13 or 14 digits")
        duplicate = db.query(ProductVariant).filter(
            ProductVariant.gtin == digits, ProductVariant.id != variant.id,
        ).first()
        if duplicate:
            raise HTTPException(409, "This GTIN already belongs to another product")
        variant.gtin = digits
        create_field_value_version(db, None, variant.id, "gtin", digits, "human_edit",
            f"user:{current_user.id}", 1.0, "confirmed", None, [],
            "GTIN supplied by a human editor.", "confirmed", "identity")
    if request.variant is not None:
        variant.variant_name = request.variant.strip() or None
        if variant.variant_name:
            create_field_value_version(db, None, variant.id, "variant", variant.variant_name, "human_edit",
                f"user:{current_user.id}", 1.0, "confirmed", None, [],
                "Variant supplied by a human editor.", "confirmed", "identity")
    if request.size is not None:
        variant.size = request.size.strip() or None
    if request.unit is not None:
        variant.unit = request.unit.strip() or None
    for field_name, value in (("product_type", request.format),):
        if value and value.strip():
            create_field_value_version(
                db, product.id, None, field_name, value.strip(), "human_edit",
                f"user:{current_user.id}", 1.0, "confirmed", None, [],
                "Identity supplied through Improve Product.", "confirmed", "identity",
            )
    if request.market and request.market.strip():
        db.query(SourcePrice).filter(SourcePrice.product_variant_id == variant.id).update(
            {"country": request.market.strip()}, synchronize_session=False
        )
    if request.category and request.category.strip():
        # Reuse the canonical taxonomy writer; a missing subcategory is
        # represented explicitly instead of inventing one.
        _set_product_classification(
            db, product, request.category,
            request.subcategory or request.product_type or "Unspecified", current_user,
            # The identity writer refreshes the contract once after every
            # foundational field has been applied. Refreshing here as well
            # queues two current product_understanding rows in one flush and
            # violates PostgreSQL's uq_current_val_product partial index.
            refresh_understanding=False,
        )
    for field_name, value in (
        ("product_type", request.product_type),
        ("application_area", request.application_area),
        ("category_module", request.category_module),
    ):
        if value and value.strip():
            create_field_value_version(
                db, product.id, None, field_name, value.strip(), "human_edit",
                f"user:{current_user.id}", 1.0, "confirmed", None, [],
                "Identity confirmed during enrichment review.", "confirmed", "identity",
            )
    contract = _refresh_product_understanding(db, product)
    return contract


@router.put("/{product_id}/identity")
def update_product_identity(
    product_id: uuid.UUID,
    request: ProductIdentityUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin),
):
    contract = _apply_product_identity(product_id, request, db, current_user)
    db.commit()
    return {"updated": True, "product_id": str(product_id), "product_understanding": contract}


@router.post("/{product_id}/identity-review/confirm")
def confirm_identity_review(
    product_id: uuid.UUID,
    request: IdentityReviewConfirmRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin),
):
    """Persist protected human identity and optionally resume blocked work."""
    if request.action not in {"confirm_and_continue", "save_only"}:
        raise HTTPException(422, "Action must be confirm_and_continue or save_only")
    product = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == product_id, CanonicalProduct.is_deleted == False,
    ).first()
    if not product:
        raise HTTPException(404, "Product not found")
    from app.services.identity_review import (
        current_understanding, does_this_product_require_identity_review,
        persist_review_state, synchronize_blocking_issue,
    )
    from app.services.product_improvement import product_improvement_summary
    current = current_understanding(db, product.id)
    current_fingerprint = current.get("foundational_fingerprint")
    if (request.understanding_fingerprint and current_fingerprint
            and request.understanding_fingerprint != current_fingerprint):
        quality = product_improvement_summary(db, product)
        decision = does_this_product_require_identity_review(db, product, quality)
        raise HTTPException(status_code=409, detail={
            "code": "stale_identity_review", "message": "Product Understanding changed; review refreshed.",
            "identity_review": decision, "improvement": quality,
        })

    before = {
        "product_understanding": current,
        "identity": request.identity.model_dump(exclude_none=True),
    }
    # Reuse the existing protected human-edit path. It validates GTIN,
    # maintains variants, writes FieldValue versions and refreshes Product Understanding.
    _apply_product_identity(product_id, request.identity, db, current_user)
    db.refresh(product)
    quality, decision = _refresh_identity_review_gate(db, product)
    status_value = "REVIEWED" if not decision["requires_review"] else "NEEDS_REVIEW"
    persist_review_state(
        db, product, status=status_value,
        fingerprint=decision.get("understanding_fingerprint"), actor_id=current_user.id,
        reason="Identity confirmed during enrichment review.",
        resume_context=request.resume_context,
    )
    resumed = False
    research_job = None
    if request.action == "confirm_and_continue" and not decision["requires_review"]:
        source_item = db.query(ImportJobItem).filter(
            ImportJobItem.canonical_product_id == product.id,
            ImportJobItem.source_listing_id.isnot(None),
        ).order_by(ImportJobItem.created_at.desc()).first()
        if source_item:
            mode = str(request.resume_context.get("mode") or "missing_only")
            fields = list(request.resume_context.get("fields") or [])
            if mode not in {"missing_only", "selected", "full"}:
                mode = "missing_only"
            applicable = {entry["field"] for entry in quality.get("research_objectives") or []}
            if mode == "selected":
                fields = [field for field in fields if field in applicable]
                if not fields:
                    mode = "missing_only"
            objectives = [entry["field"] for entry in quality.get("research_objectives") or []]
            if objectives:
                blocked_job_id = request.resume_context.get("blocked_research_job_id")
                blocked_job = None
                try:
                    blocked_job = db.query(CrawlJob).filter(CrawlJob.id == uuid.UUID(str(blocked_job_id))).first() if blocked_job_id else None
                except (TypeError, ValueError):
                    blocked_job = None
                if (blocked_job and blocked_job.domain == "product-research.internal"
                        and str((blocked_job.configuration or {}).get("research_product_id")) == str(product.id)
                        and ((blocked_job.configuration or {}).get("result") or {}).get("business_outcome") == "needs_identity_resolution"):
                    from app.services.product_research_worker import _research_snapshot
                    blocked_job.status = "queued"
                    blocked_job.completed_at = None
                    blocked_job.error_summary = None
                    blocked_job.configuration = {
                        **(blocked_job.configuration or {}), "requested_mode": mode,
                        "selected_fields": fields, "research_objectives": objectives,
                        "research_phase": quality.get("research_phase"),
                        "before_metrics": _research_snapshot(db, product), "result": None,
                        "discovery": None,
                    }
                    research_job = blocked_job
                else:
                    research_job = _enqueue_product_research(
                        db, product, source_item, ProductImproveRequest(mode=mode, fields=fields),
                        current_user, objectives, research_priority=5,
                    )
                resumed = True
    record_audit(
        db, "CanonicalProduct", product.id, product.product_name, "update", before,
        {"identity_review": status_value, "resumed": resumed},
        {"identity_review": ["pending", status_value]}, current_user.id, "user",
        "Identity confirmed during enrichment review.",
    )
    db.commit()
    return {
        "product_id": str(product.id), "review_status": status_value,
        "product_understanding": current_understanding(db, product.id),
        "completeness": quality,
        "identity_review": does_this_product_require_identity_review(db, product, quality),
        "remaining_identity_requirements": quality.get("missing_identity_fields") or [],
        "resumed": resumed, "research_job_id": str(research_job.id) if research_job else None,
        "message": (
            "Identity resolved. Continuing enrichment..." if resumed else
            "Identity confirmed and saved." if not decision["requires_review"] else
            "Identity saved, but additional foundational information is still required."
        ),
    }


@router.post("/{product_id}/identity-review/skip")
def skip_identity_review(
    product_id: uuid.UUID, request: IdentityReviewSkipRequest,
    db: Session = Depends(get_db), current_user: User = Depends(require_editor_or_admin),
):
    product = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == product_id, CanonicalProduct.is_deleted == False,
    ).first()
    if not product:
        raise HTTPException(404, "Product not found")
    from app.services.identity_review import current_understanding, persist_review_state
    current = current_understanding(db, product.id)
    fingerprint = current.get("foundational_fingerprint")
    if request.understanding_fingerprint and fingerprint and request.understanding_fingerprint != fingerprint:
        raise HTTPException(409, "Product Understanding changed; refresh the review before skipping.")
    persist_review_state(
        db, product, status="SKIPPED", fingerprint=fingerprint, actor_id=current_user.id,
        reason="Identity review deferred by user.", resume_context=request.resume_context,
    )
    db.commit()
    return {"product_id": str(product.id), "review_status": "SKIPPED", "resumed": False}


@router.post(
    "/{product_id}/research", status_code=201,
    dependencies=[Depends(rate_limit("product_research", "RATE_LIMIT_CRAWL_CREATE"))],
)
def research_product(
    product_id: uuid.UUID,
    request: ProductResearchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin),
):
    product = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == product_id, CanonicalProduct.is_deleted == False,
    ).first()
    if not product:
        raise HTTPException(404, "Product not found")
    from app.services.product_identity import product_is_fragrance, trusted_product_version
    if product_is_fragrance(db, product) and not trusted_product_version(db, product):
        raise HTTPException(
            409,
            "Confirm the fragrance concentration in Product Identity before attaching research pages.",
        )
    urls = [url.strip() for url in request.urls if url.strip()]
    if not urls:
        raise HTTPException(422, "Add at least one official or approved retailer URL")
    if request.refresh_interval_hours is not None and not 1 <= request.refresh_interval_hours <= 8760:
        raise HTTPException(422, "Refresh interval must be between 1 hour and 1 year")
    domain = (urlparse(urls[0]).hostname or "").lower()
    from app.scraping.url_safety import UnsafeUrl, validate_public_url
    try:
        for url in urls:
            validate_public_url(url, expected_domain=domain, allow_subdomains=False)
    except UnsafeUrl as exc:
        raise HTTPException(422, str(exc)) from exc
    configuration = {
        "domain": domain, "starting_urls": urls, "crawl_mode": "single_url" if len(urls) == 1 else "multiple_urls",
        "maximum_crawl_depth": 0, "maximum_pages": len(urls), "maximum_product_pages": len(urls),
        "maximum_runtime_seconds": 300, "maximum_discovered_urls": len(urls),
        "use_sitemap": False, "use_category_discovery": False,
        "use_browser_rendering": request.use_browser_rendering, "respect_robots_txt": True,
        "allow_subdomains": False, "request_delay_seconds": 1.0, "per_domain_concurrency": 1,
        "retry_limit": 2, "request_timeout_seconds": 20, "maximum_response_bytes": 8000000,
        "maximum_redirects": 5, "browser_page_limit": len(urls),
        "country": None, "locale": None, "rescrape_interval_hours": request.refresh_interval_hours,
        "recrawl_strategy": "prices_and_availability" if request.refresh_interval_hours else "crawl_once",
        "allowed_url_patterns": [], "denied_url_patterns": [],
        "include_editorial": False,
    }
    job = CrawlJob(
        id=uuid.uuid4(), domain=domain, starting_urls=urls,
        crawl_mode=configuration["crawl_mode"], status="queued",
        configuration={
            **configuration, "research_product_id": str(product.id),
            "research_expected_format": _product_expected_format(db, product),
            "research_product_name": product.product_name,
        },
        requested_by_id=current_user.id,
    )
    db.add(job)
    db.commit()
    return {"crawl_job_id": str(job.id), "status": job.status, "domain": domain}


@router.post(
    "/{product_id}/discover-sources",
    dependencies=[Depends(rate_limit("product_discovery", "RATE_LIMIT_CRAWL_CREATE"))],
)
def discover_product_source_candidates(
    product_id: uuid.UUID,
    request: ProductSourceDiscoveryRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin),
):
    product = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == product_id, CanonicalProduct.is_deleted == False,
    ).first()
    if not product:
        raise HTTPException(404, "Product not found")
    from app.services.product_identity import product_is_fragrance, trusted_product_version
    if product_is_fragrance(db, product) and not trusted_product_version(db, product):
        raise HTTPException(
            409,
            "Confirm the fragrance concentration in Product Identity before discovering exact pages.",
        )
    from app.services.product_identity import preferred_product_variant
    variant = preferred_product_variant(db, product.id)
    from app.services.web_discovery import SearchProviderUnavailable, discover_product_sources
    try:
        results = discover_product_sources(
            brand=product.brand.name if product.brand else "",
            product_name=product.product_name,
            product_format=_product_expected_format(db, product),
            gtin=variant.gtin if variant and variant.gtin else "",
            approved_domains=request.approved_domains,
        )
        from app.services.product_identity import research_identity_compatible
        expected_format = _product_expected_format(db, product)
        results = [
            result for result in results
            if research_identity_compatible(
                expected_format,
                " ".join(str(result.get(key) or "") for key in ("title", "url", "snippet")),
                product.product_name,
            )
        ]
    except SearchProviderUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"query_product_id": str(product.id), "results": results}


@router.get("/{product_id}/research-results")
def product_research_results(
    product_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_viewer_or_above),
):
    rows = db.query(ScrapedProductObservation).filter(
        or_(
            ScrapedProductObservation.canonical_product_id == product_id,
            ScrapedProductObservation.possible_match_product_id == product_id,
        )
    ).order_by(ScrapedProductObservation.scraped_at.desc()).limit(50).all()
    return [{
        "id": str(row.id), "source_url": row.source_url,
        "source_domain": row.source_domain, "observed_at": row.scraped_at,
        "match_status": row.match_status, "changed_fields": row.changed_fields,
        "data": row.normalized_payload,
    } for row in rows]

@router.get("/{product_id}", response_model=ProductDetailOut)
def get_product_detail(
    product_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_viewer_or_above),
    variant: Optional[uuid.UUID] = None,
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
    category_parts = [part.strip() for part in (category_path or "").split(">") if part.strip()]

    # Fetch Variants
    variants = db.query(ProductVariant).filter(
        ProductVariant.canonical_product_id == product_id,
        ProductVariant.is_deleted == False
    ).all()
    if variant:
        variants.sort(key=lambda row: 0 if row.id == variant else 1)
    selected_variant = variants[0] if variants else None

    # Fetch Formulations
    formulations = db.query(Formulation).filter(
        Formulation.canonical_product_id == product_id,
        Formulation.is_deleted == False,
        func.length(func.trim(Formulation.raw_inci_text)) > 0,
        *( [or_(Formulation.product_variant_id == variant, Formulation.product_variant_id.is_(None))] if variant else [] ),
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
                    position=fi.position,
                    functions=[fn for fn in funcs if fn],
                    benefits=[bn for bn in bens if bn],
                    caution_notes=[note.strip() for note in (defn.possible_concerns or "").split(",") if note.strip()],
                    is_key_ingredient=fi.is_key_ingredient,
                    key_ingredient_status=fi.key_ingredient_status,
                    formulation_reference=f.id
                ))

    # Compatibility response derived from the single consolidated concern field.
    concerns_out = []
    for fv in fields:
        if fv.is_current and fv.field_name == "targeted_concerns":
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
            
            values = fv.value.get("values", []) if isinstance(fv.value, dict) else (fv.value or [])
            for concern in values:
                concerns_out.append(DynamicConcernOut(
                    concern_name=str(concern), targeting_status=fv.semantic_status or "inferred",
                    evidence=fv_ev, confidence=float(fv.confidence_score) if fv.confidence_score is not None else None,
                    source=fv.source_type
                ))

    market_observations = []
    for price in db.query(SourcePrice).join(ProductVariant).filter(
        ProductVariant.canonical_product_id == product_id
    ).order_by(SourcePrice.captured_at.desc()).all():
        listing = db.query(SourceListing).filter(SourceListing.id == price.source_listing_id).first() if price.source_listing_id else None
        market_observations.append({
            "source_name": price.retailer or (listing.retailer if listing else None),
            "market": price.country, "price": float(price.amount),
            "promotional_price": float(price.promotional_amount) if price.promotional_amount is not None else None,
            "currency": price.currency, "source_url": listing.source_url if listing else None,
            "observed_at": price.captured_at,
        })
    for listing in db.query(SourceListing).filter(
        SourceListing.canonical_product_id == product_id, SourceListing.is_deleted == False
    ).order_by(SourceListing.created_at.desc()).limit(20).all():
        job = db.query(ImportJob).filter(ImportJob.id == listing.import_job_id).first() if listing.import_job_id else None
        mapping, raw = (job.column_mapping or {}) if job else {}, listing.raw_data or {}
        def mapped(name):
            column = mapping.get(name)
            return raw.get(column) if column else None
        market_observations.append({
            "source_name": listing.retailer or (job.source_name if job else None),
            "market": mapped("market"), "availability": mapped("availability"),
            "rating": mapped("rating"), "review_count": mapped("review_count"),
            "source_url": listing.source_url, "observed_at": listing.created_at,
        })
    for observation in db.query(ScrapedProductObservation).filter(
        ScrapedProductObservation.canonical_product_id == product_id
    ).order_by(ScrapedProductObservation.scraped_at.desc()).limit(20).all():
        payload = observation.normalized_payload or {}
        market_observations.append({
            "source_name": observation.source_name, "source_domain": observation.source_domain,
            "market": observation.country or observation.locale, "price": payload.get("price"),
            "promotional_price": payload.get("promotional_price"), "currency": payload.get("currency"),
            "availability": payload.get("availability"), "rating": payload.get("rating"),
            "review_count": payload.get("review_count"), "review_summary": payload.get("review_summary"),
            "source_url": observation.source_url, "observed_at": observation.scraped_at,
        })

    # Preserve source-authored marketing copy and image URLs for existing imports.
    # New enrichment runs also persist image_url directly on CanonicalProduct.
    source_description = next((
        fv.value for fv in fields
        if fv.is_current and fv.field_name == "description" and fv.value not in (None, "")
    ), None)
    source_image_url = prod.image_url
    latest_source = db.query(SourceListing).filter(
        SourceListing.canonical_product_id == product_id,
        SourceListing.is_deleted == False,
    ).order_by(SourceListing.created_at.desc()).first()
    if latest_source:
        source_job = db.query(ImportJob).filter(ImportJob.id == latest_source.import_job_id).first()
        mapping = source_job.column_mapping if source_job else {}
        raw = latest_source.raw_data or {}
        description_key = (mapping or {}).get("description")
        image_key = (mapping or {}).get("image_url")
        if not source_description and description_key and raw.get(description_key):
            source_description = str(raw[description_key]).strip()
        if not source_description:
            for key in ("description", "marketing_description", "marketing_copy", "details"):
                if raw.get(key):
                    source_description = str(raw[key]).strip()
                    break
        if not source_image_url:
            from app.services.image_urls import normalize_public_image_url
            candidate = raw.get(image_key) if image_key else None
            if not candidate:
                for key in ("image_url", "image", "image_link", "photo_url", "picture"):
                    if raw.get(key):
                        candidate = raw[key]
                        break
            source_image_url = normalize_public_image_url(candidate)

    from app.knowledge_corpus.retrieval import public_evidence_summary, retrieve_corpus_evidence
    corpus_evidence = public_evidence_summary(retrieve_corpus_evidence(
        db, gtin=variants[0].gtin if variants else "", brand=brand_name or "",
        product_name=prod.product_name, category=category_path or "", max_comparables=3,
    ))
    from app.services.product_improvement import product_improvement_summary
    completeness = product_improvement_summary(db, prod)
    identity_review = completeness.get("identity_review") or {}

    from app.services.review_aggregate import select_review_aggregate
    review_aggregate = select_review_aggregate(db, product_id)
    if review_aggregate and review_aggregate.get("observation_id") is not None:
        review_aggregate = {**review_aggregate, "observation_id": str(review_aggregate["observation_id"])}
    source_attributes = []
    for fv, fv_out in zip(fields, fields_out):
        if not fv.is_current or fv.source_type != "source_data" or fv.value in (None, "", [], {}):
            continue
        evidence = fv_out.evidence or []
        customer_evidence = next((
            item for item in evidence
            if isinstance(item, dict)
            and item.get("evidence_type") == "explicit_customer_source"
            and item.get("import_job_id")
            and (item.get("source_listing_id") or str(fv.source_reference or "").startswith("feed:"))
        ), None)
        if not customer_evidence:
            continue
        source_header = next((
            str(item.get("source_header") or item.get("source_field"))
            for item in evidence if isinstance(item, dict) and (item.get("source_header") or item.get("source_field"))
        ), fv.field_name)
        source_attributes.append({
            "key": fv.field_name, "label": source_header, "value": fv.value,
            "source_type": fv.source_type, "source_reference": fv.source_reference,
            "source_header": source_header, "updated_at": fv.updated_at or fv.created_at,
        })
    source_attributes.sort(key=lambda item: (item["label"].lower(), item["key"]))

    return ProductDetailOut(
        id=prod.id,
        product_id=prod.id,
        product_variant_id=selected_variant.id if selected_variant else None,
        internal_code=product_internal_code(prod.id),
        product_name=prod.product_name,
        brand_id=prod.brand_id,
        brand_name=brand_name,
        category_id=prod.category_id,
        category_path=category_path,
        product_category=category_parts[0] if category_parts else None,
        subcategory=next((fv.value for fv in fields if fv.is_current and fv.field_name == "subcategory"), None) or (category_parts[-1] if len(category_parts) > 1 else None),
        product_type=next((fv.value for fv in fields if fv.is_current and fv.field_name == "product_type"), None),
        gtin=selected_variant.gtin if selected_variant else None,
        sku=variant_sku(db, selected_variant),
        variant_name=selected_variant.variant_name if selected_variant else None,
        size=selected_variant.size if selected_variant else None,
        unit=selected_variant.unit if selected_variant else None,
        variant_count=len(variants),
        image_url=source_image_url,
        description=source_description,
        review_status=prod.review_status,
        validation_issue_count=len([issue for issue in issues if not issue.resolved]),
        highest_issue_severity=max(
            (issue.severity for issue in issues if not issue.resolved),
            key=lambda value: {"blocking": 3, "error": 2, "warning": 1, "info": 0}.get(value, 0),
            default=None,
        ),
        tags=_tag_names(db, prod.id),
        identity_review_status=identity_review.get("review_status"),
        reviewer_id=prod.reviewer_id,
        is_deleted=prod.is_deleted,
        created_at=prod.created_at,
        updated_at=prod.updated_at,
        variants=variants,
        formulations=formulations,
        field_values=fields_out,
        source_attributes=source_attributes,
        validation_issues=issues,
        enrichment_metadata=global_meta,
        key_ingredients=key_ingredients_out,
        dynamic_concerns=concerns_out,
        market_observations=market_observations,
        review_aggregate=review_aggregate,
        corpus_evidence=corpus_evidence,
        product_understanding=next((
            fv.value for fv in fields
            if fv.is_current and fv.field_name == "product_understanding" and isinstance(fv.value, dict)
        ), None),
        completeness=completeness,
        identity_review=identity_review,
    )

@router.put("/{product_id}/image", response_model=ProductDetailOut)
def update_product_image(
    product_id: uuid.UUID,
    payload: ProductImageUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin),
):
    from app.services.image_urls import normalize_public_image_url

    product = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == product_id,
        CanonicalProduct.is_deleted == False,
    ).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found.")
    normalized = normalize_public_image_url(payload.image_url)
    if payload.image_url and not normalized:
        raise HTTPException(status_code=400, detail="Image URL must be a valid public HTTP or HTTPS URL.")
    before = product.image_url
    product.image_url = normalized
    record_audit(
        db=db,
        entity_type="CanonicalProduct",
        entity_id=product.id,
        display_label=product.product_name,
        action="update",
        before={"image_url": before},
        after={"image_url": normalized},
        changed={"image_url": [before, normalized]},
        user_id=current_user.id,
        actor_type="user",
        reason="Updated product image URL.",
    )
    db.commit()
    return get_product_detail(product_id, db, current_user)


@router.put("/{product_id}/tags", response_model=ProductDetailOut)
def update_product_tags(
    product_id: uuid.UUID,
    payload: ProductTagsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin),
):
    product = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == product_id,
        CanonicalProduct.is_deleted == False,
    ).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found.")
    _replace_product_tags(db, product, payload.tags, current_user)
    db.commit()
    return get_product_detail(product_id, db, current_user)

@router.get("/{product_id}/pdf")
def download_product_pdf(
    product_id: uuid.UUID,
    variant: Optional[uuid.UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_viewer_or_above),
):
    from app.services.product_pdf import build_product_pdf

    detail = get_product_detail(product_id, db, current_user, variant)
    pdf = build_product_pdf(detail)
    safe_name = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in detail.product_name
    ).strip("-") or "product"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}-product-sheet.pdf"'},
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

@router.put("/{product_id}/classification", response_model=ProductDetailOut)
def update_product_classification(
    product_id: uuid.UUID,
    payload: ProductClassificationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin),
):
    product = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == product_id, CanonicalProduct.is_deleted == False,
    ).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found.")
    try:
        _set_product_classification(db, product, payload.category, payload.subcategory, current_user)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Classification could not be saved: {exc}") from exc
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

    # Product Understanding can identify an unresolved foundational identity
    # before its validation issue has been synchronized. Preserve the existing
    # generic blocking-issue behavior above, then apply this additional gate.
    from app.services.product_improvement import product_improvement_summary
    from app.services.identity_review import current_understanding, does_this_product_require_identity_review
    understanding = current_understanding(db, prod.id)
    identity_decision = does_this_product_require_identity_review(
        db, prod, product_improvement_summary(db, prod),
    ) if understanding else {"requires_review": False}
    if identity_decision["requires_review"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot approve product while foundational identity requires confirmation.",
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

@router.post(
    "/bulk/actions/improve", status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("bulk_improve", "2/minute"))],
)
def bulk_improve_products(
    req: BulkImproveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin),
):
    """Queue durable missing-field improvement for selected grid products."""
    targets: list[tuple[uuid.UUID, uuid.UUID | None]] = []
    for variant_id in dict.fromkeys(req.product_variant_ids):
        variant = db.query(ProductVariant).filter(ProductVariant.id == variant_id, ProductVariant.is_deleted == False).first()
        if variant:
            targets.append((variant.canonical_product_id, variant.id))
        else:
            targets.append((uuid.UUID(int=0), variant_id))
    targets.extend((product_id, None) for product_id in dict.fromkeys(req.product_ids))
    if not targets:
        raise HTTPException(422, "Select at least one product.")
    if len(targets) > 100:
        raise HTTPException(422, "Bulk Improve supports up to 100 products per request.")
    if req.mode != "missing_only":
        raise HTTPException(422, "Bulk Improve currently supports missing_only mode.")

    from app.knowledge_corpus.retrieval import evidence_is_sufficient, retrieve_corpus_evidence
    from app.services.product_identity import preferred_product_variant
    from app.services.product_improvement import product_improvement_summary

    items = []
    queued_count = skipped_count = failed_count = 0
    improve_request = ProductImproveRequest(mode="missing_only", fields=[])
    for product_id, variant_id in targets:
        try:
            product = db.query(CanonicalProduct).filter(
                CanonicalProduct.id == product_id, CanonicalProduct.is_deleted == False,
            ).first()
            if not product:
                raise ValueError("Product not found")
            source_item = db.query(ImportJobItem).filter(
                ImportJobItem.canonical_product_id == product_id,
                ImportJobItem.source_listing_id.isnot(None),
                *([ImportJobItem.product_variant_id == variant_id] if variant_id else []),
            ).order_by(ImportJobItem.created_at.desc()).first()
            if not source_item:
                raise ValueError("No source record is available for enrichment")
            quality = product_improvement_summary(db, product)
            from app.services.identity_review import synchronize_blocking_issue
            synchronize_blocking_issue(db, product, quality.get("identity_review") or {})
            objectives = [entry["field"] for entry in quality.get("research_objectives") or []]
            gaps = quality.get("missing_high_priority_fields") or []
            if not objectives:
                skipped_count += 1
                items.append({
                    "product_id": str(product_id), "product_name": product.product_name,
                    "status": "skipped", "message": "No meaningful evidence gaps found.",
                })
                continue

            variant = (db.query(ProductVariant).filter(ProductVariant.id == variant_id).first()
                       if variant_id else preferred_product_variant(db, product.id))
            category = db.query(Category).filter(Category.id == product.category_id).first() if product.category_id else None
            corpus = retrieve_corpus_evidence(
                db, gtin=variant.gtin if variant else "",
                brand=product.brand.name if product.brand else "",
                product_name=product.product_name,
                category=category.path if category else "",
            )
            local_only = evidence_is_sufficient(corpus, None) and not quality.get("market_observation_gaps")
            initial_discovery = ({
                "provider": "internal_corpus", "status": "completed", "response_id": None,
                "domains": [], "candidates": [],
            } if local_only else None)
            research_job = _enqueue_product_research(
                db, product, source_item, improve_request, current_user,
                objectives,
                initial_discovery=initial_discovery,
                research_priority=10,
            )
            db.commit()
            queued_count += 1
            items.append({
                "product_id": str(product_id), "product_variant_id": str(variant_id) if variant_id else None,
                "product_name": product.product_name,
                "status": research_job.status, "research_job_id": str(research_job.id),
                "web_search_planned": not local_only,
                "missing_high_priority_fields": gaps,
            })
        except Exception as exc:
            db.rollback()
            failed_count += 1
            items.append({"product_id": str(product_id), "product_variant_id": str(variant_id) if variant_id else None,
                          "status": "failed", "error": str(exc)})

    db.commit()
    return {
        "action": "improve", "requested_count": len(targets),
        "queued_count": queued_count, "skipped_count": skipped_count,
        "failed_count": failed_count, "items": items,
        "message": f"Queued {queued_count} products for background improvement.",
    }


@router.post("/bulk/actions/improve/status")
def bulk_improve_status(
    req: BulkImproveStatusRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_viewer_or_above),
):
    """Return one authoritative progress snapshot for a set of improvement jobs."""
    job_ids = list(dict.fromkeys(req.research_job_ids))
    if not job_ids:
        raise HTTPException(422, "Provide at least one research job.")
    if len(job_ids) > 100:
        raise HTTPException(422, "Bulk status supports up to 100 research jobs per request.")

    jobs = db.query(CrawlJob).filter(
        CrawlJob.id.in_(job_ids),
        CrawlJob.domain == "product-research.internal",
    ).all()
    by_id = {job.id: job for job in jobs}
    terminal = {"completed", "partially_completed", "failed", "blocked", "cancelled"}
    successful_outcomes = {"improved", "partially_improved"}
    items = []
    for job_id in job_ids:
        job = by_id.get(job_id)
        if not job:
            items.append({
                "research_job_id": str(job_id), "product_id": None,
                "status": "failed", "terminal": True, "successful": False,
                "error": "Research job not found.",
            })
            continue
        configuration = job.configuration or {}
        result = configuration.get("result") or {}
        outcome = result.get("business_outcome")
        if not outcome and job.status in terminal:
            outcome = "failed" if job.status in {"failed", "blocked", "cancelled"} else "partially_improved"
        product = db.query(CanonicalProduct).filter(
            CanonicalProduct.id == configuration.get("research_product_id")
        ).first()
        items.append({
            "research_job_id": str(job.id),
            "product_id": configuration.get("research_product_id"),
            "product_name": product.product_name if product else None,
            "status": job.status,
            "waiting_for_rate_limit": bool(configuration.get("waiting_for_rate_limit")),
            "business_outcome": outcome,
            "business_status": result.get("business_status"),
            "terminal": job.status in terminal,
            "successful": outcome in successful_outcomes,
            "error": job.error_summary,
            "before_completeness": result.get("before_completeness"),
            "after_completeness": result.get("after_completeness"),
            "fields_added": result.get("fields_added") or [],
            "fields_still_missing": result.get("fields_still_missing") or [],
            "sources_discovered": result.get("sources_discovered", 0),
            "sources_ingested": result.get("sources_ingested", 0),
            "sources_blocked": result.get("sources_blocked", 0),
            "image_added": result.get("image_added", False),
            "review_evidence_added": result.get("review_evidence_added", False),
            "review_evidence_found_this_run": result.get("review_evidence_found_this_run", False),
            "failure_reason": result.get("failure_reason"),
            "result": result,
        })

    completed_count = sum(1 for item in items if item["terminal"])
    successful_count = sum(1 for item in items if item["successful"])
    failed_count = sum(1 for item in items if item["terminal"] and not item["successful"])
    outcome_counts = {
        outcome: sum(1 for item in items if item.get("business_outcome") == outcome)
        for outcome in (
            "improved", "partially_improved", "no_material_improvement",
            "needs_identity_resolution", "needs_taxonomy_resolution", "blocked_sources", "rate_limited_retriable", "failed",
        )
    }
    return {
        "requested_count": len(job_ids),
        "completed_count": completed_count,
        "pending_count": len(job_ids) - completed_count,
        "successful_count": successful_count,
        "failed_count": failed_count,
        "progress_percent": round((completed_count / len(job_ids)) * 100),
        "all_terminal": completed_count == len(job_ids),
        "outcome_counts": outcome_counts,
        "items": items,
    }


@router.post("/bulk/actions", status_code=status.HTTP_200_OK)
def bulk_product_action(
    req: BulkActionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin)
):
    targets: list[tuple[uuid.UUID, uuid.UUID | None]] = []
    for variant_id in dict.fromkeys(req.product_variant_ids):
        variant = db.query(ProductVariant).filter(ProductVariant.id == variant_id, ProductVariant.is_deleted == False).first()
        targets.append((variant.canonical_product_id if variant else uuid.UUID(int=0), variant_id))
    targets.extend((product_id, None) for product_id in dict.fromkeys(req.product_ids))
    action = req.action

    if not targets:
        raise HTTPException(status_code=422, detail="Select at least one product.")
    if len(targets) > 100:
        raise HTTPException(status_code=422, detail="Bulk actions support up to 100 products per request.")
    if action not in ["approve", "reject", "re_enrich", "set_classification", "add_tags", "remove_tags"]:
        raise HTTPException(status_code=400, detail="Invalid action name")
    if action == "set_classification" and (not req.category or not req.subcategory):
        raise HTTPException(status_code=400, detail="Category and subcategory are required.")
    if action in {"add_tags", "remove_tags"} and not _normalize_tags(req.tags):
        raise HTTPException(status_code=400, detail="Enter at least one tag.")

    success_count = 0
    errors = []
    items = []

    processed_canonical: set[uuid.UUID] = set()
    for pid, variant_id in targets:
        try:
            if pid.int == 0:
                raise HTTPException(status_code=404, detail="Product variant not found")
            # Canonical actions are intentionally idempotent when several
            # selected variant rows share one parent. Each selected row still
            # receives its own successful result below.
            already_processed = pid in processed_canonical
            if action == "approve":
                if not already_processed: approve_product(pid, db, current_user)
            elif action == "reject":
                if not already_processed: reject_product(pid, db, current_user)
            elif action == "re_enrich":
                detail = re_enrich_product(pid, variant_id, db, current_user)
                improvement = detail.improvement_result or {}
                items.append({
                    "product_id": str(pid),
                    "status": "queued" if improvement.get("research_pending") else "completed",
                    "research_job_id": improvement.get("research_job_id"),
                })
            elif action == "set_classification":
                product = db.query(CanonicalProduct).filter(CanonicalProduct.id == pid, CanonicalProduct.is_deleted == False).first()
                if not product:
                    raise HTTPException(status_code=404, detail="Product not found")
                if not already_processed:
                    _set_product_classification(db, product, req.category or "", req.subcategory or "", current_user)
                    db.commit()
            elif action in {"add_tags", "remove_tags"}:
                product = db.query(CanonicalProduct).filter(
                    CanonicalProduct.id == pid, CanonicalProduct.is_deleted == False,
                ).first()
                if not product:
                    raise HTTPException(status_code=404, detail="Product not found")
                existing = _tag_names(db, product.id)
                requested = [name for _, name in _normalize_tags(req.tags)]
                if action == "add_tags":
                    target = existing + requested
                else:
                    removed = {name.casefold() for name in requested}
                    target = [name for name in existing if name.casefold() not in removed]
                if not already_processed:
                    _replace_product_tags(db, product, target, current_user)
                    db.commit()
            processed_canonical.add(pid)
            success_count += 1
            if action != "re_enrich":
                items.append({"product_id": str(pid), "product_variant_id": str(variant_id) if variant_id else None,
                              "status": "completed"})
        except HTTPException as e:
            db.rollback()
            errors.append({"product_id": str(pid), "error": e.detail})
            items.append({"product_id": str(pid), "status": "failed", "error": e.detail})
        except Exception as e:
            db.rollback()
            errors.append({"product_id": str(pid), "error": str(e)})
            items.append({"product_id": str(pid), "status": "failed", "error": str(e)})

    return {
        "action": action,
        "success_count": success_count,
        "failed_count": len(targets) - success_count,
        "errors": errors,
        "items": items,
    }
