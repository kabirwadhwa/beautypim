import uuid

from app.config import settings
from app.models import (
    Brand, CanonicalProduct, CrawlJob, CrawlUrl, FieldValue, ImportJob, RawPageObservation,
    ScrapedProductObservation, SourceListing,
)
from app.services.review_summarization import summarize_product_reviews, _insufficient_lines
from app.services.review_aggregate import select_review_aggregate
from app.services.review_aggregate import classify_review_quality, _quality_rank


def test_review_quality_thresholds_are_central_and_truthful():
    assert classify_review_quality(1.0, 1) == "insufficient"
    assert classify_review_quality(None, 30) == "insufficient"
    assert classify_review_quality(4.2, 8) == "weak"
    assert classify_review_quality(4.4, 20) == "moderate"
    assert classify_review_quality(4.7, 500) == "strong"
    assert _quality_rank(4.2, 8) > _quality_rank(None, 3000)
    lines = _insufficient_lines({"average_rating": 1.0, "review_count": 1})
    assert "does not present" in " ".join(lines)
    assert "1.0/5" not in " ".join(lines)


def test_review_synthesis_reads_and_persists_actual_deidentified_samples(db, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    brand = Brand(id=uuid.uuid4(), name="Review Lab", normalized_name=uuid.uuid4().hex)
    product = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, product_name="Moon Serum", normalized_name="moonserum",
    )
    crawl = CrawlJob(
        id=uuid.uuid4(), domain="shop.example", starting_urls=["https://shop.example/p/moon"],
        crawl_mode="single_url", status="completed", configuration={},
    )
    url = CrawlUrl(
        id=uuid.uuid4(), crawl_job_id=crawl.id, url="https://shop.example/p/moon",
        normalized_url="https://shop.example/p/moon", state="completed", depth=0,
    )
    page = RawPageObservation(
        id=uuid.uuid4(), crawl_job_id=crawl.id, crawl_url_id=url.id,
        source_url=url.url, final_url=url.url, http_status=200,
        content_hash=uuid.uuid4().hex, response_size=100, parser_version="test",
    )
    observation = ScrapedProductObservation(
        id=uuid.uuid4(), crawl_job_id=crawl.id, raw_page_id=page.id,
        canonical_product_id=product.id, source_name="Retail Data", source_domain="shop.example",
        source_url=url.url, canonical_url=url.url, identity_hash=uuid.uuid4().hex,
        structured_hash=uuid.uuid4().hex, match_status="matched", adapter_name="test",
        adapter_version="1", parser_version="1",
        normalized_payload={"rating": 4.7, "review_count": 128, "review_summary": {
            "average_rating": 4.7, "review_count": 128, "review_sample_count": 2,
            "review_samples": [
                {"text": "The silky texture absorbs quickly and the bottle feels sturdy.", "rating": 5},
                {"text": "The serum works well but the price feels high for the quantity.", "rating": 3},
            ],
            "frequently_praised_topics": ["texture", "packaging"],
            "frequent_complaint_topics": ["value"],
        }},
    )
    db.add_all([brand, crawl])
    db.flush()
    db.add(product)
    db.flush()
    db.add(url)
    db.flush()
    db.add(page)
    db.flush()
    db.add(observation)
    db.commit()

    summary = summarize_product_reviews(db, product.id)
    db.commit()
    db.refresh(observation)

    assert summary["review_sample_count"] == 2
    assert "2 de-identified review samples" in summary["ai_summary_text"]
    assert observation.normalized_payload["review_summary"]["summary_model"] == "deterministic-evidence-summary"
    selected = select_review_aggregate(db, product.id)
    assert selected["average_rating"] == 4.7
    assert selected["review_count"] == 128
    assert selected["evidence_reference"].endswith(str(observation.id))

    second = ScrapedProductObservation(
        id=uuid.uuid4(), crawl_job_id=crawl.id, raw_page_id=page.id,
        canonical_product_id=product.id, source_name="Second Retailer", source_domain="other.example",
        source_url="https://other.example/moon", canonical_url="https://other.example/moon",
        identity_hash=uuid.uuid4().hex, structured_hash=uuid.uuid4().hex,
        match_status="matched", adapter_name="test", adapter_version="1", parser_version="1",
        normalized_payload={"rating": 5.0, "review_count": 8, "review_summary": {
            "average_rating": 5.0, "review_count": 8,
            "review_samples": [
                {"text": "The silky texture absorbs quickly and the bottle feels sturdy.", "rating": 5},
                {"text": "A lightweight formula that layers well under makeup every morning.", "rating": 5},
            ],
        }},
    )
    db.add(second); db.commit()
    combined = select_review_aggregate(db, product.id)
    assert combined["average_rating"] == round((4.7 * 128 + 5.0 * 8) / 136, 3)
    assert combined["represented_review_count"] == 136
    assert combined["review_source_count"] == 2
    assert combined["review_sample_count"] == 3  # duplicate sample removed across sources

    wrong_variant = ScrapedProductObservation(
        id=uuid.uuid4(), crawl_job_id=crawl.id, raw_page_id=page.id,
        canonical_product_id=product.id, source_name="Wrong Variant", source_domain="wrong.example",
        source_url="https://wrong.example/sibling", canonical_url="https://wrong.example/sibling",
        identity_hash=uuid.uuid4().hex, structured_hash=uuid.uuid4().hex,
        match_status="conflict", adapter_name="test", adapter_version="1", parser_version="1",
        normalized_payload={"rating": 1.0, "review_count": 9999, "review_summary": {
            "review_samples": [{"text": "This is a different shade and must never enter the exact product summary.", "rating": 1}]
        }},
    )
    db.add(wrong_variant); db.commit()
    safe = select_review_aggregate(db, product.id)
    assert safe["represented_review_count"] == 136
    assert all(sample["source_domain"] != "wrong.example" for sample in safe["review_summary"]["review_samples"])


def test_selector_always_exposes_truthful_summary_for_aggregate_only_evidence(db):
    brand = Brand(id=uuid.uuid4(), name="Givenchy", normalized_name=uuid.uuid4().hex)
    product = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, product_name="Prisme Libre Puff",
        normalized_name="prismelibrepuff",
    )
    crawl = CrawlJob(
        id=uuid.uuid4(), domain="shop.example", starting_urls=["https://shop.example/p/puff"],
        crawl_mode="single_url", status="completed", configuration={},
    )
    url = CrawlUrl(
        id=uuid.uuid4(), crawl_job_id=crawl.id, url="https://shop.example/p/puff",
        normalized_url="https://shop.example/p/puff", state="completed", depth=0,
    )
    page = RawPageObservation(
        id=uuid.uuid4(), crawl_job_id=crawl.id, crawl_url_id=url.id,
        source_url=url.url, final_url=url.url, http_status=200,
        content_hash=uuid.uuid4().hex, response_size=100, parser_version="test",
    )
    observation = ScrapedProductObservation(
        id=uuid.uuid4(), crawl_job_id=crawl.id, raw_page_id=page.id,
        canonical_product_id=product.id, source_name="Retail Data", source_domain="shop.example",
        source_url=url.url, canonical_url=url.url, identity_hash=uuid.uuid4().hex,
        structured_hash=uuid.uuid4().hex, match_status="matched", adapter_name="test",
        adapter_version="1", parser_version="1",
        normalized_payload={"rating": 4.5, "review_count": 44},
    )
    db.add_all([brand, crawl]); db.flush()
    db.add(product); db.flush()
    db.add(url); db.flush()
    db.add(page); db.flush()
    db.add(observation); db.commit()

    selected = select_review_aggregate(db, product.id)
    assert selected["average_rating"] == 4.5
    assert selected["review_count"] == 44
    assert selected["review_sample_count"] == 0
    assert selected["evidence_strength"] == "aggregate_only"
    assert "review-text evidence was insufficient" in selected["review_summary"]["evidence_limitation"]


def test_selector_uses_cited_web_search_field_evidence_when_retailer_blocks_crawl(db):
    brand = Brand(id=uuid.uuid4(), name="Search Brand", normalized_name=uuid.uuid4().hex)
    product = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, product_name="Search Fragrance",
        normalized_name="searchfragrance",
    )
    db.add(brand); db.flush(); db.add(product); db.flush()
    source_url = "https://retailer.example/product/search-fragrance"
    db.add_all([
        FieldValue(
            canonical_product_id=product.id, field_name="rating", value=4.6,
            source_type="source_data", source_reference=source_url,
            review_status="inferred", is_current=True, evidence=[{
                "evidence_type": "licensed_web_search_market_observation",
                "match_scope": "exact_gtin",
            }],
        ),
        FieldValue(
            canonical_product_id=product.id, field_name="review_count", value=920,
            source_type="source_data", source_reference=source_url,
            review_status="inferred", is_current=True, evidence=[{
                "evidence_type": "licensed_web_search_market_observation",
                "match_scope": "exact_gtin",
            }],
        ),
    ])
    db.commit()

    selected = select_review_aggregate(db, product.id)

    assert selected["average_rating"] == 4.6
    assert selected["review_count"] == 920
    assert selected["source_domain"] == "retailer.example"
    assert selected["match_scope"] == "exact_product"
    assert selected["review_sample_count"] == 0
    assert selected["review_summary"]["evidence_limitation"]


def test_declared_samples_without_text_never_create_intelligence_or_themes(db, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    brand = Brand(id=uuid.uuid4(), name="Tool Brand", normalized_name=uuid.uuid4().hex)
    product = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Curler Refill Pad", normalized_name="curlerrefillpad")
    crawl = CrawlJob(id=uuid.uuid4(), domain="shop.example", starting_urls=["https://shop.example/p/pad"], crawl_mode="single_url", status="completed", configuration={})
    url = CrawlUrl(id=uuid.uuid4(), crawl_job_id=crawl.id, url="https://shop.example/p/pad", normalized_url="https://shop.example/p/pad", state="completed", depth=0)
    page = RawPageObservation(id=uuid.uuid4(), crawl_job_id=crawl.id, crawl_url_id=url.id, source_url=url.url, final_url=url.url, http_status=200, content_hash=uuid.uuid4().hex, response_size=100, parser_version="test")
    observation = ScrapedProductObservation(
        id=uuid.uuid4(), crawl_job_id=crawl.id, raw_page_id=page.id, canonical_product_id=product.id,
        source_name="Retailer", source_domain="shop.example", source_url=url.url, canonical_url=url.url,
        identity_hash=uuid.uuid4().hex, structured_hash=uuid.uuid4().hex, match_status="matched",
        adapter_name="test", adapter_version="1", parser_version="1",
        normalized_payload={"rating": 4.7, "review_count": 248, "review_summary": {
            "review_sample_count": 8, "review_samples": [],
            "frequently_praised_topics": ["value"], "positive_themes": ["sensitivity"],
            "ai_summary_text": "Unsupported summary",
        }},
    )
    db.add_all([brand, crawl]); db.flush(); db.add(product); db.flush(); db.add(url); db.flush(); db.add(page); db.flush(); db.add(observation); db.commit()

    summary = summarize_product_reviews(db, product.id)
    selected = select_review_aggregate(db, product.id)
    assert selected["review_sample_count"] == 0
    assert selected["aggregate_strength"] == "strong"
    assert selected["review_intelligence_strength"] == "insufficient"
    assert summary["ai_summary_text"] is None
    assert summary["positive_themes"] == summary["negative_themes"] == summary["mixed_themes"] == []


def test_uncorroborated_field_values_do_not_leak_into_canonical_reviews(db):
    brand = Brand(id=uuid.uuid4(), name="Conflict Brand", normalized_name=uuid.uuid4().hex)
    product = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Conflict Product", normalized_name="conflictproduct")
    db.add_all([brand, product]); db.flush()
    db.add(FieldValue(canonical_product_id=product.id, field_name="rating", value=1.0,
                      source_type="source_data", source_reference="https://wrong.example/p", review_status="conflicting", is_current=True,
                      evidence=[{"evidence_type": "scraped_product_observation", "match_scope": "comparable"}]))
    db.commit()
    assert select_review_aggregate(db, product.id) is None


def test_review_synthesis_accepts_top_level_aggregate_rating(db, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    brand = Brand(id=uuid.uuid4(), name="Aggregate Brand", normalized_name=uuid.uuid4().hex)
    product = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, product_name="Aggregate Lipstick",
        normalized_name="aggregatelipstick",
    )
    crawl = CrawlJob(
        id=uuid.uuid4(), domain="shop.example", starting_urls=["https://shop.example/p/lipstick"],
        crawl_mode="single_url", status="completed", configuration={},
    )
    url = CrawlUrl(
        id=uuid.uuid4(), crawl_job_id=crawl.id, url="https://shop.example/p/lipstick",
        normalized_url="https://shop.example/p/lipstick", state="completed", depth=0,
    )
    page = RawPageObservation(
        id=uuid.uuid4(), crawl_job_id=crawl.id, crawl_url_id=url.id,
        source_url=url.url, final_url=url.url, http_status=200,
        content_hash=uuid.uuid4().hex, response_size=100, parser_version="test",
    )
    observation = ScrapedProductObservation(
        id=uuid.uuid4(), crawl_job_id=crawl.id, raw_page_id=page.id,
        canonical_product_id=product.id, source_name="Retail Data", source_domain="shop.example",
        source_url=url.url, canonical_url=url.url, identity_hash=uuid.uuid4().hex,
        structured_hash=uuid.uuid4().hex, match_status="matched", adapter_name="test",
        adapter_version="1", parser_version="1",
        normalized_payload={"rating": 4.5, "review_count": 37},
    )
    db.add_all([brand, crawl])
    db.flush()
    db.add(product)
    db.flush()
    db.add(url)
    db.flush()
    db.add(page)
    db.flush()
    db.add(observation)
    db.commit()

    summary = summarize_product_reviews(db, product.id)
    db.commit()
    db.refresh(observation)

    assert summary["average_rating"] == 4.5
    assert summary["review_count"] == 37
    assert summary["ai_summary_text"] is None
    assert "review-text evidence was insufficient" in summary["evidence_limitation"]
    assert observation.normalized_payload["review_summary"]["summary_model"] == "aggregate-only-no-synthesis"


def test_source_listing_review_summary_is_persisted_and_reused(db, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    brand = Brand(id=uuid.uuid4(), name="Source Brand", normalized_name=uuid.uuid4().hex)
    product = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Source Lip", normalized_name="sourcelip")
    job = ImportJob(id=uuid.uuid4(), filename="reviews.csv", source_name="Company Feed", file_hash=uuid.uuid4().hex,
                    status="completed", column_mapping={"rating": "stars", "review_count": "reviews"})
    listing = SourceListing(id=uuid.uuid4(), import_job_id=job.id, canonical_product_id=product.id,
                            raw_data={"stars": "4.8", "reviews": "210"}, source_hash=uuid.uuid4().hex)
    db.add_all([brand, job]); db.flush(); db.add(product); db.flush(); db.add(listing); db.commit()
    summary = summarize_product_reviews(db, product.id)
    db.commit()
    saved = db.query(FieldValue).filter(FieldValue.canonical_product_id == product.id,
                                        FieldValue.field_name == "review_summary", FieldValue.is_current == True).one()
    selected = select_review_aggregate(db, product.id)
    assert summary == saved.value
    assert selected["review_summary"]["summary"] is None
    assert selected["average_rating"] == 4.8
