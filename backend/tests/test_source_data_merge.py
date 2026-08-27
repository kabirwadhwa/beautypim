import uuid
import io
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

from app.models import (
    Brand, CanonicalProduct, CrawlJob, CrawlUrl, FieldValue, Formulation,
    ImportJob, ImportJobItem, ProductTag, ProductVariant, RawPageObservation,
    ScrapedProductObservation, SourceListing,
)
from app.services.ingestion import read_preview, suggest_mapping
from app.services.business_export import build_business_row
from app.services.source_data_merge import dynamic_source_field_key, reprocess_import_job_source_data
from app.worker import run_job_worker


def _current(db, product_id, field_name):
    return db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product_id,
        FieldValue.field_name == field_name,
        FieldValue.is_current == True,
    ).one()


def test_mapping_suggests_mat_business_columns():
    mapping = suggest_mapping([
        "SKU Number", "EAN code", "Brand", "Product Name", "Size", "Article description",
        "BGB Subgroup", "BGB Typegroup", "Product Description", "Product Benefits",
        "Product USP", "Product Review Summary",
    ])
    assert mapping["description"] == "Product Description"
    assert mapping["benefits"] == "Product Benefits"
    assert mapping["product_usp"] == "Product USP"
    assert mapping["sku"] == "SKU Number"
    assert mapping["ean"] == "EAN code"
    assert mapping["article_description"] == "Article description"
    assert mapping["bgb_subgroup"] == "BGB Subgroup"
    assert mapping["bgb_typegroup"] == "BGB Typegroup"
    assert mapping["customer_review_summary"] == "Product Review Summary"


def test_mapping_uses_explicit_aliases_and_leaves_marketing_copy_dynamic():
    mapping = suggest_mapping(["Market", "Marketing Story", "Marketing Copy"])
    assert mapping["market"] == "Market"
    assert "Marketing Story" not in mapping.values()
    assert "Marketing Copy" not in mapping.values()


def test_dynamic_keys_do_not_collide_for_normalized_header_variants():
    headers = ["Packaging Material", "Packaging-Material", "PACKAGING MATERIAL"]
    keys = [dynamic_source_field_key(header) for header in headers]
    assert len(set(keys)) == 3


def test_xlsm_is_read_as_cell_data_without_macro_execution():
    stream = io.BytesIO()
    with pd.ExcelWriter(stream, engine="openpyxl") as writer:
        pd.DataFrame([{"EAN code": "3605522075283", "Product Name": "Lip Maestro"}]).to_excel(writer, index=False)
    headers, rows, total = read_preview(stream.getvalue(), "xlsm")
    assert headers == ["EAN code", "Product Name"]
    assert rows[0]["EAN code"] == "3605522075283"
    assert total == 1


def _make_job(db, row, *, source_row_number=1, product_id=None, variant_id=None):
    mapping = suggest_mapping(list(row))
    job = ImportJob(
        id=uuid.uuid4(), filename="MAT Missing Web Enrichment 070826.xlsm", source_name="MAT customer feed",
        file_hash=uuid.uuid4().hex, status="pending", total_rows=1, processed_rows=0,
        column_mapping=mapping,
    )
    listing = SourceListing(
        id=uuid.uuid4(), import_job_id=job.id, source_hash=uuid.uuid4().hex,
        raw_data=row, canonical_product_id=product_id, product_variant_id=variant_id,
    )
    item = ImportJobItem(
        id=uuid.uuid4(), import_job_id=job.id, source_row_number=source_row_number,
        source_listing_id=listing.id, status="pending", match_status="not_evaluated",
        enrichment_status="not_requested", canonical_product_id=product_id,
        product_variant_id=variant_id,
    )
    db.add_all([job, listing]); db.flush(); db.add(item); db.commit()
    return job, listing, item


def test_real_mat_shape_updates_exact_ean_preserves_enrichment_and_exposes_customer_content(db):
    old_brand = Brand(id=uuid.uuid4(), name="OLD BRAND", normalized_name="oldbrand")
    product = CanonicalProduct(
        id=uuid.uuid4(), brand_id=old_brand.id, product_name="OLD PRODUCT", normalized_name="oldproduct",
        image_url="https://images.example/existing-lipstick.jpg", review_status="approved",
    )
    variant = ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id, gtin="3605522075283")
    existing = [
        FieldValue(id=uuid.uuid4(), canonical_product_id=product.id, field_name="makeup",
                   value={"shade_colour": "405 Sultan", "finish": "Velvet matte"}, source_type="ai_inference", review_status="inferred", is_current=True),
        FieldValue(id=uuid.uuid4(), canonical_product_id=product.id, field_name="ingredients_intelligence",
                   value={"key_ingredients": ["Dimethicone"]}, source_type="deterministic_rule", review_status="confirmed", is_current=True),
        FieldValue(id=uuid.uuid4(), canonical_product_id=product.id, field_name="rating",
                   value=4.7, source_type="deterministic_rule", review_status="confirmed", is_current=True),
    ]
    db.add_all([old_brand, product, variant, *existing]); db.commit()
    row = {
        "SKU Number": "1503369", "EAN code": "3605522075283", "Brand": "Armani",
        "Product Name": "Lip Maestro Liquid Lipstick – 405 Sultan", "Size": "6.5 ml",
        "Article description": "ARM LIP 405 SULTAN MAESTRO", "BGB Subgroup": "MAKEUP (1ST LEVEL)",
        "BGB Typegroup": "LIPS (2ND LEVEL)",
        "Product USP": "Armani's iconic liquid lip color with a plush velvet-matte finish and saturated pigment.",
        "Product Description": "Lip Maestro is a lightweight liquid lipstick...",
        "Product Benefits": "High-impact color; soft velvety finish; comfortable liquid texture",
        "Product Review Summary": "Customers commonly praise the saturated pigment...",
    }
    job, _, item = _make_job(db, row)
    with patch("app.worker.run_ai_enrichment") as ai, patch("app.worker.queue_exact_formulation_research") as research:
        run_job_worker(db, job.id)
        ai.assert_not_called(); research.assert_not_called()

    db.refresh(product); db.refresh(variant); db.refresh(item)
    assert item.canonical_product_id == product.id
    assert product.brand.name == "Armani"
    assert product.product_name == "Lip Maestro Liquid Lipstick – 405 Sultan"
    assert variant.gtin == "3605522075283"
    assert (variant.size, variant.unit) == ("6.5", "ml")
    assert _current(db, product.id, "sku").value == ["1503369"]
    assert _current(db, product.id, "article_description").value == "ARM LIP 405 SULTAN MAESTRO"
    assert _current(db, product.id, "bgb_subgroup").value == "MAKEUP (1ST LEVEL)"
    assert _current(db, product.id, "bgb_typegroup").value == "LIPS (2ND LEVEL)"
    assert _current(db, product.id, "product_usp").value.startswith("Armani's iconic")
    assert _current(db, product.id, "description").value.startswith("Lip Maestro")
    assert _current(db, product.id, "benefits").value == ["High-impact color", "soft velvety finish", "comfortable liquid texture"]
    assert _current(db, product.id, "customer_review_summary").value.startswith("Customers commonly praise")
    assert product.image_url == "https://images.example/existing-lipstick.jpg"
    assert _current(db, product.id, "makeup").value["finish"] == "Velvet matte"
    assert _current(db, product.id, "ingredients_intelligence").value["key_ingredients"] == ["Dimethicone"]
    assert _current(db, product.id, "rating").value == 4.7
    assert db.query(CrawlJob).count() == 0

    from app.routes.products import get_product_detail
    detail = get_product_detail(product.id, db, None)
    assert detail.description == row["Product Description"]
    attributes = {item.key: item for item in detail.source_attributes}
    assert attributes["customer_review_summary"].value == row["Product Review Summary"]
    assert detail.review_aggregate is None or detail.review_aggregate.get("review_sample_count", 0) == 0
    exported = build_business_row(db, detail, include_inferred=True)
    assert exported["customer_review_summary"] == row["Product Review Summary"]
    assert exported["article_description"] == row["Article description"]


def test_unknown_columns_are_generic_latest_nonempty_and_exported(db):
    brand = Brand(id=uuid.uuid4(), name="Known", normalized_name="known")
    product = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Known Product", normalized_name="knownproduct", review_status="approved")
    variant = ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id, gtin="1234567890123")
    db.add_all([brand, product, variant]); db.commit()
    first = {"EAN": variant.gtin, "Brand": "Known", "Product Name": "Known Product", "Packaging Material": "Glass", "Launch Wave": "Wave 3", "Customer Internal Label": "Prestige Priority"}
    job, _, _ = _make_job(db, first)
    with patch("app.worker.run_ai_enrichment") as ai, patch("app.worker.queue_exact_formulation_research") as research:
        run_job_worker(db, job.id); ai.assert_not_called(); research.assert_not_called()
    assert _current(db, product.id, dynamic_source_field_key("Packaging Material")).value == "Glass"
    assert _current(db, product.id, dynamic_source_field_key("Launch Wave")).value == "Wave 3"
    assert _current(db, product.id, dynamic_source_field_key("Customer Internal Label")).value == "Prestige Priority"

    second = {"EAN": variant.gtin, "Brand": "", "Product Name": "", "Packaging Material": "Recycled Glass", "Launch Wave": ""}
    job2, _, _ = _make_job(db, second)
    # Exact GTIN remains sufficient even when identity text is blank.
    with patch("app.worker.run_ai_enrichment") as ai, patch("app.worker.queue_exact_formulation_research") as research:
        run_job_worker(db, job2.id); ai.assert_not_called(); research.assert_not_called()
    assert _current(db, product.id, dynamic_source_field_key("Packaging Material")).value == "Recycled Glass"
    assert _current(db, product.id, dynamic_source_field_key("Launch Wave")).value == "Wave 3"

    from app.routes.products import get_product_detail
    detail = get_product_detail(product.id, db, None)
    labels = {item.label: item.value for item in detail.source_attributes}
    assert labels["Packaging Material"] == "Recycled Glass"
    assert labels["Launch Wave"] == "Wave 3"
    assert labels["Customer Internal Label"] == "Prestige Priority"
    exported = build_business_row(db, detail, include_inferred=True)
    assert exported["imported_attribute:Packaging Material"] == "Recycled Glass"
    assert exported["imported_attribute:Launch Wave"] == "Wave 3"


def test_import_chronology_survives_historical_reprocess(db):
    brand = Brand(id=uuid.uuid4(), name="Brand", normalized_name="brand")
    product = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Product", normalized_name="product")
    variant = ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id, gtin="1234567890123")
    db.add_all([brand, product, variant]); db.commit()
    base = {"EAN": variant.gtin, "Brand": "Brand", "Product Name": "Product"}
    created = datetime(2026, 1, 1, 12, 0, 0)
    jobs = []
    for offset, value in enumerate(("Glass", "Plastic", "")):
        job, _, _ = _make_job(db, {**base, "Packaging Material": value})
        job.created_at = created + timedelta(minutes=offset)
        db.commit()
        run_job_worker(db, job.id)
        jobs.append(job)
    key = dynamic_source_field_key("Packaging Material")
    assert _current(db, product.id, key).value == "Plastic"
    reprocess_import_job_source_data(db, jobs[0].id)
    assert _current(db, product.id, key).value == "Plastic"
    reprocess_import_job_source_data(db, jobs[1].id)
    assert _current(db, product.id, key).value == "Plastic"


def test_marketing_story_is_dynamic_and_same_import_row_order_is_stable(db):
    brand = Brand(id=uuid.uuid4(), name="Brand", normalized_name="brand")
    product = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Product", normalized_name="product")
    variant = ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id, gtin="1111222233334")
    db.add_all([brand, product, variant]); db.commit()
    rows = [(10, "A"), (20, "B")]
    mapping = suggest_mapping(["EAN", "Brand", "Product Name", "Marketing Story"])
    assert "Marketing Story" not in mapping.values()
    job = ImportJob(id=uuid.uuid4(), filename="stories.xlsx", source_name="customer", file_hash=uuid.uuid4().hex,
                    status="pending", total_rows=2, processed_rows=0, column_mapping=mapping)
    db.add(job); db.flush()
    for row_number, story in rows:
        listing = SourceListing(id=uuid.uuid4(), import_job_id=job.id, source_hash=uuid.uuid4().hex,
                                raw_data={"EAN": variant.gtin, "Brand": "Brand", "Product Name": "Product", "Marketing Story": story})
        db.add(listing); db.flush()
        db.add(ImportJobItem(id=uuid.uuid4(), import_job_id=job.id, source_row_number=row_number,
                             source_listing_id=listing.id, status="pending", match_status="not_evaluated",
                             enrichment_status="not_requested"))
    db.commit(); run_job_worker(db, job.id)
    key = dynamic_source_field_key("Marketing Story")
    assert _current(db, product.id, key).value == "B"
    assert db.query(FieldValue).filter(FieldValue.canonical_product_id == product.id, FieldValue.field_name == "market").count() == 0
    reprocess_import_job_source_data(db, job.id)
    assert _current(db, product.id, key).value == "B"


def test_formatted_gtin_updates_same_product_even_when_identity_text_changes(db):
    brand = Brand(id=uuid.uuid4(), name="Old Brand", normalized_name="oldbrand")
    product = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Old Name", normalized_name="oldname")
    variant = ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id, gtin="3605522075283")
    db.add_all([brand, product, variant]); db.commit()
    job, _, item = _make_job(db, {
        "EAN code": " 3605522075283.0 ", "Brand": "Armani",
        "Product Name": "Lip Maestro Liquid Lipstick", "Product USP": "Velvet colour",
    })
    run_job_worker(db, job.id)
    db.refresh(item); db.refresh(product)
    assert item.canonical_product_id == product.id
    assert db.query(CanonicalProduct).count() == 1
    assert product.product_name == "Lip Maestro Liquid Lipstick"
    assert product.brand.name == "Armani"


def test_exact_ean_merge_and_old_reprocess_preserve_all_unrelated_objects(db):
    brand = Brand(id=uuid.uuid4(), name="Original Brand", normalized_name="originalbrand")
    product = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, product_name="Original Product",
        normalized_name="originalproduct", image_url="https://images.test/original.jpg",
    )
    matched_variant = ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id, gtin="7611773160681", size="50", unit="ml")
    unrelated_variant = ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id, gtin="7611773160698", size="30", unit="ml")
    unrelated_field = FieldValue(
        id=uuid.uuid4(), canonical_product_id=product.id, field_name="sensory_description",
        value="Silky cream", source_type="ai_inference", review_status="inferred", is_current=True,
    )
    tag = ProductTag(id=uuid.uuid4(), canonical_product_id=product.id, name="Investor Demo", normalized_name="investor demo")
    formulation = Formulation(
        id=uuid.uuid4(), canonical_product_id=product.id, product_variant_id=matched_variant.id,
        raw_inci_text="Water, Glycerin", source_reference="existing", content_hash="f" * 64,
    )
    crawl = CrawlJob(id=uuid.uuid4(), domain="reviews.test", starting_urls=["https://reviews.test/p"],
                     crawl_mode="single_url", status="completed", configuration={})
    crawl_url = CrawlUrl(id=uuid.uuid4(), crawl_job_id=crawl.id, url="https://reviews.test/p",
                         normalized_url="https://reviews.test/p", state="completed", page_type="product")
    raw_page = RawPageObservation(
        id=uuid.uuid4(), crawl_job_id=crawl.id, crawl_url_id=crawl_url.id,
        source_url="https://reviews.test/p", final_url="https://reviews.test/p", http_status=200,
        content_hash="r" * 64, response_size=100, parser_version="test",
    )
    review = ScrapedProductObservation(
        id=uuid.uuid4(), crawl_job_id=crawl.id, raw_page_id=raw_page.id,
        canonical_product_id=product.id, product_variant_id=matched_variant.id,
        source_name="Reviews", source_domain="reviews.test", source_url="https://reviews.test/p",
        canonical_url="https://reviews.test/p", normalized_payload={"rating": 4.8, "review_count": 50},
        identity_hash="i" * 64, structured_hash="s" * 64, match_status="matched",
        adapter_name="test", adapter_version="1", parser_version="1",
    )
    # Flush each FK layer explicitly so this preservation fixture behaves the
    # same on PostgreSQL as it does on SQLite (the models use scalar FK ids,
    # rather than ORM relationships, so SQLAlchemy cannot infer insert order).
    db.add_all([brand, product]); db.flush()
    db.add_all([matched_variant, unrelated_variant, unrelated_field, tag, crawl]); db.flush()
    db.add_all([formulation, crawl_url]); db.flush()
    db.add(raw_page); db.flush()
    db.add(review); db.commit()
    preserved_ids = {
        "product": product.id, "matched_variant": matched_variant.id, "unrelated_variant": unrelated_variant.id,
        "field": unrelated_field.id, "tag": tag.id, "formulation": formulation.id, "review": review.id,
    }
    old_job, _, _ = _make_job(db, {
        "EAN": matched_variant.gtin, "Brand": "Old Feed Brand", "Product Name": "Old Feed Name",
        "Product USP": "Old USP", "Size": "40 ml", "Variant": "Old Feed Variant",
    })
    old_job.created_at = datetime(2026, 1, 1, 10, 0); db.commit(); run_job_worker(db, old_job.id)
    new_job, _, item = _make_job(db, {
        "EAN": matched_variant.gtin, "Brand": "Current Brand", "Product Name": "Current Name",
        "Product USP": "Current USP", "Product Description": "Current description",
        "Size": "50 ml", "Variant": "Current Variant",
    })
    new_job.created_at = datetime(2026, 1, 2, 10, 0); db.commit(); run_job_worker(db, new_job.id)
    db.refresh(item); db.refresh(product)
    assert item.canonical_product_id == preserved_ids["product"]
    assert product.product_name == "Current Name" and product.brand.name == "Current Brand"
    assert _current(db, product.id, "product_usp").value == "Current USP"
    assert (matched_variant.size, matched_variant.unit, matched_variant.variant_name) == ("50", "ml", "Current Variant")
    reprocess_import_job_source_data(db, old_job.id)
    db.refresh(product)
    assert product.id == preserved_ids["product"]
    assert product.product_name == "Current Name" and product.brand.name == "Current Brand"
    assert _current(db, product.id, "product_usp").value == "Current USP"
    db.refresh(matched_variant)
    assert (matched_variant.size, matched_variant.unit, matched_variant.variant_name) == ("50", "ml", "Current Variant")
    assert product.image_url == "https://images.test/original.jpg"
    assert db.query(ProductVariant).filter(ProductVariant.id == preserved_ids["matched_variant"]).count() == 1
    assert db.query(ProductVariant).filter(ProductVariant.id == preserved_ids["unrelated_variant"]).count() == 1
    assert db.query(Formulation).filter(Formulation.id == preserved_ids["formulation"]).count() == 1
    assert db.query(ScrapedProductObservation).filter(ScrapedProductObservation.id == preserved_ids["review"]).count() == 1
    assert db.query(ProductTag).filter(ProductTag.id == preserved_ids["tag"]).count() == 1
    assert db.query(FieldValue).filter(FieldValue.id == preserved_ids["field"], FieldValue.is_current == True).count() == 1


def test_source_attributes_require_explicit_customer_import_provenance(db):
    brand = Brand(id=uuid.uuid4(), name="Brand", normalized_name="brand")
    product = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Product", normalized_name="product")
    variant = ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id, gtin="1234567890123")
    db.add_all([brand, product, variant]); db.commit()
    job, _, _ = _make_job(db, {"EAN": variant.gtin, "Brand": "Brand", "Product Name": "Product", "Packaging Material": "Glass"})
    run_job_worker(db, job.id)
    db.add(FieldValue(
        id=uuid.uuid4(), canonical_product_id=product.id,
        field_name="source_attr.web-only.fake", value="Must not leak",
        source_type="source_data", source_reference="crawl:example", review_status="confirmed", is_current=True,
        evidence=[{"evidence_type": "web_page", "source_url": "https://example.test/product"}],
    )); db.commit()
    from app.routes.products import get_product_detail
    detail = get_product_detail(product.id, db, None)
    attributes = {item.key: item.value for item in detail.source_attributes}
    assert attributes[dynamic_source_field_key("Packaging Material")] == "Glass"
    assert "source_attr.web-only.fake" not in attributes


def test_customer_ingredients_remain_visible_without_formulation(db):
    brand = Brand(id=uuid.uuid4(), name="Brand", normalized_name="brand")
    product = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Cream", normalized_name="cream")
    variant = ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id, gtin="9876543210987")
    db.add_all([brand, product, variant]); db.commit()
    job, _, _ = _make_job(db, {"EAN": variant.gtin, "Brand": "Brand", "Product Name": "Cream", "Ingredients": "Water, Glycerin"})
    run_job_worker(db, job.id)
    from app.routes.products import get_product_detail
    detail = get_product_detail(product.id, db, None)
    assert not detail.formulations
    assert next(fv.value for fv in detail.field_values if fv.is_current and fv.field_name == "ingredients") == "Water, Glycerin"


def test_human_identity_survives_customer_feed_while_other_values_update(db):
    brand = Brand(id=uuid.uuid4(), name="Human Corrected Brand", normalized_name="humancorrectedbrand")
    product = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Product", normalized_name="product")
    variant = ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id, gtin="9876543210987")
    human = FieldValue(id=uuid.uuid4(), canonical_product_id=product.id, field_name="brand", value="Human Corrected Brand", source_type="human_edit", review_status="confirmed", is_current=True)
    db.add_all([brand, product, variant, human]); db.commit()
    row = {"EAN": variant.gtin, "Brand": "Feed Brand", "Product Name": "Feed Product", "Product USP": "Feed USP"}
    job, _, _ = _make_job(db, row)
    with patch("app.worker.run_ai_enrichment") as ai, patch("app.worker.queue_exact_formulation_research") as research:
        run_job_worker(db, job.id); ai.assert_not_called(); research.assert_not_called()
    db.refresh(product)
    assert product.brand.name == "Human Corrected Brand"
    assert product.product_name == "Feed Product"
    assert _current(db, product.id, "product_usp").value == "Feed USP"


def test_duplicate_ean_rows_are_ordered_latest_nonempty_and_preserve_all_skus(db):
    brand = Brand(id=uuid.uuid4(), name="Brand", normalized_name="brand")
    product = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Product", normalized_name="product")
    variant = ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id, gtin="1111222233334")
    db.add_all([brand, product, variant]); db.commit()
    mapping = {"ean": "EAN", "brand": "Brand", "product_name": "Product Name", "sku": "SKU Number", "product_usp": "Product USP"}
    job = ImportJob(id=uuid.uuid4(), filename="duplicates.xlsx", source_name="customer", file_hash=uuid.uuid4().hex,
                    status="pending", total_rows=2, processed_rows=0, column_mapping=mapping)
    first = SourceListing(id=uuid.uuid4(), import_job_id=job.id, source_hash=uuid.uuid4().hex,
                          raw_data={"EAN": variant.gtin, "Brand": "Brand", "Product Name": "Product", "SKU Number": "SKU-1", "Product USP": "First USP"})
    second = SourceListing(id=uuid.uuid4(), import_job_id=job.id, source_hash=uuid.uuid4().hex,
                           raw_data={"EAN": variant.gtin, "Brand": "Brand", "Product Name": "Product", "SKU Number": "SKU-2", "Product USP": "Second USP"})
    db.add_all([job, first, second]); db.flush()
    db.add_all([
        ImportJobItem(id=uuid.uuid4(), import_job_id=job.id, source_row_number=1, source_listing_id=first.id, status="pending", match_status="not_evaluated", enrichment_status="not_requested"),
        ImportJobItem(id=uuid.uuid4(), import_job_id=job.id, source_row_number=2, source_listing_id=second.id, status="pending", match_status="not_evaluated", enrichment_status="not_requested"),
    ]); db.commit()
    with patch("app.worker.run_ai_enrichment") as ai, patch("app.worker.queue_exact_formulation_research") as research:
        run_job_worker(db, job.id); ai.assert_not_called(); research.assert_not_called()
    assert _current(db, product.id, "product_usp").value == "Second USP"
    assert _current(db, product.id, "sku").value == ["SKU-1", "SKU-2"]


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
