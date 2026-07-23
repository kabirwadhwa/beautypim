import pytest
from fastapi.testclient import TestClient
import json
import uuid
from app.models import CanonicalProduct, Brand, FieldValue, ImportJob, User, ProductVariant, ValidationIssue, Category

def test_database_dialect_matches_environment(db):
    dialect_name = db.bind.dialect.name
    print(f"DIALECT_NAME: {dialect_name}")
    assert dialect_name in ["sqlite", "postgresql"]

def get_admin_token(client: TestClient) -> str:
    resp = client.post(
        "/api/auth/token",
        data={"username": "admin@test.com", "password": "securepassword123"}
    )
    return resp.json()["access_token"]

def test_get_templates_list(client: TestClient):
    token = get_admin_token(client)
    resp = client.get("/api/feeds/templates", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)

def test_upload_file_invalid_format(client: TestClient):
    token = get_admin_token(client)
    files = {"file": ("test.txt", b"invalid text", "text/plain")}
    resp = client.post(
        "/api/feeds/upload",
        files=files,
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 400

def test_list_products_api(client: TestClient, db):
    token = get_admin_token(client)
    
    # Seed a product
    brand = Brand(id=uuid.uuid4(), name="Drunk Elephant", normalized_name="drunkelephant")
    db.add(brand)
    db.flush()
    prod = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Lala Retro", normalized_name="lalaretro")
    db.add(prod)
    db.commit()

    resp = client.get(
        "/api/products",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["product_name"] == "Lala Retro"
    assert data[0]["internal_code"].startswith("ICN-")


def test_product_grid_search_and_filters_include_gtin_icn_and_variant_issues(client: TestClient, db):
    token = get_admin_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    brand = Brand(id=uuid.uuid4(), name="Grid Test", normalized_name="gridtest")
    db.add(brand)
    db.flush()
    product = CanonicalProduct(
        id=uuid.uuid4(),
        brand_id=brand.id,
        product_name="Searchable Serum",
        normalized_name="searchableserum",
        review_status="needs_review",
    )
    db.add(product)
    db.flush()
    variant = ProductVariant(
        id=uuid.uuid4(),
        canonical_product_id=product.id,
        gtin="1234567890123",
    )
    db.add(variant)
    db.flush()
    db.add(ValidationIssue(
        id=uuid.uuid4(),
        product_variant_id=variant.id,
        severity="warning",
        issue_type="test_issue",
        message="Variant needs attention.",
        created_by_type="system",
    ))
    db.commit()

    by_gtin = client.get("/api/products?search=1234567890123", headers=headers)
    assert by_gtin.status_code == 200
    assert [row["id"] for row in by_gtin.json()] == [str(product.id)]
    assert by_gtin.json()[0]["validation_issue_count"] == 1

    internal_code = by_gtin.json()[0]["internal_code"]
    by_icn = client.get(f"/api/products?search={internal_code}", headers=headers)
    assert [row["id"] for row in by_icn.json()] == [str(product.id)]

    with_issues = client.get("/api/products?issue_filter=true", headers=headers)
    assert str(product.id) in [row["id"] for row in with_issues.json()]
    clear = client.get("/api/products?issue_filter=false", headers=headers)
    assert str(product.id) not in [row["id"] for row in clear.json()]
    status_filtered = client.get("/api/products?status_filter=needs_review", headers=headers)
    assert str(product.id) in [row["id"] for row in status_filtered.json()]


def test_taxonomy_crud_and_guards(client: TestClient, db):
    token = get_admin_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    root = client.post("/api/settings/categories", json={"name": "Skincare"}, headers=headers)
    assert root.status_code == 201, root.text
    root_id = root.json()["id"]
    child = client.post(
        "/api/settings/categories",
        json={"name": "Serums", "parent_id": root_id},
        headers=headers,
    )
    assert child.status_code == 201
    assert child.json()["path"] == "Skincare > Serums"

    renamed = client.put(
        f"/api/settings/categories/{root_id}",
        json={"name": "Face Care"},
        headers=headers,
    )
    assert renamed.status_code == 200
    categories = client.get("/api/settings/categories", headers=headers).json()
    assert any(category["path"] == "Face Care > Serums" for category in categories)

    blocked = client.delete(f"/api/settings/categories/{root_id}", headers=headers)
    assert blocked.status_code == 409
    child_id = child.json()["id"]
    assert client.delete(f"/api/settings/categories/{child_id}", headers=headers).status_code == 204
    assert client.delete(f"/api/settings/categories/{root_id}", headers=headers).status_code == 204

def test_edit_product_api(client: TestClient, db):
    token = get_admin_token(client)

    # Seed
    brand = Brand(id=uuid.uuid4(), name="Paula's Choice", normalized_name="paulaschoice")
    db.add(brand)
    db.flush()
    prod = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="BHA Exfoliant", normalized_name="bhaexfoliant")
    db.add(prod)
    db.commit()

    # Edit
    edit_payload = {
        "field_name": "vegan",
        "value": "yes",
        "reason": "Verified brand certification page"
    }
    resp = client.put(
        f"/api/products/{prod.id}",
        json=edit_payload,
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    
    # Verify new human edit is current
    fvs = db.query(FieldValue).filter(FieldValue.canonical_product_id == prod.id).all()
    assert len(fvs) == 1
    assert fvs[0].is_current == True
    assert fvs[0].value == "yes"
    assert fvs[0].source_type == "human_edit"

def test_run_export_api(client: TestClient):
    token = get_admin_token(client)
    payload = {
        "export_mode": "business",
        "file_format": "json",
        "include_inferred": True
    }
    resp = client.post(
        "/api/exports/run",
        json=payload,
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert "download_url" in resp.json()

def test_upload_file_valid_csv(client: TestClient):
    token = get_admin_token(client)
    csv_data = b"product_name,brand,ean,price,description\nDaily Cleanser,Cerave,3337875597198,12.50,Hydrating face lotion"
    files = {"file": ("products.csv", csv_data, "text/csv")}
    resp = client.post(
        "/api/feeds/upload",
        files=files,
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["filename"] == "products.csv"
    assert "headers" in data
    assert "suggested_mapping" in data
    assert data["total_rows"] == 1

def test_atomic_upload_and_process_flow(client: TestClient, db):
    token = get_admin_token(client)
    csv_data = b"product_name,brand,ean,price,description,ingredients,size\nDaily Cleanser,Cerave,03337875597198,12.50,Hydrating vegan face cleanser,Aqua; Glycerin,236ml"
    preview = client.post(
        "/api/feeds/upload",
        files={"file": ("products.csv", csv_data, "text/csv")},
        headers={"Authorization": f"Bearer {token}"}
    ).json()
    request_json = json.dumps({
        "filename": "products.csv",
        "file_hash": preview["file_hash"],
        "column_mapping": preview["suggested_mapping"]
    })
    resp = client.post(
        "/api/feeds/process-upload",
        files={"file": ("products.csv", csv_data, "text/csv")},
        data={"request_json": request_json},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    job = resp.json()
    assert job["total_rows"] == 1
    # Test fixtures use an isolated transaction that a fresh background-worker
    # connection cannot see, so execute the queued job in that same transaction.
    from app.worker import run_job_worker
    run_job_worker(db, uuid.UUID(job["id"]))
    product = db.query(CanonicalProduct).filter(CanonicalProduct.product_name == "Daily Cleanser").first()
    assert product is not None

def test_approve_product_api(client: TestClient, db):
    token = get_admin_token(client)

    # Seed
    brand = Brand(id=uuid.uuid4(), name="Glow Recipe", normalized_name="glowrecipe")
    db.add(brand)
    db.flush()
    prod = CanonicalProduct(
        id=uuid.uuid4(), 
        brand_id=brand.id, 
        product_name="Watermelon Glow Toner", 
        normalized_name="watermelonglowtoner",
        review_status="imported"
    )
    db.add(prod)
    db.commit()

    resp = client.post(
        f"/api/products/{prod.id}/approve",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["review_status"] == "approved"

def test_health_and_readiness_api(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}

    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}

def test_export_download_json(client: TestClient, db):
    token = get_admin_token(client)
    # Seed approved product
    brand = Brand(id=uuid.uuid4(), name="Bio-Oil", normalized_name="biooil")
    db.add(brand)
    db.flush()
    prod = CanonicalProduct(
        id=uuid.uuid4(), 
        brand_id=brand.id, 
        product_name="Skincare Oil", 
        normalized_name="skincareoil",
        review_status="approved"
    )
    db.add(prod)
    db.commit()

    resp = client.get(
        "/api/exports/download?mode=business&format=json",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert "beauty_pim_export_business.json" in resp.headers["Content-Disposition"]

def test_export_download_csv(client: TestClient):
    token = get_admin_token(client)
    resp = client.get(
        "/api/exports/download?mode=business&format=csv",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert "beauty_pim_export_business.csv" in resp.headers["Content-Disposition"]
