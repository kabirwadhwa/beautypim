import pytest
from sqlalchemy.orm import Session
import uuid
from fastapi.testclient import TestClient
from fastapi import HTTPException
from app.worker import (
    apply_category_specific_enrichment, collect_structured_evidence,
    compact_enrichment_value, create_field_value_version, normalize_claim_value,
    normalize_gtin_value, run_job_worker, source_alias_value,
)
from app.models import FieldValue, CanonicalProduct, Brand, ValidationIssue, User
from app.routes.products import approve_product


def test_premium_fragrance_normalization_and_missing_inci_message():
    result = apply_category_specific_enrichment({
        "gender_target": {"value": "Men seeking an evening fragrance"},
        "fragrance_intelligence": {
            "applicable": True, "fragrance_presence_status": "yes",
            "fragrance_family": "Woody", "top_notes": ["Bergamot"],
            "middle_notes": [], "base_notes": ["Amber"], "evidence": [],
            "confidence": 0.8,
        },
    }, "Dior Sauvage Eau de Toilette perfume", "")
    assert result["gender_target"]["value"] == "Men"
    assert result["application_area"]["value"] == "Pulse points and skin"
    assert result["absorption_profile"]["value"] == "Evaporative fragrance"
    assert result["fragrance_intelligence"]["concentration"] == "Eau de Toilette"
    assert result["fragrance_intelligence"]["longevity_profile"] == "Moderate"
    assert result["allergen_warning_observation"]["review_required"] is True
    assert "Cannot assess" in result["allergen_warning_observation"]["review_message"]


def test_premium_helpers_bound_context_and_promote_nested_evidence():
    compacted = compact_enrichment_value({"description": "x" * 5000, "rows": list(range(30))})
    assert len(compacted["description"]) == 3000
    assert len(compacted["rows"]) == 10
    evidence = collect_structured_evidence([
        {"statement": "Hydrates", "evidence": "Contains glycerin."},
        {"evidence": [{"source_field": "description", "supporting_text": "Fresh citrus.", "evidence_type": "explicit"}]},
    ])
    assert [item["supporting_text"] for item in evidence] == ["Contains glycerin.", "Fresh citrus."]


def test_fragrance_does_not_promote_model_guessed_concentration():
    result = apply_category_specific_enrichment({
        "fragrance_intelligence": {
            "applicable": True, "concentration": "Eau de Parfum",
            "longevity_profile": "Long-lasting", "sillage_projection": "Strong",
        },
    }, "Dior Sauvage Perfume __model_type__ Eau de Parfum", "")

    assert result["fragrance_intelligence"]["concentration"] == "Not specified in source"
    assert result["fragrance_intelligence"]["longevity_profile"] == "Varies by concentration and application"


def test_binary_claim_values_are_normalized_across_providers():
    assert normalize_claim_value(True) == "yes"
    assert normalize_claim_value("TRUE") == "yes"
    assert normalize_claim_value(False) == "no"
    assert normalize_claim_value(None) == "unverified"


def test_spreadsheet_gtin_decimal_suffix_is_removed_safely():
    assert normalize_gtin_value("3605970360757.0") == "3605970360757"
    assert normalize_gtin_value(" 769915190540 ") == "769915190540"
    assert normalize_gtin_value("not-an-id") is None


def test_unmapped_type_column_is_still_available_as_product_identity():
    assert source_alias_value({"Type": "Eau de Toilette"}, "product type", "type") == "Eau de Toilette"


def test_category_specific_normalization_does_not_leak_into_skincare():
    original = {
        "application_area": {"value": "Face"},
        "absorption_profile": {"value": "Fast-absorbing"},
    }
    result = apply_category_specific_enrichment(original, "Vitamin C face serum skincare", "Aqua")
    assert result["application_area"]["value"] == "Face"
    assert result["absorption_profile"]["value"] == "Fast-absorbing"
    assert "fragrance_intelligence" not in result

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


def test_confirmed_source_value_is_not_replaced_by_ai(db: Session):
    brand = Brand(id=uuid.uuid4(), name="Source Brand", normalized_name="sourcebrand")
    db.add(brand)
    db.flush()
    product = CanonicalProduct(
        id=uuid.uuid4(),
        brand_id=brand.id,
        product_name="Observed Cream",
        normalized_name="observedcream",
    )
    db.add(product)
    db.flush()
    observed = FieldValue(
        id=uuid.uuid4(),
        canonical_product_id=product.id,
        field_name="skin_types",
        value=["dry skin"],
        source_type="source_data",
        source_reference="https://retail-data.invalid/products/123",
        confidence_score=1,
        review_status="confirmed",
        is_current=True,
    )
    db.add(observed)
    db.commit()

    create_field_value_version(
        db=db,
        canonical_product_id=product.id,
        product_variant_id=None,
        field_name="skin_types",
        value=["oily skin"],
        source_type="ai_inference",
        source_ref="test_run",
        confidence=0.9,
        status="inferred",
    )
    db.commit()
    db.refresh(observed)

    assert observed.is_current is True
    candidate = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product.id,
        FieldValue.source_type == "ai_inference",
    ).one()
    assert candidate.is_current is False

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
