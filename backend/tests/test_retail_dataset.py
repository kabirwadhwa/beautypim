import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from app.scraping.adapters.retail_dataset import RetailDatasetAdapter
from app.scraping.dataset_import import (
    analyze_retail_export,
    import_retail_export,
    import_summary,
)
from app.models import CanonicalProduct, ScrapedProductObservation
from app.services.catalogue_knowledge import build_catalogue_knowledge_context
from app.services.catalogue_knowledge import _retail_similarity


def record(record_id="3400000000001"):
    return {
        "_id": record_id,
        "source": "ALKEMICS",
        "retailBrandCode": {"label": "Maison Test"},
        "naming": {"longName": "Radiance Serum", "shortName": "Vitamin C"},
        "description": "<p>Brightening serum.</p><script>bad()</script>",
        "barcodeScanText": record_id,
        "composition": ["Aqua", "Niacinamide", "Glycerin"],
        "isClassifiedIn": {
            "path": [{"label": "Skincare"}, {"label": "Face"}],
            "name": {"label": "Serum"},
        },
        "netSizing": {"content": {"value": 30, "unityLabel": "ml"}},
        "retailPriceIncludingVAT": {
            "organisation1337": {"value": "24.90", "currency": "EUR"}
        },
        "retekEnrichment": {
            "masterData": {
                "productId": "P-123",
                "baseProductId": "P-123",
                "erp": {"status": "AVAILABLE"},
            }
        },
        "productBenefits": [{"label": "Radiance"}],
        "advices": ["<p>Apply morning and evening.</p>"],
        "skinTypeList": [{"label": "All skin types"}],
        "assets": {
            "pictures": [{
                "exportables": [{
                    "width": 1200,
                    "uniformResourceIdentifier": "https://images.example/product.jpg",
                }]
            }]
        },
    }


def test_retail_dataset_adapter_preserves_source_data_and_sanitizes_html():
    product = RetailDatasetAdapter().parse_record(
        record(), datetime.now(timezone.utc)
    )
    assert product.brand == "Maison Test"
    assert product.product_name == "Radiance Serum"
    assert product.description == "Brightening serum."
    assert "bad()" not in product.description
    assert product.gtin == "3400000000001"
    assert product.category_path == ["Skincare", "Face", "Serum"]
    assert product.ingredient_text_raw == "Aqua, Niacinamide, Glycerin"
    assert product.price == Decimal("24.90")
    assert product.image_urls == ["https://images.example/product.jpg"]
    assert product.fields["ingredient_text_raw"].path == "composition[]"


def test_dataset_analysis_streams_and_reports_coverage(tmp_path):
    path = tmp_path / "products.json"
    path.write_text(json.dumps([record(), record("3400000000002")]), encoding="utf-8")
    result = analyze_retail_export(path)
    assert result["records_analyzed"] == 2
    assert result["invalid_brand_or_name"] == 0
    assert result["unique_gtins"] == 2
    assert result["coverage"]["ingredient_text_raw"]["percentage"] == 100.0


def test_dataset_import_persists_products_provenance_and_is_idempotent(db, tmp_path):
    path = tmp_path / "products.json"
    path.write_text(
        json.dumps([record(), record("3400000000002"), {"_id": "invalid"}]),
        encoding="utf-8",
    )

    job = import_retail_export(
        db,
        str(path),
        batch_size=1,
        retain_raw_file=True,
    )
    result = import_summary(db, job)
    assert result["status"] == "partially_completed"
    assert result["products_persisted"] == 2
    assert result["products_failed"] == 1
    assert result["drafts"] == 2
    assert result["matched"] == 0
    assert db.query(CanonicalProduct).count() == 0
    assert db.query(ScrapedProductObservation).count() == 2
    assert "missing its product name or brand" in result["error_summary"]

    repeated = import_retail_export(db, str(path))
    assert repeated.id == job.id


def test_dataset_import_resumes_same_job_from_committed_progress(db, tmp_path):
    path = tmp_path / "resume-products.json"
    path.write_text(
        json.dumps([record("3400000000101"), record("3400000000102")]),
        encoding="utf-8",
    )
    job = import_retail_export(db, str(path), maximum_records=1, batch_size=1)
    assert job.products_persisted == 1

    job.status = "failed"
    job.completed_at = None
    db.commit()
    resumed = import_retail_export(db, str(path), batch_size=1)
    result = import_summary(db, resumed)

    assert resumed.id == job.id
    assert result["status"] == "completed"
    assert result["products_persisted"] == 2


def test_retail_corpus_retrieves_comparable_products_for_enrichment(db, tmp_path):
    path = tmp_path / "knowledge-products.json"
    path.write_text(json.dumps([record()]), encoding="utf-8")
    import_retail_export(db, str(path))

    context = build_catalogue_knowledge_context(
        db,
        uuid.uuid4(),
        product_name="Golden Glow Vitamin C Serum",
        brand="A New Brand",
        category="Skincare",
        product_family="Serum",
        description="Brightening niacinamide face serum for radiance",
    )

    assert context["retail_reference_matches"] == []
    examples = context["retail_knowledge_examples"]
    assert len(examples) == 1
    assert examples[0]["similarity_score"] > 0
    assert examples[0]["data"]["product_name"] == "Radiance Serum"
    assert "supports inference, not direct claims" in examples[0]["knowledge_role"]


def test_retail_similarity_prefers_beauty_product_concepts_over_generic_words():
    moisturizer_score, _ = _retail_similarity(
        {
            "product_name": "Antioxidant Day Cream",
            "product_type": "HYDRATANT VISAGE",
            "category_path": ["SOIN VISAGE", "HYDRATANT VISAGE"],
            "description": "Daily face hydration.",
        },
        name="Ultra Facial Cream",
        brand="Example",
        category="Skincare",
        product_family="Moisturizer",
        description="24-hour facial moisturizer",
    )
    foundation_score, _ = _retail_similarity(
        {
            "product_name": "Longwear Cream",
            "product_type": "FOND DE TEINT",
            "category_path": ["TEINT", "FOND DE TEINT"],
            "description": "Cream makeup formula.",
        },
        name="Ultra Facial Cream",
        brand="Example",
        category="Skincare",
        product_family="Moisturizer",
        description="24-hour facial moisturizer",
    )

    assert moisturizer_score >= foundation_score + 10


def test_free_from_word_does_not_change_the_product_type_concept():
    serum_score, _ = _retail_similarity(
        {
            "product_name": "Niacinamide Serum",
            "product_type": "SERUM",
            "category_path": ["SOIN VISAGE", "SERUM"],
            "description": "Face treatment.",
        },
        name="Niacinamide Treatment",
        brand="Example",
        category="Skincare",
        product_family="Serum",
        description="A vegan and fragrance-free face formula.",
    )
    perfume_score, _ = _retail_similarity(
        {
            "product_name": "Eau de Parfum",
            "product_type": "EAU DE PARFUM",
            "category_path": ["PARFUM"],
            "description": "Vegan fragrance.",
        },
        name="Niacinamide Treatment",
        brand="Example",
        category="Skincare",
        product_family="Serum",
        description="A vegan and fragrance-free face formula.",
    )

    assert serum_score >= perfume_score + 10
