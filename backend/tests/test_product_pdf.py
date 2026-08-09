import uuid
from io import BytesIO

import pytest

from app.auth import create_access_token
from app.models import Brand, CanonicalProduct, FieldValue, Formulation, ProductVariant, ImportJob, SourceListing
from app.services.image_urls import _assert_public_host, fetch_public_image, normalize_public_image_url
from app.services.product_pdf import build_product_pdf


def auth_headers(email: str = "admin@test.com") -> dict[str, str]:
    token = create_access_token(data={"sub": email})
    return {"Authorization": f"Bearer {token}"}


def make_product(db):
    brand = Brand(
        id=uuid.uuid4(),
        name="PDF Beauty Lab",
        normalized_name=f"pdfbeautylab{uuid.uuid4().hex}",
    )
    product = CanonicalProduct(
        id=uuid.uuid4(),
        brand_id=brand.id,
        product_name="Cloudberry Barrier Serum",
        normalized_name="cloudberrybarrierserum",
        review_status="approved",
    )
    variant = ProductVariant(
        id=uuid.uuid4(),
        canonical_product_id=product.id,
        variant_name="30 ml",
        gtin=f"9{str(product.id.int)[:12]}",
        size="30",
        unit="ml",
    )
    formulation = Formulation(
        id=uuid.uuid4(),
        canonical_product_id=product.id,
        raw_inci_text="Aqua, Glycerin, Cloudberry Extract, Ceramide NP",
        content_hash=uuid.uuid4().hex,
    )
    db.add_all([brand, product, variant, formulation])
    for name, value in (
        ("product_type", "Face Serum"),
        ("benefits", [{"statement": "Supports hydration and the skin barrier"}]),
        ("directions", "Apply two drops morning and evening."),
        ("vegan", "yes"),
    ):
        db.add(FieldValue(
            id=uuid.uuid4(),
            canonical_product_id=product.id,
            field_name=name,
            value=value,
            source_type="ai_inference",
            review_status="inferred",
            is_current=True,
        ))
    db.commit()
    return product


def test_image_url_normalization_rejects_non_http_protocols():
    assert normalize_public_image_url("https://cdn.example.com/product.jpg") == "https://cdn.example.com/product.jpg"
    assert normalize_public_image_url("ftp://cdn.example.com/product.jpg") is None
    assert normalize_public_image_url("file:///etc/passwd") is None
    assert normalize_public_image_url("https://user:pass@example.com/product.jpg") is None
    assert normalize_public_image_url("http://127.0.0.1/product.jpg") is None
    assert normalize_public_image_url("http://metadata.railway.internal/product.jpg") is None


def test_image_fetch_skips_invalid_urls_without_network_access():
    assert fetch_public_image(None) is None
    assert fetch_public_image("file:///etc/passwd") is None


def test_image_host_validation_requires_a_hostname():
    with pytest.raises(ValueError, match="no host"):
        _assert_public_host("https:///missing-host.png")


def test_admin_can_set_and_clear_product_image_url(client, db):
    product = make_product(db)
    response = client.put(
        f"/api/products/{product.id}/image",
        headers=auth_headers(),
        json={"image_url": "https://images.example.com/cloudberry-serum.png"},
    )
    assert response.status_code == 200
    assert response.json()["image_url"] == "https://images.example.com/cloudberry-serum.png"

    response = client.put(
        f"/api/products/{product.id}/image",
        headers=auth_headers(),
        json={"image_url": None},
    )
    assert response.status_code == 200
    assert response.json()["image_url"] is None


def test_invalid_product_image_url_is_rejected(client, db):
    product = make_product(db)
    response = client.put(
        f"/api/products/{product.id}/image",
        headers=auth_headers(),
        json={"image_url": "file:///etc/passwd"},
    )
    assert response.status_code == 400


def test_viewer_can_download_grounded_product_pdf(client, db):
    product = make_product(db)
    response = client.get(
        f"/api/products/{product.id}/pdf",
        headers=auth_headers("viewer@test.com"),
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "Cloudberry-Barrier-Serum-product-sheet.pdf" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF")
    assert len(response.content) > 3000
    assert response.content.count(b"/Type /Page\n") == 1


def test_pdf_requires_authentication(client, db):
    product = make_product(db)
    response = client.get(f"/api/products/{product.id}/pdf")
    assert response.status_code == 401


def test_fragrance_pdf_prioritizes_pyramid_and_compacts_missing_inci():
    pdf = build_product_pdf({
        "product_name": "Y", "brand_name": "YSL", "product_category": "Perfume",
        "gtin": "3614271716026", "image_url": None, "variants": [{"size": "100", "unit": "ml", "gtin": "3614271716026"}],
        "market_observations": [{"source_name": "Retail Data", "rating": 4.6, "review_count": 218,
                                 "review_summary": {"summary": "Praised for its versatile fresh woody profile."}}],
        "formulations": [], "field_values": [
            {"field_name": "product_type", "value": "Eau de Toilette", "is_current": True},
            {"field_name": "fragrance", "value": {"concentration": "Eau de Toilette", "fragrance_family": "Woody Aromatic",
                "top_notes": ["Bergamot"], "heart_notes": ["Sage"], "base_notes": ["Cedar"],
                "longevity": "Moderate", "sillage_projection": "Moderate", "seasonal_fit": ["Spring"], "occasion_fit": ["Office"]}, "is_current": True},
            {"field_name": "target_audience", "value": {"value": ["Need-led profile", "Taste-led profile", "Occasion-led profile"]}, "is_current": True},
            {"field_name": "directions", "value": {"text": "Spray onto pulse points."}, "is_current": True},
            {"field_name": "claims", "value": [{"name": "Fresh and clean fragrance", "status": "unverified"}], "is_current": True},
        ],
    })
    assert pdf.startswith(b"%PDF")
    from pypdf import PdfReader
    document = PdfReader(BytesIO(pdf))
    text = "\n".join(page.extract_text() or "" for page in document.pages)
    assert "TOP NOTES" in text and "HEART NOTES" in text and "BASE NOTES" in text
    assert "Ingredient list not available from current evidence" in text
    assert "Morning:" not in text and "Evening:" not in text
    assert "Fresh and clean fragrance" not in text
    assert "RATINGS, REVIEWS & CLAIMS" in text
    assert "4.6/5" in text and "218 reviews" in text
    assert "versatile fresh woody profile" in text


@pytest.mark.parametrize(("module_name", "module_value", "expected_heading"), [
    ("skincare", {"skin_types": {"recommended_for": ["Dry"]}, "texture": "Cream", "finish": "Dewy"}, "RATINGS, REVIEWS & CLAIMS"),
    ("haircare", {"hair_types": {"recommended_for": ["Dry"]}, "texture_format": "Cream"}, "RATINGS & REVIEWS"),
    ("makeup", {"shade_colour": "Rose", "coverage": "Medium", "finish": "Satin"}, "RATINGS & REVIEWS"),
])
def test_non_fragrance_pdfs_keep_category_layout_and_reviews(module_name, module_value, expected_heading):
    pdf = build_product_pdf({
        "product_name": "Category Test", "brand_name": "Beauty Lab", "product_category": module_name,
        "variants": [{"size": "30", "unit": "ml", "gtin": "1234567890123"}], "formulations": [],
        "market_observations": [{"source_name": "Retail Data", "rating": 4.4, "review_count": 72}],
        "field_values": [
            {"field_name": module_name, "value": module_value, "is_current": True},
            {"field_name": "target_audience", "value": {"value": ["Profile one", "Profile two", "Profile three"]}, "is_current": True},
        ],
    })
    from pypdf import PdfReader
    text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages)
    assert expected_heading in text
    assert "4.4/5" in text and "72 reviews" in text


def test_dossier_catalogue_fields_are_editable(client, db):
    product = make_product(db)
    for field_name, value in (
            ("skincare", {"skin_types": {"applicable": True, "recommended_for": ["Normal", "Dry"]}, "finish": {"value": "Velvety satin"}}),
            ("claims", [{"name": "Dermatologically Tested", "value": "Yes", "status": "verified"}]),
        ("ingredients_intelligence", [{"ingredient_name": "Glycerin", "inci_position": 2}]),
    ):
        response = client.put(
            f"/api/products/{product.id}",
            headers=auth_headers(),
            json={"field_name": field_name, "value": value, "reason": "Verified dossier correction."},
        )
        assert response.status_code == 200, response.text


def test_detail_uses_description_and_image_from_existing_source_record(client, db):
    product = make_product(db)
    job = ImportJob(
        id=uuid.uuid4(),
        filename="legacy.csv",
        file_hash=uuid.uuid4().hex,
        column_mapping={"description": "copy", "image_url": "hero_image"},
    )
    listing = SourceListing(
        id=uuid.uuid4(),
        import_job_id=job.id,
        canonical_product_id=product.id,
        raw_data={
            "copy": "A source-authored barrier serum description.",
            "hero_image": "https://images.example.com/cloudberry.jpg",
        },
        source_hash=uuid.uuid4().hex,
    )
    db.add_all([job, listing])
    db.commit()
    response = client.get(f"/api/products/{product.id}", headers=auth_headers("viewer@test.com"))
    assert response.status_code == 200
    assert response.json()["description"] == "A source-authored barrier serum description."
    assert response.json()["image_url"] == "https://images.example.com/cloudberry.jpg"
