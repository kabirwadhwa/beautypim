import uuid

from app.auth import create_access_token
from app.models import Brand, CanonicalProduct, FieldValue, Formulation, ProductVariant, ImportJob, SourceListing
from app.services.image_urls import normalize_public_image_url


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


def test_pdf_requires_authentication(client, db):
    product = make_product(db)
    response = client.get(f"/api/products/{product.id}/pdf")
    assert response.status_code == 401


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
