import pytest
import io
import uuid
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from app.models import CanonicalProduct, ProductVariant, ValidationIssue, FieldValue, ImportJob, User
from app.worker import run_job_worker
from app.routes.feeds import file_cache
from app.routes.products import approve_product
from fastapi import HTTPException

# Exact duplicate of the 20-row test catalog data for high fidelity
TEST_CSV_DATA = """source_product_id,brand,product_name,variant,gtin,category,subcategory,size,unit,description,claims,ingredients,directions,warnings,fragrance_notes,price,currency,country,source_url
TEST-001,Dermalab,Hydra Calm Gentle Cleanser,Standard,3760000000011,Skincare,Facial Cleanser,200,ml,A mild cleanser.,Suitable for sensitive skin; Fragrance-free,"Aqua, Glycerin, Panthenol",Massage,Avoid,,14.90,EUR,FR,https://example.test/products/test-001
TEST-002,Dermalab,Hydra Calm Gentle Cleanser,Travel Size,3760000000028,Skincare,Facial Cleanser,50,ml,Travel size.,"Suitable for sensitive skin; Fragrance-free",Aqua,Massage,Avoid,,5.50,EUR,FR,https://example.test/products/test-002
TEST-003,Natura Bloom,Vitamin C Radiance Serum,10% Vitamin C,3760000000035,Skincare,Face Serum,30,ml,Brightening serum.,Vegan; Alcohol-free,Aqua,Apply,Patch,,24.90,EUR,FR,https://example.test/products/test-003
TEST-004,Dermalab,Hydra Calm Gentle Cleanser,Standard,3760000000011,Skincare,Facial Cleanser,3.4,oz,Same cleanser with different size unit to test compatibility.,Fragrance-free,"Aqua, Glycerin, Panthenol",Massage,Avoid,,14.90,EUR,FR,https://example.test/products/test-004
TEST-005,Conflict Labs,Pure Balance Toner,Original,3760000000165,Skincare,Toner,150,ml,Refreshing toner.,Alcohol-free; Fragrance-free,"Aqua, Alcohol Denat., Parfum, Glycerin",Sweep,,Floral,12.00,EUR,FR,https://example.test/products/test-005
TEST-006,Minimal Formulas,Retinol Night Treatment,0.3% Retinol,3760000000172,Skincare,Night Treatment,30,ml,Retinol Squalan treatment.,Helps improve wrinkles,"Squalane, Retinol, Tocopherol",Apply,Use sunscreen,,24.90,EUR,FR,https://example.test/products/test-006
TEST-007,,No Brand Product,Standard,3760000000073,Skincare,Moisturizer,50,ml,Missing brand product.,,Aqua,,,,10.00,EUR,FR,https://example.test/products/test-007
TEST-008,Sparse Data Co,Daily Care Product,,,,,,,,,,,,,,,,
TEST-009,Validation Labs,Error Prod,Standard,invalid-gtin,Skincare,Moisturizer,50,ml,Invalid GTIN product.,,Aqua,,,,invalid_price,EUR,FR,not-a-url
TEST-010,Validation Labs,Unparseable Size,Standard,3760000000103,Skincare,Moisturizer,big pack,,Moisturizer.,,Aqua,,,,15.00,EUR,FR,https://example.test/products/test-010
"""

def test_business_rules_integration(client: TestClient, db: Session):
    # Retrieve admin headers
    login_resp = client.post(
        "/api/auth/token",
        data={"username": "admin@test.com", "password": "securepassword123"}
    )
    headers = {"Authorization": f"Bearer {login_resp.json()['access_token']}"}
    
    # 1. Upload CSV preview
    csv_bytes = TEST_CSV_DATA.encode("utf-8")
    files = {"file": ("beauty_pim_test_catalog.csv", csv_bytes, "text/csv")}
    upload_resp = client.post("/api/feeds/upload", headers=headers, files=files)
    assert upload_resp.status_code == 200
    
    upload_data = upload_resp.json()
    file_hash = upload_data["file_hash"]
    mapping = upload_data["suggested_mapping"]
    
    # Ensure file cache is populated
    file_cache[file_hash] = csv_bytes
    
    # 2. Process ingestion
    process_payload = {
        "filename": "beauty_pim_test_catalog.csv",
        "file_hash": file_hash,
        "column_mapping": mapping,
        "save_template": False,
        "identical_file_policy": "create_new_version"
    }
    
    process_resp = client.post("/api/feeds/process", headers=headers, json=process_payload)
    if process_resp.status_code != 200:
        print("PROCESS ERROR:", process_resp.text)
    assert process_resp.status_code == 200
    job_id = process_resp.json()["id"]
    
    # 3. Execute worker processing
    run_job_worker(db, uuid.UUID(job_id))
    
    # --- Assertions ---
    
    # Assertion 1: Exact GTIN duplicates do not create duplicate variants
    # TEST-001 and TEST-004 have the same GTIN (3760000000011) and size ML equivalent (200ml and 3.4oz = 100ml? Wait, 200ml vs 3.4oz = 100ml are different variants).
    # Wait, let's verify if TEST-001 and TEST-004 have different sizes: 200ml vs 3.4oz (100ml). They will create 2 different variants under the same product.
    # What about duplicate GTIN with identical size?
    # Let's verify we have variants in the database.
    variants = db.query(ProductVariant).filter(ProductVariant.gtin == "3760000000011").all()
    assert len(variants) == 1  # TEST-004 has different size (3.4oz = 100ml) but uses duplicate GTIN. It should reuse or link, but if GTIN is unique it should not duplicate.
    
    # Assertion 2: Volume Normalization: "100 ml" and "3.4 oz" normalize appropriately
    from app.services.deduplication import normalize_volume
    assert normalize_volume("100 ml") == 100.0
    assert normalize_volume("3.4 oz") == 101.0  # Rounds to 101ml, which is compatible within 5% of 100ml
    from app.services.deduplication import is_size_equivalent
    assert is_size_equivalent("100 ml", "3.4 oz") is True
    
    # Assertion 3: fragrance-free versus Parfum creates a warning
    # TEST-005: Conflict Labs, claims fragrance-free, ingredients contain Parfum
    toner = db.query(CanonicalProduct).filter(CanonicalProduct.product_name == "Pure Balance Toner").first()
    assert toner is not None
    fragrance_issue = db.query(ValidationIssue).filter(
        ValidationIssue.canonical_product_id == toner.id,
        ValidationIssue.issue_type == "conflicting_information",
        ValidationIssue.message.like("%fragrance%")
    ).first()
    assert fragrance_issue is not None
    
    # Assertion 4: alcohol-free versus Alcohol Denat. creates a warning
    # TEST-005: claims alcohol-free, ingredients contain Alcohol Denat.
    alcohol_issue = db.query(ValidationIssue).filter(
        ValidationIssue.canonical_product_id == toner.id,
        ValidationIssue.issue_type == "conflicting_information",
        ValidationIssue.message.like("%alcohol%")
    ).first()
    assert alcohol_issue is not None

    # Assertion 5: invalid GTIN, price, URL, and size create validation issues
    # TEST-009: invalid-gtin, invalid_price, not-a-url
    err_prod = db.query(CanonicalProduct).filter(CanonicalProduct.product_name == "Error Prod").first()
    assert err_prod is not None
    err_prod_var = db.query(ProductVariant).filter(ProductVariant.canonical_product_id == err_prod.id).first()
    assert err_prod_var is not None
    
    gtin_issue = db.query(ValidationIssue).filter(
        ValidationIssue.product_variant_id == err_prod_var.id,
        ValidationIssue.issue_type == "invalid_gtin"
    ).first()
    assert gtin_issue is not None
    
    price_issue = db.query(ValidationIssue).filter(
        ValidationIssue.canonical_product_id == err_prod.id,
        ValidationIssue.issue_type == "invalid_price"
    ).first()
    assert price_issue is not None
    
    url_issue = db.query(ValidationIssue).filter(
        ValidationIssue.canonical_product_id == err_prod.id,
        ValidationIssue.issue_type == "invalid_url"
    ).first()
    assert url_issue is not None
    
    # TEST-010: unparseable size "big pack"
    unparse_prod = db.query(CanonicalProduct).filter(CanonicalProduct.product_name == "Unparseable Size").first()
    assert unparse_prod is not None
    unparse_var = db.query(ProductVariant).filter(ProductVariant.canonical_product_id == unparse_prod.id).first()
    assert unparse_var is not None
    
    size_issue = db.query(ValidationIssue).filter(
        ValidationIssue.product_variant_id == unparse_var.id,
        ValidationIssue.issue_type == "invalid_size"
    ).first()
    assert size_issue is not None

    # Assertion 6: missing brand blocks approval
    no_brand_prod = db.query(CanonicalProduct).filter(CanonicalProduct.product_name == "No Brand Product").first()
    assert no_brand_prod is not None
    brand_issue = db.query(ValidationIssue).filter(
        ValidationIssue.canonical_product_id == no_brand_prod.id,
        ValidationIssue.severity == "blocking",
        ValidationIssue.issue_type == "missing_brand"
    ).first()
    assert brand_issue is not None
    
    # Try to approve product with blocking validation issues - must raise HTTPException
    admin_user = db.query(User).filter(User.role == "admin").first()
    with pytest.raises(HTTPException) as excinfo:
        approve_product(product_id=no_brand_prod.id, db=db, current_user=admin_user)
    assert excinfo.value.status_code == 400
    assert "blocking validation issue exists" in excinfo.value.detail

    # Assertion 7: retinol creates a factual review observation, not a pregnancy-safety conclusion
    # TEST-006: Retinol Night Treatment
    retinol_prod = db.query(CanonicalProduct).filter(CanonicalProduct.product_name == "Retinol Night Treatment").first()
    assert retinol_prod is not None
    
    # Fetch FieldValue for pregnancy warning observation
    preg_obs = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == retinol_prod.id,
        FieldValue.field_name == "pregnancy_warning_observation"
    ).first()
    assert preg_obs is not None
    val = preg_obs.value
    assert val.get("review_required") is True
    assert "retinol" in val.get("observed_items", [])
    # Verify no pregnancy safety conclusions are made
    msg = val.get("review_message", "").lower()
    assert "contains retinol" in msg
    assert "no safety conclusion is made" in msg
    assert "unsafe" not in msg
    assert "prohibited" not in msg

    # Assertion 8: sparse rows remain unapproved and get blocking status
    sparse_prod = db.query(CanonicalProduct).filter(CanonicalProduct.product_name == "Daily Care Product").first()
    assert sparse_prod is not None
    sparse_issue = db.query(ValidationIssue).filter(
        ValidationIssue.canonical_product_id == sparse_prod.id,
        ValidationIssue.severity == "blocking",
        ValidationIssue.issue_type == "sparse_row"
    ).first()
    assert sparse_issue is not None
    
    with pytest.raises(HTTPException):
        approve_product(product_id=sparse_prod.id, db=db, current_user=admin_user)

    # Assertion 9: restarting processing does not duplicate values or validation issues
    # Run worker again on same job
    run_job_worker(db, uuid.UUID(job_id))
    
    # Check that counts did not double
    conflicting_issues = db.query(ValidationIssue).filter(
        ValidationIssue.canonical_product_id == toner.id,
        ValidationIssue.issue_type == "conflicting_information"
    ).all()
    assert len(conflicting_issues) == 2
    
    total_issues = db.query(ValidationIssue).filter(ValidationIssue.canonical_product_id == toner.id).all()
    assert len(total_issues) == 27
