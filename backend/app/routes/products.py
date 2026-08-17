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
    ScrapedProductObservation, CrawlJob, SourcePrice
)
from app.schemas import (
    ProductOut, ProductDetailOut, ProductEdit, FieldEnrichmentMetadataOut,
    FieldValueOut, EnrichmentMetadataSchema, KeyIngredientOut, DynamicConcernOut,
    EDITABLE_FIELDS_REGISTRY, ProductCategoryUpdate, ProductClassificationUpdate, ProductImageUpdate
)
from app.worker import record_audit, process_item_enrichment, create_field_value_version
from pydantic import BaseModel

from app.limiter import rate_limit
from app.config import settings
from app.services.deduplication import normalize_text

class BulkActionRequest(BaseModel):
    product_ids: List[uuid.UUID]
    action: str
    category: Optional[str] = None
    subcategory: Optional[str] = None


class BulkImproveRequest(BaseModel):
    product_ids: List[uuid.UUID]
    mode: str = "missing_only"


class ProductImproveRequest(BaseModel):
    mode: str = "missing_only"
    fields: List[str] = []


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


class ProductResearchRequest(BaseModel):
    urls: List[str]
    use_browser_rendering: bool = False
    refresh_interval_hours: Optional[int] = None


class ProductSourceDiscoveryRequest(BaseModel):
    approved_domains: List[str] = []

router = APIRouter(prefix="/products", tags=["Product PIM Center"])


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


def _clean_classification(value: str, label: str) -> str:
    cleaned = " ".join(value.split()).strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail=f"{label} is required.")
    return cleaned.title()


def _set_product_classification(db: Session, product: CanonicalProduct, category: str, subcategory: str, user: User):
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
    product_ids = [product.id for product in products]
    variant_counts = dict(
        db.query(ProductVariant.canonical_product_id, func.count(ProductVariant.id))
        .filter(
            ProductVariant.canonical_product_id.in_(product_ids),
            ProductVariant.is_deleted == False,
        )
        .group_by(ProductVariant.canonical_product_id)
        .all()
    ) if product_ids else {}

    # Format output items with Brand and Category titles
    out = []
    for prod in products:
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
            
        from app.services.product_identity import preferred_product_variant
        variant = preferred_product_variant(db, prod.id)
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
            product_category=category_parts[0] if category_parts else None,
            subcategory=current_classifications.get("subcategory") or (
                category_parts[-1] if len(category_parts) > 1 else None
            ),
            product_type=current_classifications.get("product_type"),
            gtin=variant.gtin if variant else None,
            variant_count=variant_counts.get(prod.id, 0),
            image_url=prod.image_url,
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
        # Re-enrichment must use the same evidence-gap orchestration as Improve
        # Product.  Previously it stopped after the catalogue/LLM pass, so an
        # exact, trusted EDT could remain without INCI forever even though the
        # missing formulation was researchable.
        from app.services.product_improvement import product_improvement_summary
        from app.services.product_identity import product_is_fragrance, trusted_product_version
        quality = product_improvement_summary(db, product)
        objectives = [entry["field"] for entry in quality.get("research_objectives") or []]
        research_job = None
        identity_is_safe = not product_is_fragrance(db, product) or trusted_product_version(db, product)
        if objectives and identity_is_safe and (settings.OPENAI_API_KEY or settings.BRAVE_SEARCH_API_KEY):
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
) -> dict:
    """Discover and ingest a small exact-product evidence set before enrichment.

    This is deliberately bounded: up to three exact product-page candidates
    across distinct domains and no category traversal. Failure to access one source does not
    prevent the normal enrichment fallback from running.
    """
    from app.services.web_discovery import discover_product_sources
    from app.services.image_urls import normalize_public_image_url
    from app.scraping.runner import run_crawl_job

    from app.services.product_identity import preferred_product_variant
    variant = preferred_product_variant(db, product.id)
    expected_format = _product_expected_format(db, product)
    from app.services.product_identity import product_is_fragrance, trusted_product_version
    if product_is_fragrance(db, product) and not trusted_product_version(db, product):
        return {
            "candidates": 0, "sources_ingested": 0,
            "image_found": bool(product.image_url), "review_evidence_found": False,
            "official_evidence_found": False, "formulation_evidence_found": False,
            "variant_identity_found": False, "identity_required": True,
            "errors": [
                "Confirm the fragrance concentration (for example EDT, EDP, Parfum or Elixir) "
                "before exact-page research so editions are not mixed."
            ],
        }
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
    candidates = [
        candidate for candidate in candidates
        if research_identity_compatible(
            expected_format,
            " ".join(str(candidate.get(key) or "") for key in ("title", "url", "snippet")),
            product.product_name,
        )
    ]
    candidates = sorted(candidates, key=candidate_score, reverse=True)
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
    source_limit = 6 if "inci" in set(research_objectives or []) else 3
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

    for url, domain in selected:
        configuration = {
            "domain": domain, "starting_urls": [url], "crawl_mode": "single_url",
            "maximum_crawl_depth": 0, "maximum_pages": 1, "maximum_product_pages": 1,
            "maximum_runtime_seconds": 45, "maximum_discovered_urls": 1,
            "use_sitemap": False, "use_category_discovery": False,
            "use_browser_rendering": False, "respect_robots_txt": True,
            "allow_subdomains": False, "request_delay_seconds": 0.25,
            "per_domain_concurrency": 1, "retry_limit": 0,
            "request_timeout_seconds": 20, "maximum_response_bytes": 8000000,
            "maximum_redirects": 5, "browser_page_limit": 1,
            "country": None, "locale": None, "rescrape_interval_hours": None,
            "recrawl_strategy": "crawl_once", "allowed_url_patterns": [],
            "denied_url_patterns": [], "include_editorial": False,
        }
        job = CrawlJob(
            id=uuid.uuid4(), domain=domain, starting_urls=[url],
            crawl_mode="single_url", status="queued",
            configuration={
                **configuration, "research_product_id": str(product.id),
                "research_expected_format": expected_format,
                "research_product_name": product.product_name,
            },
            requested_by_id=user.id,
        )
        db.add(job)
        db.commit()
        try:
            run_crawl_job(db, job.id)
            db.refresh(job)
            if job.products_persisted:
                completed += 1
                db.refresh(product)
                # Official pages commonly provide imagery but no customer-review
                # aggregate. Continue across distinct sources until both evidence
                # needs are met, while retaining every source independently.
                if (
                    product.image_url and has_review_evidence()
                    and (has_official_evidence() or has_formulation_evidence())
                    and has_variant_identity()
                ):
                    break
            elif job.error_summary:
                errors.append(f"{domain}: {job.error_summary}")
        except Exception as exc:
            db.rollback()
            errors.append(f"{domain}: {exc}")
    db.commit()
    return {
        "candidates": len(candidates), "sources_ingested": completed,
        "image_found": bool(product.image_url), "review_evidence_found": has_review_evidence(),
        "official_evidence_found": has_official_evidence(),
        "formulation_evidence_found": has_formulation_evidence(),
        "variant_identity_found": has_variant_identity(),
        "errors": errors,
    }


def _enqueue_product_research(
    db: Session, product: CanonicalProduct, item: ImportJobItem,
    request: ProductImproveRequest, user: User, research_objectives: list[str] | None = None,
    initial_discovery: dict | None = None,
) -> CrawlJob:
    from app.services.product_improvement import product_improvement_summary
    active = db.query(CrawlJob).filter(
        CrawlJob.domain == "product-research.internal",
        CrawlJob.status.in_(["queued", "discovering", "crawling", "parsing"]),
    ).order_by(CrawlJob.created_at.desc()).all()
    for job in active:
        if str((job.configuration or {}).get("research_product_id")) == str(product.id):
            return job
    job = CrawlJob(
        id=uuid.uuid4(), domain="product-research.internal", starting_urls=[],
        crawl_mode="single_url", status="queued", requested_by_id=user.id,
        configuration={
            "product_research_job": True,
            "research_product_id": str(product.id),
            "research_item_id": str(item.id),
            "requested_mode": request.mode,
            "selected_fields": request.fields,
            "research_objectives": research_objectives or [],
            "research_phase": product_improvement_summary(db, product).get("research_phase"),
            "discovery": initial_discovery,
            "result": None,
        },
    )
    db.add(job)
    db.flush()
    return job


def _research_job_payload(job: CrawlJob) -> dict:
    configuration = job.configuration or {}
    return {
        "research_job_id": str(job.id),
        "research_status": job.status,
        "research_pending": job.status in {"queued", "discovering", "crawling", "parsing"},
        "result": configuration.get("result"),
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
        meaningful_gaps = before_quality.get("missing_high_priority_fields") or []
        if not meaningful_gaps:
            research_summary = {
                "web_search_skipped": True, "reason": "No meaningful evidence gaps found.",
                "sources_ingested": 0, "errors": [],
            }
        elif evidence_is_sufficient(corpus_result, requested):
            research_summary = {
                "web_search_skipped": True, "reason": "Exact internal retail evidence already covers the requested product fields.",
                "corpus_match_level": corpus_result.get("match_level"), "sources_ingested": 0, "errors": [],
            }
        elif settings.OPENAI_API_KEY or settings.BRAVE_SEARCH_API_KEY:
            from app.services.product_identity import product_is_fragrance, trusted_product_version
            if product_is_fragrance(db, product) and not trusted_product_version(db, product):
                research_summary = {
                    "identity_required": True, "sources_ingested": 0,
                    "errors": [
                        "Confirm the fragrance concentration (for example EDT, EDP, Parfum or Elixir) "
                        "before exact-page research so editions are not mixed."
                    ],
                }
            else:
                research_job = _enqueue_product_research(
                    db, product, item, request, current_user,
                    [item["field"] for item in before_quality.get("research_objectives") or []],
                )
                research_summary = {
                    **_research_job_payload(research_job),
                    "sources_ingested": 0, "errors": [],
                    "message": "Catalogue enrichment completed. Image and review research is continuing in the background.",
                }
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
    }
    return detail


@router.put("/{product_id}/identity")
def update_product_identity(
    product_id: uuid.UUID,
    request: ProductIdentityUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin),
):
    product = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == product_id, CanonicalProduct.is_deleted == False,
    ).first()
    if not product:
        raise HTTPException(404, "Product not found")
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
    _refresh_product_understanding(db, product)
    db.commit()
    return {"updated": True, "product_id": str(product.id)}


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
    category_parts = [part.strip() for part in (category_path or "").split(">") if part.strip()]

    # Fetch Variants
    variants = db.query(ProductVariant).filter(
        ProductVariant.canonical_product_id == product_id,
        ProductVariant.is_deleted == False
    ).all()

    # Fetch Formulations
    formulations = db.query(Formulation).filter(
        Formulation.canonical_product_id == product_id,
        Formulation.is_deleted == False,
        func.length(func.trim(Formulation.raw_inci_text)) > 0,
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
    source_description = None
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
        if description_key and raw.get(description_key):
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

    from app.services.review_aggregate import select_review_aggregate
    review_aggregate = select_review_aggregate(db, product_id)
    if review_aggregate and review_aggregate.get("observation_id") is not None:
        review_aggregate = {**review_aggregate, "observation_id": str(review_aggregate["observation_id"])}
    return ProductDetailOut(
        id=prod.id,
        internal_code=product_internal_code(prod.id),
        product_name=prod.product_name,
        brand_id=prod.brand_id,
        brand_name=brand_name,
        category_id=prod.category_id,
        category_path=category_path,
        product_category=category_parts[0] if category_parts else None,
        subcategory=next((fv.value for fv in fields if fv.is_current and fv.field_name == "subcategory"), None) or (category_parts[-1] if len(category_parts) > 1 else None),
        product_type=next((fv.value for fv in fields if fv.is_current and fv.field_name == "product_type"), None),
        gtin=variants[0].gtin if variants else None,
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
        dynamic_concerns=concerns_out,
        market_observations=market_observations,
        review_aggregate=review_aggregate,
        corpus_evidence=corpus_evidence,
        product_understanding=next((
            fv.value for fv in fields
            if fv.is_current and fv.field_name == "product_understanding" and isinstance(fv.value, dict)
        ), None),
        completeness=completeness,
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

@router.get("/{product_id}/pdf")
def download_product_pdf(
    product_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_viewer_or_above),
):
    from app.services.product_pdf import build_product_pdf

    detail = get_product_detail(product_id, db, current_user)
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
    product_ids = list(dict.fromkeys(req.product_ids))
    if not product_ids:
        raise HTTPException(422, "Select at least one product.")
    if len(product_ids) > 50:
        raise HTTPException(422, "Bulk Improve supports up to 50 products per request.")
    if req.mode != "missing_only":
        raise HTTPException(422, "Bulk Improve currently supports missing_only mode.")

    from app.knowledge_corpus.retrieval import evidence_is_sufficient, retrieve_corpus_evidence
    from app.services.product_identity import preferred_product_variant
    from app.services.product_improvement import product_improvement_summary

    items = []
    queued_count = skipped_count = failed_count = 0
    improve_request = ProductImproveRequest(mode="missing_only", fields=[])
    for product_id in product_ids:
        try:
            product = db.query(CanonicalProduct).filter(
                CanonicalProduct.id == product_id, CanonicalProduct.is_deleted == False,
            ).first()
            if not product:
                raise ValueError("Product not found")
            source_item = db.query(ImportJobItem).filter(
                ImportJobItem.canonical_product_id == product_id,
                ImportJobItem.source_listing_id.isnot(None),
            ).order_by(ImportJobItem.created_at.desc()).first()
            if not source_item:
                raise ValueError("No source record is available for enrichment")
            quality = product_improvement_summary(db, product)
            gaps = quality.get("missing_high_priority_fields") or []
            if not gaps:
                skipped_count += 1
                items.append({
                    "product_id": str(product_id), "product_name": product.product_name,
                    "status": "skipped", "message": "No meaningful evidence gaps found.",
                })
                continue

            variant = preferred_product_variant(db, product.id)
            category = db.query(Category).filter(Category.id == product.category_id).first() if product.category_id else None
            corpus = retrieve_corpus_evidence(
                db, gtin=variant.gtin if variant else "",
                brand=product.brand.name if product.brand else "",
                product_name=product.product_name,
                category=category.path if category else "",
            )
            local_only = evidence_is_sufficient(corpus, None)
            initial_discovery = ({
                "provider": "internal_corpus", "status": "completed", "response_id": None,
                "domains": [], "candidates": [],
            } if local_only else None)
            research_job = _enqueue_product_research(
                db, product, source_item, improve_request, current_user,
                [entry["field"] for entry in quality.get("research_objectives") or []],
                initial_discovery=initial_discovery,
            )
            db.commit()
            queued_count += 1
            items.append({
                "product_id": str(product_id), "product_name": product.product_name,
                "status": research_job.status, "research_job_id": str(research_job.id),
                "web_search_planned": not local_only,
                "missing_high_priority_fields": gaps,
            })
        except Exception as exc:
            db.rollback()
            failed_count += 1
            items.append({"product_id": str(product_id), "status": "failed", "error": str(exc)})

    db.commit()
    return {
        "action": "improve", "requested_count": len(product_ids),
        "queued_count": queued_count, "skipped_count": skipped_count,
        "failed_count": failed_count, "items": items,
        "message": f"Queued {queued_count} products for background improvement.",
    }


@router.post("/bulk/actions", status_code=status.HTTP_200_OK)
def bulk_product_action(
    req: BulkActionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin)
):
    product_ids = req.product_ids
    action = req.action

    if action not in ["approve", "reject", "re_enrich", "set_classification"]:
        raise HTTPException(status_code=400, detail="Invalid action name")
    if action == "set_classification" and (not req.category or not req.subcategory):
        raise HTTPException(status_code=400, detail="Category and subcategory are required.")

    success_count = 0
    errors = []

    for pid in product_ids:
        try:
            if action == "approve":
                approve_product(pid, db, current_user)
            elif action == "reject":
                reject_product(pid, db, current_user)
            elif action == "re_enrich":
                re_enrich_product(pid, db, current_user)
            elif action == "set_classification":
                product = db.query(CanonicalProduct).filter(CanonicalProduct.id == pid, CanonicalProduct.is_deleted == False).first()
                if not product:
                    raise HTTPException(status_code=404, detail="Product not found")
                _set_product_classification(db, product, req.category or "", req.subcategory or "", current_user)
                db.commit()
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
