import pytest
from fastapi.testclient import TestClient
import json
import uuid
from app.models import CanonicalProduct, Brand, FieldValue, ImportJob, User

def get_admin_token(client: TestClient) -> str:
    resp = client.post(
        "/api/auth/token",
        data={"username": "admin@test.com", "password": "password123"}
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
