import uuid
from unittest.mock import patch

from app.models import (
    Brand, CanonicalProduct, CrawlJob, FieldValue, ImportJob, ImportJobItem,
    ProductVariant, SourceListing,
)
from app.services.ingestion import suggest_mapping
from app.services.source_data_merge import reprocess_import_job_source_data
from app.worker import run_job_worker


def _current(db, product_id, field_name):
    return db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product_id,
        FieldValue.field_name == field_name,
        FieldValue.is_current == True,
    ).one()


def test_mapping_suggests_mat_business_columns():
    mapping = suggest_mapping([
        "EAN", "Brand", "Product Name", "Product Description",
        "Product Benefits", "Product USP",
    ])
    assert mapping["description"] == "Product Description"
    assert mapping["benefits"] == "Product Benefits"
    assert mapping["product_usp"] == "Product USP"


def test_existing_ean_source_merge_and_stored_row_reprocess_are_external_call_free(db):
    brand = Brand(id=uuid.uuid4(), name="MAT Brand", normalized_name="matbrand")
    product = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, product_name="Existing Serum",
        normalized_name="existingserum", image_url="https://images.example/existing.jpg",
    )
    variant = ProductVariant(
        id=uuid.uuid4(), canonical_product_id=product.id,
        gtin="7612345678901", size="30", unit="ml",
    )
    existing = [
        FieldValue(id=uuid.uuid4(), canonical_product_id=product.id, field_name="description",
                   value="Old inferred description", source_type="ai_inference",
                   review_status="inferred", is_current=True),
        FieldValue(id=uuid.uuid4(), canonical_product_id=product.id, field_name="benefits",
                   value=["Old inferred benefit"], source_type="ai_inference",
                   review_status="inferred", is_current=True),
        FieldValue(id=uuid.uuid4(), canonical_product_id=product.id, field_name="product_positioning",
                   value="Existing independent positioning", source_type="ai_inference",
                   review_status="inferred", is_current=True),
        FieldValue(id=uuid.uuid4(), canonical_product_id=product.id, field_name="skincare",
                   value={"skin_types": ["Dry"], "finish": "Radiant"}, source_type="ai_inference",
                   review_status="inferred", is_current=True),
        FieldValue(id=uuid.uuid4(), canonical_product_id=product.id, field_name="rating",
                   value=4.8, source_type="source_data", review_status="confirmed", is_current=True),
    ]
    job = ImportJob(
        id=uuid.uuid4(), filename="MAT.csv", source_name="MAT", file_hash=uuid.uuid4().hex,
        status="pending", total_rows=1, processed_rows=0,
        column_mapping={
            "product_name": "Product Name", "brand": "Brand", "ean": "EAN",
            "description": "Product Description", "benefits": "Product Benefits",
            "product_usp": "Product USP",
        },
    )
    listing = SourceListing(
        id=uuid.uuid4(), import_job_id=job.id, source_hash=uuid.uuid4().hex,
        raw_data={
            "EAN": "7612345678901", "Brand": "MAT Brand", "Product Name": "Existing Serum",
            "Product Description": "Authoritative MAT description",
            "Product Benefits": "Hydrates visibly; Supports a softer feel",
            "Product USP": "A premium daily hydration serum",
        },
    )
    item = ImportJobItem(
        id=uuid.uuid4(), import_job_id=job.id, source_row_number=1,
        source_listing_id=listing.id, status="pending", match_status="not_evaluated",
        enrichment_status="not_requested",
    )
    # PostgreSQL cannot insert the job item until its referenced source listing
    # exists.  Flush the parent records explicitly instead of relying on
    # SQLite's more permissive insert ordering in the test fixture.
    db.add_all([brand, product, variant, *existing, job, listing])
    db.flush()
    db.add(item)
    db.commit()

    with patch("app.worker.run_ai_enrichment") as ai, \
         patch("app.worker.queue_exact_formulation_research") as research:
        run_job_worker(db, job.id)
        ai.assert_not_called()
        research.assert_not_called()

    assert item.canonical_product_id == product.id
    assert item.enrichment_status == "not_requested"
    assert _current(db, product.id, "description").value == "Authoritative MAT description"
    assert _current(db, product.id, "description").source_type == "source_data"
    assert _current(db, product.id, "benefits").value == ["Hydrates visibly", "Supports a softer feel"]
    assert _current(db, product.id, "product_usp").value == "A premium daily hydration serum"
    assert _current(db, product.id, "product_positioning").value == "Existing independent positioning"
    assert _current(db, product.id, "skincare").value["finish"] == "Radiant"
    assert _current(db, product.id, "rating").value == 4.8
    assert product.image_url == "https://images.example/existing.jpg"
    assert db.query(CrawlJob).count() == 0

    # Reprocess the preserved row: blank description cannot erase the source
    # value, while a protected human benefit cannot be overwritten.
    current_benefits = _current(db, product.id, "benefits")
    current_benefits.is_current = False
    human_benefits = FieldValue(
        id=uuid.uuid4(), canonical_product_id=product.id, field_name="benefits",
        value=["Human-approved benefit"], source_type="human_edit",
        review_status="confirmed", is_current=True,
    )
    db.add(human_benefits)
    listing.raw_data = {
        **listing.raw_data,
        "Product Description": "  ",
        "Product Benefits": "Replacement source benefit",
        "Product USP": "Updated authoritative positioning",
    }
    db.commit()

    with patch("app.worker.run_ai_enrichment") as ai, \
         patch("app.worker.queue_exact_formulation_research") as research:
        result = reprocess_import_job_source_data(db, job.id)
        ai.assert_not_called()
        research.assert_not_called()

    assert result.listings_processed == 1
    assert result.human_values_protected == 1
    assert _current(db, product.id, "description").value == "Authoritative MAT description"
    assert _current(db, product.id, "benefits").value == ["Human-approved benefit"]
    assert _current(db, product.id, "product_usp").value == "Updated authoritative positioning"
    assert _current(db, product.id, "product_positioning").value == "Existing independent positioning"
    assert _current(db, product.id, "skincare").value["skin_types"] == ["Dry"]
    assert _current(db, product.id, "rating").value == 4.8
    assert db.query(CrawlJob).count() == 0


def test_reprocess_endpoint_is_authenticated_and_source_only(client, db):
    job = ImportJob(
        id=uuid.uuid4(), filename="stored.csv", source_name="MAT",
        file_hash=uuid.uuid4().hex, status="completed", total_rows=0,
        processed_rows=0, column_mapping={},
    )
    db.add(job); db.commit()
    login = client.post(
        "/api/auth/token",
        data={"username": "admin@test.com", "password": "securepassword123"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    with patch("app.worker.run_ai_enrichment") as ai, \
         patch("app.worker.queue_exact_formulation_research") as research:
        response = client.post(
            f"/api/feeds/jobs/{job.id}/reprocess-source-data", headers=headers,
        )
        ai.assert_not_called()
        research.assert_not_called()
    assert response.status_code == 200
    assert response.json()["mode"] == "source_data_only"
