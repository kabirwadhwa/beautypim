import pytest
from sqlalchemy.orm import Session
import uuid
from fastapi.testclient import TestClient
from fastapi import HTTPException
from app.worker import create_field_value_version, run_job_worker
from app.models import FieldValue, CanonicalProduct, Brand, ValidationIssue, User
from app.routes.products import approve_product

def test_override_preservation_locked(db: Session):
    brand = Brand(id=uuid.uuid4(), name="The Ordinary", normalized_name="theordinary")
    db.add(brand)
    db.flush()

    prod = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Niacinamide 10%", normalized_name="niacinamide10")
    db.add(prod)
    db.flush()

    # Seed human edit (is_current = True)
    human_fv = FieldValue(
        id=uuid.uuid4(),
        canonical_product_id=prod.id,
        field_name="vegan",
        value="yes",
        source_type="human_edit",
        review_status="confirmed",
        is_current=True
    )
    db.add(human_fv)
    db.commit()

    # Trigger AI writes vegan = "no"
    create_field_value_version(
        db=db,
        canonical_product_id=prod.id,
        product_variant_id=None,
        field_name="vegan",
        value="no",
        source_type="ai_inference",
        source_ref="test_run",
        confidence=0.99,
        status="inferred"
    )
    db.commit()

    # Human value must remain current
    db.refresh(human_fv)
    assert human_fv.is_current == True
    
    # AI candidate must be recorded as non-current
    ai_fv = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == prod.id,
        FieldValue.source_type == "ai_inference"
    ).first()
    assert ai_fv is not None
    assert ai_fv.is_current == False
    
    # Conflict warning issue must be registered
    issue = db.query(ValidationIssue).filter(ValidationIssue.canonical_product_id == prod.id).first()
    assert issue is not None
    assert issue.issue_type == "conflicting_information"

def test_blocking_issue_prevents_approval(db: Session):
    brand = Brand(id=uuid.uuid4(), name="La Roche-Posay", normalized_name="larocheposay")
    db.add(brand)
    db.flush()

    prod = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Cicaplast", normalized_name="cicaplast")
    db.add(prod)
    db.flush()

    # Seed blocking issue
    issue = ValidationIssue(
        id=uuid.uuid4(),
        canonical_product_id=prod.id,
        severity="blocking",
        issue_type="missing_ean",
        message="Missing barcode code on variant.",
        created_by_type="system"
    )
    db.add(issue)
    db.commit()

    user = db.query(User).filter(User.role == "admin").first()

    # Attempt approve must raise HTTP Exception (400 Bad Request)
    with pytest.raises(HTTPException) as exc_info:
        approve_product(prod.id, db, user)
    assert exc_info.value.status_code == 400
    assert "blocking validation issue" in exc_info.value.detail

def test_run_job_worker_lifecycle(db: Session):
    from app.models import ImportJob, ImportJobItem, SourceListing, CanonicalProduct, ProductVariant, FieldValue
    
    # 1. Create Import Job
    job_id = uuid.uuid4()
    job = ImportJob(
        id=job_id,
        filename="test_products.csv",
        file_hash="test_file_hash_123",
        status="pending",
        column_mapping={
            "product_name": "name",
            "brand": "brand",
            "ean": "ean",
            "size": "size",
            "price": "price",
            "description": "desc",
            "ingredients": "ingredients"
        }
    )
    db.add(job)
    db.flush()

    # 2. Add Source Listing
    listing_id = uuid.uuid4()
    listing = SourceListing(
        id=listing_id,
        import_job_id=job_id,
        raw_data={
            "name": "Hyaluronic Acid 2% + B5",
            "brand": "The Ordinary",
            "ean": "761805012345",
            "size": "30ml",
            "price": "8.90",
            "desc": "A hydrating formula with ultra-pure, vegan hyaluronic acid.",
            "ingredients": "Aqua, Pentylene Glycol, Sodium Hyaluronate"
        },
        source_hash="test_source_hash_999",
        retailer="deciem"
    )
    db.add(listing)
    db.flush()

    # 3. Add Import Job Item
    item = ImportJobItem(
        id=uuid.uuid4(),
        import_job_id=job_id,
        source_row_number=1,
        source_listing_id=listing_id,
        status="pending",
        match_status="not_evaluated",
        enrichment_status="not_requested"
    )
    db.add(item)
    db.commit()

    # 4. Trigger Worker Run
    run_job_worker(db, job_id)

    # 5. Assertions
    db.refresh(job)
    assert job.status == "completed"
    assert job.processed_rows == 1

    # Check item status
    db.refresh(item)
    assert item.status == "completed"
    assert item.match_status == "new_product"
    assert item.enrichment_status == "succeeded"

    # Check Canonical Product created
    canonical = db.query(CanonicalProduct).filter(CanonicalProduct.id == item.canonical_product_id).first()
    assert canonical is not None
    assert canonical.product_name == "Hyaluronic Acid 2% + B5"

    # Check Variant created
    variant = db.query(ProductVariant).filter(ProductVariant.canonical_product_id == canonical.id).first()
    assert variant is not None
    assert variant.gtin == "761805012345"
    assert variant.size == "30ml"

    # Check Field Value created for vegan
    vegan_fv = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == canonical.id,
        FieldValue.field_name == "vegan",
        FieldValue.is_current == True
    ).first()
    assert vegan_fv is not None
    assert vegan_fv.value == "yes"  # Keyword 'vegan' detected

def test_recover_unfinished_jobs(db: Session):
    from app.worker import run_job_in_background, recover_unfinished_jobs
    from app.models import ImportJob, ImportJobItem
    
    # 1. Create a job stuck in processing
    job_id = uuid.uuid4()
    job = ImportJob(
        id=job_id,
        filename="test_unfinished.csv",
        file_hash="test_file_hash_unfinished",
        status="processing",
        column_mapping={"product_name": "name"}
    )
    db.add(job)
    db.flush()
    
    # Run background wrapper directly to test logic
    run_job_in_background(job_id)
    
    db.refresh(job)
    assert job.status == "processing"

