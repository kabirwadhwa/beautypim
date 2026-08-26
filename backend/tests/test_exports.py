import pytest
from sqlalchemy.orm import Session
import uuid
import json
import csv
import io
from app.routes.exports import build_business_export_data, build_audit_export_data
from app.models import CanonicalProduct, ProductVariant, Brand, FieldValue, ValidationIssue
from app.schemas import EDITABLE_FIELDS_REGISTRY, ProductDetailOut
from app.services.business_export import (
    BUSINESS_EXPORT_COLUMNS,
    CATEGORY_FIELD_EXPORT_MAP,
    FIELD_VALUE_EXPORT_COVERAGE,
    INTERNAL_FIELD_VALUE_FIELDS,
    INTERNAL_PRODUCT_DETAIL_FIELDS,
    PRODUCT_DETAIL_EXPORT_COVERAGE,
    build_business_row,
)
from app.services.category_completeness import CATEGORY_RULES

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
        field_name="claims",
        value=[{"name": "Vegan", "value": "Yes", "status": "verified"}],
        source_type="human_edit",
        review_status="confirmed",
        is_current=True
    )
    usp = FieldValue(
        id=uuid.uuid4(), canonical_product_id=prod.id, field_name="product_usp",
        value="Mineral-rich daily hydration USP", source_type="source_data",
        review_status="confirmed", is_current=True,
    )
    db.add_all([fv, usp])
    db.commit()

    # Business export must fetch approved only
    data = build_business_export_data(db, include_inferred=False)
    assert len(data) == 1
    assert data[0]["product_name"] == "Mineral 89"
    assert data[0]["claims"][0]["status"] == "verified"
    assert data[0]["product_usp"] == "Mineral-rich daily hydration USP"
    assert tuple(data[0]) == BUSINESS_EXPORT_COLUMNS
    assert data[0]["target_audience_profile_1"] == ""
    assert data[0]["fragrance_top_notes"] == "NOT_APPLICABLE"

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
    usp = FieldValue(
        id=uuid.uuid4(), canonical_product_id=prod.id, field_name="product_usp",
        value="Fine-mist mineral water", source_type="source_data",
        review_status="confirmed", is_current=True,
    )
    db.add_all([issue, usp])
    db.commit()

    # Business export must exclude it
    biz_data = build_business_export_data(db, include_inferred=False)
    assert len(biz_data) == 0

    # Audit export must include it
    audit_data = build_audit_export_data(db)
    assert len(audit_data) == 1
    assert audit_data[0]["product_name"] == "Thermal Water"
    assert "[warning] Barcode missing." in audit_data[0]["validation_issues"]
    assert any(
        item["field"] == "product_usp" and item["value"] == "Fine-mist mineral water"
        for item in json.loads(audit_data[0]["provenance_history"])
    )


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
    assert tuple(rows[0]) == BUSINESS_EXPORT_COLUMNS


def test_business_export_contract_covers_every_product_detail_field():
    """A new ProductDetail field must be mapped or deliberately internal."""
    detail_fields = set(ProductDetailOut.model_fields)
    classified = set(PRODUCT_DETAIL_EXPORT_COVERAGE) | INTERNAL_PRODUCT_DETAIL_FIELDS
    assert detail_fields - classified == set(), (
        "New Product Detail attributes require a Business Export mapping or an "
        f"explicit internal-only classification: {sorted(detail_fields - classified)}"
    )
    assert classified - detail_fields == set()
    for source_field, columns in PRODUCT_DETAIL_EXPORT_COVERAGE.items():
        assert columns <= set(BUSINESS_EXPORT_COLUMNS), source_field


def test_business_export_contract_covers_editable_and_category_attributes():
    """Prevents silent omissions when a business/category attribute is added."""
    covered_fields = FIELD_VALUE_EXPORT_COVERAGE | INTERNAL_FIELD_VALUE_FIELDS
    assert set(EDITABLE_FIELDS_REGISTRY) - covered_fields == set()
    for module, rules in CATEGORY_RULES.items():
        if module == "unknown":
            continue
        assert set(rules) == set(CATEGORY_FIELD_EXPORT_MAP[module]), module
        assert set(CATEGORY_FIELD_EXPORT_MAP[module].values()) <= set(BUSINESS_EXPORT_COLUMNS)


def test_business_export_has_stable_unique_columns():
    assert len(BUSINESS_EXPORT_COLUMNS) == len(set(BUSINESS_EXPORT_COLUMNS))
    assert len(BUSINESS_EXPORT_COLUMNS) >= 100


@pytest.mark.parametrize("module", ["skincare", "haircare", "makeup", "fragrance", "beauty_accessory"])
def test_each_category_module_is_projected_into_its_business_columns(db, module):
    values = {name: f"{module}-{name}" for name in CATEGORY_FIELD_EXPORT_MAP[module]}
    detail = {
        "id": str(uuid.uuid4()), "internal_code": "ICN-TEST", "product_name": "Export fixture",
        "brand_name": "Fixture", "review_status": "approved", "tags": [], "variant_count": 0,
        "variants": [], "formulations": [], "key_ingredients": [], "market_observations": [],
        "review_aggregate": {}, "product_understanding": {"category_module": module},
        "completeness": {"category_module": module}, "field_values": [{
            "field_name": module, "value": values, "source_type": "source_data",
            "review_status": "confirmed", "is_current": True, "updated_at": "2026-01-01",
        }, {
            "field_name": "directions", "value": "Use safely", "source_type": "source_data",
            "review_status": "confirmed", "is_current": True, "updated_at": "2026-01-01",
        }, {
            "field_name": "targeted_concerns", "value": ["Concern"], "source_type": "source_data",
            "review_status": "confirmed", "is_current": True, "updated_at": "2026-01-01",
        }],
    }
    row = build_business_row(db, detail, include_inferred=True)
    for source_field, column in CATEGORY_FIELD_EXPORT_MAP[module].items():
        expected = "Use safely" if source_field == "directions" else ["Concern"] if source_field == "targeted_concerns" else "" if source_field == "inci" else values[source_field]
        assert row[column] == expected, (module, source_field, column)
