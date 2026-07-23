import pytest
from sqlalchemy.orm import Session
import uuid
import json
import csv
import io
from app.routes.exports import build_business_export_data, build_audit_export_data
from app.models import CanonicalProduct, ProductVariant, Brand, FieldValue, ValidationIssue

def test_business_export_rules(db: Session):
    brand = Brand(id=uuid.uuid4(), name="Vichy", normalized_name="vichy")
    db.add(brand)
    db.flush()

    # Seed approved product
    prod = CanonicalProduct(
        id=uuid.uuid4(), 
        brand_id=brand.id, 
        product_name="Mineral 89", 
        normalized_name="mineral89",
        review_status="approved"
    )
    db.add(prod)
    db.flush()

    # Seed current value (is_current = True)
    fv = FieldValue(
        id=uuid.uuid4(),
        canonical_product_id=prod.id,
        field_name="vegan",
        value="yes",
        source_type="human_edit",
        review_status="confirmed",
        is_current=True
    )
    db.add(fv)
    db.commit()

    # Business export must fetch approved only
    data = build_business_export_data(db, include_inferred=False)
    assert len(data) == 1
    assert data[0]["product_name"] == "Mineral 89"
    assert data[0]["vegan"] == "yes"

def test_audit_export_rules(db: Session):
    brand = Brand(id=uuid.uuid4(), name="Avene", normalized_name="avene")
    db.add(brand)
    db.flush()

    # Seed imported (unapproved) product
    prod = CanonicalProduct(
        id=uuid.uuid4(), 
        brand_id=brand.id, 
        product_name="Thermal Water", 
        normalized_name="thermalwater",
        review_status="imported"
    )
    db.add(prod)
    db.flush()

    # Seed issue
    issue = ValidationIssue(
        id=uuid.uuid4(),
        canonical_product_id=prod.id,
        severity="warning",
        issue_type="missing_ean",
        message="Barcode missing.",
        created_by_type="system"
    )
    db.add(issue)
    db.commit()

    # Business export must exclude it
    biz_data = build_business_export_data(db, include_inferred=False)
    assert len(biz_data) == 0

    # Audit export must include it
    audit_data = build_audit_export_data(db)
    assert len(audit_data) == 1
    assert audit_data[0]["product_name"] == "Thermal Water"
    assert "[warning] Barcode missing." in audit_data[0]["validation_issues"]


def test_export_endpoint_validates_options_and_requires_download_auth(client):
    token_response = client.post(
        "/api/auth/token",
        data={"username": "admin@test.com", "password": "securepassword123"},
    )
    headers = {"Authorization": f"Bearer {token_response.json()['access_token']}"}

    invalid = client.post(
        "/api/exports/run",
        json={"export_mode": "wrong", "file_format": "pdf"},
        headers=headers,
    )
    assert invalid.status_code == 422
    assert client.get("/api/exports/download?mode=audit&format=json").status_code == 401

    response = client.post(
        "/api/exports/run",
        json={"export_mode": "audit", "file_format": "json"},
        headers=headers,
    )
    assert response.status_code == 200
    assert isinstance(response.json()["row_count"], int)


def test_csv_export_uses_standard_csv_quoting(client, db):
    token_response = client.post(
        "/api/auth/token",
        data={"username": "admin@test.com", "password": "securepassword123"},
    )
    headers = {"Authorization": f"Bearer {token_response.json()['access_token']}"}
    brand = Brand(id=uuid.uuid4(), name="CSV Brand", normalized_name="csvbrand")
    db.add(brand)
    db.flush()
    db.add(CanonicalProduct(
        id=uuid.uuid4(),
        brand_id=brand.id,
        product_name="One; Two",
        normalized_name="onetwo",
        review_status="approved",
    ))
    db.commit()

    response = client.get("/api/exports/download?mode=business&format=csv", headers=headers)
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text), delimiter=";"))
    assert any(row["product_name"] == "One; Two" for row in rows)
