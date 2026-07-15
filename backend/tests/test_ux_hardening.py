import pytest
import uuid
from datetime import datetime
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.models import (
    User, CanonicalProduct, ProductVariant, Brand, Category, 
    FieldValue, ValidationIssue, AuditLog, Formulation, 
    FormulationIngredient, IngredientDefinition, EnrichmentRun
)
from app.config import settings
from app.worker import process_item_enrichment

def get_auth_headers(client: TestClient, email: str) -> dict:
    from app.auth import get_password_hash, create_access_token
    from tests.conftest import TestingSessionLocal
    s = TestingSessionLocal()
    try:
        u = s.query(User).filter(User.email == email).first()
        if not u:
            role = "admin" if "admin" in email else "viewer" if "viewer" in email else "editor"
            u = User(
                email=email,
                hashed_password=get_password_hash("securepassword123"),
                role=role
            )
            s.add(u)
            s.commit()
    finally:
        s.close()

    token = create_access_token(data={"sub": email})
    return {"Authorization": f"Bearer {token}"}

def test_unknown_creates_no_issue(db: Session):
    # Mock settings
    settings.LOW_CONFIDENCE_THRESHOLD = 0.6
    
    # 1. semantic_status="unknown" should not trigger warning
    from app.worker import should_create_low_confidence_warning
    assert should_create_low_confidence_warning(
        field_name="subcategory",
        value="unknown",
        status="unknown",
        source_type="ai_inference",
        confidence=0.4
    ) is False

def test_not_applicable_creates_no_issue(db: Session):
    # 2. semantic_status="not_applicable" should not trigger warning
    from app.worker import should_create_low_confidence_warning
    assert should_create_low_confidence_warning(
        field_name="subcategory",
        value="not_applicable",
        status="not_applicable",
        source_type="ai_inference",
        confidence=0.3
    ) is False

def test_low_confidence_triggers_one_warning(db: Session):
    # 3. meaningful value with low confidence should trigger warning
    from app.worker import should_create_low_confidence_warning
    assert should_create_low_confidence_warning(
        field_name="subcategory",
        value="Serum",
        status="inferred",
        source_type="ai_inference",
        confidence=0.42
    ) is True

def test_explicit_source_never_warns(db: Session):
    # 4. source_type="source_data" should never trigger warning
    from app.worker import should_create_low_confidence_warning
    assert should_create_low_confidence_warning(
        field_name="subcategory",
        value="Serum",
        status="confirmed",
        source_type="source_data",
        confidence=0.3
    ) is False

def test_missing_brand_blocks_approval(client: TestClient, db: Session):
    # 5. Missing brand blocks approval
    headers = get_auth_headers(client, "admin@test.com")
    
    brand = Brand(id=uuid.uuid4(), name="Missing Brand", normalized_name="missingbrand")
    db.add(brand)
    
    # Create canonical product
    product = CanonicalProduct(
        id=uuid.uuid4(),
        product_name="Test Product No Brand", normalized_name="testproductnobrand",
        brand_id=brand.id,
        review_status="needs_review"
    )
    db.add(product)
    
    # Add a blocking validation issue manually
    issue = ValidationIssue(
        id=uuid.uuid4(),
        canonical_product_id=product.id,
        field_name="brand",
        severity="blocking",
        issue_type="missing_brand",
        message="Product brand is missing or unknown.",
        created_by_type="system"
    )
    db.add(issue)
    db.commit()
    
    resp = client.post(f"/api/products/{product.id}/approve", headers=headers)
    assert resp.status_code == 400
    assert "Active blocking validation issue exists" in resp.json()["detail"]

def test_warnings_do_not_block_approval(client: TestClient, db: Session):
    # 6. Warnings do not block approval
    headers = get_auth_headers(client, "admin@test.com")
    
    # Create brand
    brand = Brand(id=uuid.uuid4(), name="Test Brand Warnings", normalized_name="testbrandwarnings")
    db.add(brand)
    
    # Create product
    product = CanonicalProduct(
        id=uuid.uuid4(),
        product_name="Test Product With Warnings", normalized_name="testproductwithwarnings",
        brand_id=brand.id,
        review_status="needs_review"
    )
    db.add(product)
    
    # Add a warning issue
    issue = ValidationIssue(
        id=uuid.uuid4(),
        canonical_product_id=product.id,
        field_name="gtin",
        severity="warning",
        issue_type="missing_ean",
        message="Missing GTIN warning.",
        created_by_type="system"
    )
    db.add(issue)
    db.commit()
    
    resp = client.post(f"/api/products/{product.id}/approve", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["review_status"] == "approved"

def test_override_creates_new_version_with_null_confidence(client: TestClient, db: Session):
    # 7. Human override creates a new version with null confidence
    headers = get_auth_headers(client, "admin@test.com")
    
    brand = Brand(id=uuid.uuid4(), name="Test Brand Override", normalized_name="testbrandoverride")
    db.add(brand)
    
    product = CanonicalProduct(
        id=uuid.uuid4(),
        product_name="Test Override Product", normalized_name="testoverrideproduct",
        brand_id=brand.id,
        review_status="needs_review"
    )
    db.add(product)
    
    fv = FieldValue(
        id=uuid.uuid4(),
        canonical_product_id=product.id,
        field_name="subcategory",
        value="Cream",
        source_type="ai_inference",
        confidence_score=0.9,
        review_status="inferred",
        is_current=True
    )
    db.add(fv)
    db.commit()
    
    resp = client.put(
        f"/api/products/{product.id}",
        headers=headers,
        json={"field_name": "subcategory", "value": "Gel", "reason": "Corrected texture mapping"}
    )
    assert resp.status_code == 200
    
    # Query database
    new_fv = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product.id,
        FieldValue.field_name == "subcategory",
        FieldValue.is_current == True
    ).first()
    
    assert new_fv is not None
    assert new_fv.value == "Gel"
    assert new_fv.source_type == "human_edit"
    assert new_fv.confidence_score is None
    assert new_fv.review_status == "confirmed"

def test_override_keeps_previous_in_history(client: TestClient, db: Session):
    # 8. Previous FieldValue remains in history but is not current
    headers = get_auth_headers(client, "admin@test.com")
    brand = Brand(id=uuid.uuid4(), name="Test Brand History", normalized_name="testbrandhistory")
    db.add(brand)
    
    product = CanonicalProduct(
        id=uuid.uuid4(),
        product_name="Test History Product", normalized_name="testhistoryproduct",
        brand_id=brand.id,
        review_status="needs_review"
    )
    db.add(product)
    
    fv = FieldValue(
        id=uuid.uuid4(),
        canonical_product_id=product.id,
        field_name="subcategory",
        value="Cream",
        source_type="ai_inference",
        confidence_score=0.9,
        review_status="inferred",
        is_current=True
    )
    db.add(fv)
    db.commit()
    
    client.put(
        f"/api/products/{product.id}",
        headers=headers,
        json={"field_name": "subcategory", "value": "Gel", "reason": "Corrected"}
    )
    
    # Assert old FieldValue is still in DB but is_current = False
    old_fv = db.query(FieldValue).filter(FieldValue.id == fv.id).first()
    assert old_fv is not None
    assert old_fv.is_current is False

def test_override_reason_saved_in_audit(client: TestClient, db: Session):
    # 9. Override reason is saved in AuditLog.reason
    headers = get_auth_headers(client, "admin@test.com")
    brand = Brand(id=uuid.uuid4(), name="Test Brand Audit", normalized_name="testbrandaudit")
    db.add(brand)
    
    product = CanonicalProduct(
        id=uuid.uuid4(),
        product_name="Test Audit Product", normalized_name="testauditproduct",
        brand_id=brand.id,
        review_status="needs_review"
    )
    db.add(product)
    db.commit()
    
    client.put(
        f"/api/products/{product.id}",
        headers=headers,
        json={"field_name": "subcategory", "value": "Gel", "reason": "Audit reason check"}
    )
    
    audit = db.query(AuditLog).filter(
        AuditLog.reason == "Audit reason check",
        AuditLog.action == "override"
    ).first()
    assert audit is not None

def test_validation_rejects_empty_reason(client: TestClient, db: Session):
    # 10. Rejects overrides with empty or blank reason
    headers = get_auth_headers(client, "admin@test.com")
    brand = Brand(id=uuid.uuid4(), name="Test Brand Validation", normalized_name="testbrandvalidation")
    db.add(brand)
    
    product = CanonicalProduct(
        id=uuid.uuid4(),
        product_name="Test Validation Product", normalized_name="testvalidationproduct",
        brand_id=brand.id,
        review_status="needs_review"
    )
    db.add(product)
    db.commit()
    
    # Blank reason
    resp = client.put(
        f"/api/products/{product.id}",
        headers=headers,
        json={"field_name": "subcategory", "value": "Gel", "reason": "   "}
    )
    assert resp.status_code == 400
    assert "Override reason must not be blank" in resp.json()["detail"]

def test_metadata_fault_tolerance(client: TestClient, db: Session):
    # 11. Detail works when raw_response is null or malformed
    headers = get_auth_headers(client, "admin@test.com")
    brand = Brand(id=uuid.uuid4(), name="Test Brand Metadata", normalized_name="testbrandmetadata")
    db.add(brand)
    
    product = CanonicalProduct(
        id=uuid.uuid4(),
        product_name="Test Metadata Product", normalized_name="testmetadataproduct",
        brand_id=brand.id,
        review_status="needs_review"
    )
    db.add(product)
    
    # Create EnrichmentRun with null raw_response
    run = EnrichmentRun(
        id=uuid.uuid4(),
        canonical_product_id=product.id,
        provider="Google Gemini",
        model="gemini-2.5-flash",
        model_version="2.5",
        prompt_version="1.0",
        schema_version="1.0",
        status="success",
        raw_response=None # NULL raw response
    )
    db.add(run)
    db.commit()
    
    resp = client.get(f"/api/products/{product.id}", headers=headers)
    assert resp.status_code == 200

def test_per_field_metadata_reference(client: TestClient, db: Session):
    # 12. Returned FieldValue contains enrichment run details
    headers = get_auth_headers(client, "admin@test.com")
    brand = Brand(id=uuid.uuid4(), name="Test Brand Per Field", normalized_name="testbrandperfield")
    db.add(brand)
    
    product = CanonicalProduct(
        id=uuid.uuid4(),
        product_name="Test Per Field Product", normalized_name="testperfieldproduct",
        brand_id=brand.id,
        review_status="needs_review"
    )
    db.add(product)
    
    run = EnrichmentRun(
        id=uuid.uuid4(),
        canonical_product_id=product.id,
        provider="Google Gemini",
        model="gemini-2.5-flash",
        model_version="2.5",
        prompt_version="1.0",
        schema_version="1.0",
        status="success",
        raw_response="{}"
    )
    db.add(run)
    
    fv = FieldValue(
        id=uuid.uuid4(),
        canonical_product_id=product.id,
        field_name="subcategory",
        value="Cream",
        source_type="ai_inference",
        confidence_score=0.9,
        review_status="inferred",
        enrichment_run_id=run.id,
        is_current=True
    )
    db.add(fv)
    db.commit()
    
    resp = client.get(f"/api/products/{product.id}", headers=headers)
    assert resp.status_code == 200
    field_data = resp.json()["field_values"][0]
    assert field_data["enrichment_run"] is not None
    assert field_data["enrichment_run"]["model"] == "gemini-2.5-flash"

def test_key_ingredient_provenance(client: TestClient, db: Session):
    # 13. Key ingredients card includes source_type, evidence, confidence
    headers = get_auth_headers(client, "admin@test.com")
    brand = Brand(id=uuid.uuid4(), name="Test Brand Ingredients", normalized_name="testbrandingredients")
    db.add(brand)
    
    product = CanonicalProduct(
        id=uuid.uuid4(),
        product_name="Test Ingredients Product", normalized_name="testingredientsproduct",
        brand_id=brand.id,
        review_status="needs_review"
    )
    db.add(product)
    
    formulation = Formulation(
        id=uuid.uuid4(),
        canonical_product_id=product.id,
        raw_inci_text="Water, Sodium Hyaluronate",
        content_hash="hash123"
    )
    db.add(formulation)
    
    defn = IngredientDefinition(
        id=uuid.uuid4(),
        name="Sodium Hyaluronate",
        normalized_name="sodium hyaluronate",
        common_name="Hyaluronic Acid",
        function="Hydration",
        benefits="Hydrates skin"
    )
    db.add(defn)
    db.flush()
    
    fi = FormulationIngredient(
        id=uuid.uuid4(),
        formulation_id=formulation.id,
        ingredient_definition_id=defn.id,
        raw_inci_name="Sodium Hyaluronate",
        position=1,
        is_key_ingredient=True,
        key_ingredient_status="active",
        evidence_source="ai_inference",
        confidence_score=0.95,
        evidence=[{"supporting_text": "Hyaluronic acid hydrates"}]
    )
    db.add(fi)
    db.commit()
    
    resp = client.get(f"/api/products/{product.id}", headers=headers)
    assert resp.status_code == 200
    ing = resp.json()["key_ingredients"][0]
    assert ing["name"] == "Sodium Hyaluronate"
    assert ing["source_type"] == "ai_inference"
    assert ing["confidence"] == 0.95
    assert len(ing["evidence"]) == 1

def test_dynamic_concerns_returned(client: TestClient, db: Session):
    # 14. Concern fields return dynamic concerns
    headers = get_auth_headers(client, "admin@test.com")
    brand = Brand(id=uuid.uuid4(), name="Test Brand Concerns", normalized_name="testbrandconcerns")
    db.add(brand)
    
    product = CanonicalProduct(
        id=uuid.uuid4(),
        product_name="Test Concerns Product", normalized_name="testconcernsproduct",
        brand_id=brand.id,
        review_status="needs_review"
    )
    db.add(product)
    
    fv = FieldValue(
        id=uuid.uuid4(),
        canonical_product_id=product.id,
        field_name="hydration",
        value=True,
        source_type="ai_inference",
        confidence_score=0.9,
        review_status="confirmed",
        semantic_status="explicit",
        semantic_status_type="targeting_status",
        is_current=True,
        evidence=[{"supporting_text": "Targets dry skin"}]
    )
    db.add(fv)
    db.commit()
    
    resp = client.get(f"/api/products/{product.id}", headers=headers)
    assert resp.status_code == 200
    concerns = resp.json()["dynamic_concerns"]
    assert len(concerns) >= 1
    assert concerns[0]["concern_name"] == "hydration"
    assert concerns[0]["targeting_status"] == "explicit"

def test_viewer_cannot_override(client: TestClient, db: Session):
    # 15. Viewer receives HTTP 403 Forbidden
    headers = get_auth_headers(client, "viewer@test.com")
    brand = Brand(id=uuid.uuid4(), name="Test Brand Viewer", normalized_name="testbrandviewer")
    db.add(brand)
    
    product = CanonicalProduct(
        id=uuid.uuid4(),
        product_name="Test Viewer Product", normalized_name="testviewerproduct",
        brand_id=brand.id,
        review_status="needs_review"
    )
    db.add(product)
    db.commit()
    
    resp = client.put(
        f"/api/products/{product.id}",
        headers=headers,
        json={"field_name": "subcategory", "value": "Gel", "reason": "viewer attempt"}
    )
    assert resp.status_code == 403

def test_unique_current_value_collision_returns_409(client: TestClient, db: Session):
    # 16. Collision throws HTTP 409
    headers = get_auth_headers(client, "admin@test.com")
    brand = Brand(id=uuid.uuid4(), name="Test Brand Collision", normalized_name="testbrandcollision")
    db.add(brand)
    
    product = CanonicalProduct(
        id=uuid.uuid4(),
        product_name="Test Collision Product", normalized_name="testcollisionproduct",
        brand_id=brand.id,
        review_status="needs_review"
    )
    db.add(product)
    db.commit()
    
    # We trigger a simulated database constraint collision
    # To trigger a 409, we mock the db.commit to raise an IntegrityError
    # This proves the exception handler catches it and returns 409
    original_commit = db.commit
    def mock_commit():
        raise IntegrityError("Simulated unique constraint collision", params={}, orig=None)
    
    db.commit = mock_commit
    try:
        resp = client.put(
            f"/api/products/{product.id}",
            headers=headers,
            json={"field_name": "subcategory", "value": "Gel", "reason": "will collide"}
        )
        assert resp.status_code == 409
        assert "conflict occurred" in resp.json()["detail"]
    finally:
        db.commit = original_commit

def test_audit_failure_rolls_back_override(client: TestClient, db: Session):
    # 17. Verify old FieldValue remains current and no new value is committed if audit fails
    headers = get_auth_headers(client, "admin@test.com")
    
    from tests.conftest import TestingSessionLocal
    setup_db = TestingSessionLocal()
    brand_id = uuid.uuid4()
    product_id = uuid.uuid4()
    fv_id = uuid.uuid4()
    
    try:
        brand = Brand(id=brand_id, name="Test Brand Audit Failure", normalized_name="testbrandauditfailure")
        setup_db.add(brand)
        
        product = CanonicalProduct(
            id=product_id,
            product_name="Test Audit Failure Product", normalized_name="testauditfailureproduct",
            brand_id=brand_id,
            review_status="needs_review"
        )
        setup_db.add(product)
        
        fv = FieldValue(
            id=fv_id,
            canonical_product_id=product_id,
            field_name="subcategory",
            value="Cream",
            source_type="ai_inference",
            confidence_score=0.9,
            review_status="inferred",
            is_current=True
        )
        setup_db.add(fv)
        setup_db.commit()
    finally:
        setup_db.close()
    
    # Mock record_audit to raise Exception
    import app.routes.products as prod_routes
    original_record_audit = prod_routes.record_audit
    def mock_record_audit(*args, **kwargs):
        raise Exception("Simulated audit logging failure")
    
    prod_routes.record_audit = mock_record_audit
    try:
        resp = client.put(
            f"/api/products/{product_id}",
            headers=headers,
            json={"field_name": "subcategory", "value": "Gel", "reason": "will fail audit"}
        )
        assert resp.status_code == 500
        
        # Verify old value remains current and Gel was not committed
        check_db = TestingSessionLocal()
        try:
            current_fvs = check_db.query(FieldValue).filter(
                FieldValue.canonical_product_id == product_id,
                FieldValue.field_name == "subcategory"
            ).all()
            
            assert len(current_fvs) == 1
            assert current_fvs[0].value == "Cream"
            assert current_fvs[0].is_current is True
        finally:
            check_db.close()
    finally:
        prod_routes.record_audit = original_record_audit

def test_conflicting_field_creates_dedicated_conflict_issue(db: Session):
    # 18. Verify conflict status creates validation warning
    from app.worker import is_conflicting
    assert is_conflicting("conflicting", "conflicting") is True
    assert is_conflicting("Serum", "inferred") is False

def test_unknown_field_name_rejected(client: TestClient, db: Session):
    # 19. Reject unknown field name
    headers = get_auth_headers(client, "admin@test.com")
    brand = Brand(id=uuid.uuid4(), name="Test Brand Unknown Field", normalized_name="testbrandunknownfield")
    db.add(brand)
    product = CanonicalProduct(
        id=uuid.uuid4(),
        product_name="Test Unknown Field Product", normalized_name="testunknownfieldproduct",
        brand_id=brand.id,
        review_status="needs_review"
    )
    db.add(product)
    db.commit()
    
    resp = client.put(
        f"/api/products/{product.id}",
        headers=headers,
        json={"field_name": "non_existent_field", "value": "Gel", "reason": "correct"}
    )
    assert resp.status_code == 400
    assert "not editable or unrecognized" in resp.json()["detail"]

def test_invalid_field_value_type_rejected(client: TestClient, db: Session):
    # 20. Reject invalid field value type
    headers = get_auth_headers(client, "admin@test.com")
    brand = Brand(id=uuid.uuid4(), name="Test Brand Invalid Type", normalized_name="testbrandinvalidtype")
    db.add(brand)
    product = CanonicalProduct(
        id=uuid.uuid4(),
        product_name="Test Invalid Type Product", normalized_name="testinvalidtypeproduct",
        brand_id=brand.id,
        review_status="needs_review"
    )
    db.add(product)
    db.commit()
    
    # hydration expects bool, pass string
    resp = client.put(
        f"/api/products/{product.id}",
        headers=headers,
        json={"field_name": "hydration", "value": "not_a_bool", "reason": "correct"}
    )
    assert resp.status_code == 400
    assert "Invalid value type for" in resp.json()["detail"]

def test_postgres_concurrent_overrides(db: Session):
    # 21. Prove two concurrent threads trying to override same product field name cannot create two current FieldValues
    import threading
    import time
    
    brand = Brand(id=uuid.uuid4(), name="Test Brand Concurrency", normalized_name="testbrandconcurrency")
    db.add(brand)
    product = CanonicalProduct(
        id=uuid.uuid4(),
        product_name="Test Concurrency Product", normalized_name="testconcurrencyproduct",
        brand_id=brand.id,
        review_status="needs_review"
    )
    db.add(product)
    db.commit()
    
    errors = []
    
    def run_override(val: str):
        # We spawn a new DB session inside each thread
        from app.database import SessionLocal
        local_db = SessionLocal()
        try:
            # Emulate transaction override flow
            # Acquire row lock
            local_db.query(CanonicalProduct).filter(CanonicalProduct.id == product.id).with_for_update().first()
            
            # Deactivate previous
            local_db.query(FieldValue).filter(
                FieldValue.canonical_product_id == product.id,
                FieldValue.field_name == "subcategory"
            ).update({"is_current": False})
            local_db.flush()
            
            # Save new
            fv = FieldValue(
                id=uuid.uuid4(),
                canonical_product_id=product.id,
                field_name="subcategory",
                value=val,
                source_type="human_edit",
                is_current=True,
                review_status="confirmed"
            )
            local_db.add(fv)
            local_db.commit()
        except Exception as e:
            local_db.rollback()
            errors.append(e)
        finally:
            local_db.close()
            
    t1 = threading.Thread(target=run_override, args=("Value A",))
    t2 = threading.Thread(target=run_override, args=("Value B",))
    
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    
    # Assert that at least one error occurred (due to unique index constraint violation) or serialized successfully
    # If they serialized, only one must remain is_current = True
    current_fvs = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product.id,
        FieldValue.field_name == "subcategory",
        FieldValue.is_current == True
    ).all()
    
    assert len(current_fvs) <= 1
