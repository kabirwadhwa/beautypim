import uuid

from app.config import settings
from app.models import (
    Brand, CanonicalProduct, CrawlJob, CrawlUrl, FieldValue, ImportJob, RawPageObservation,
    ScrapedProductObservation, SourceListing,
)
from app.services.review_summarization import summarize_product_reviews
from app.services.review_aggregate import select_review_aggregate


def test_review_synthesis_persists_four_evidence_grounded_lines(db, monkeypatch):
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
        structured_hash=uuid.uuid4().hex, match_status="conflict", adapter_name="test",
        adapter_version="1", parser_version="1",
        normalized_payload={"rating": 4.7, "review_count": 128, "review_summary": {
            "average_rating": 4.7, "review_count": 128, "review_sample_count": 24,
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

    assert len(summary["ai_summary_lines"]) == 4
    assert "texture" in " ".join(summary["ai_summary_lines"]).lower()
    assert "value" in " ".join(summary["ai_summary_lines"]).lower()
    assert observation.normalized_payload["review_summary"]["summary_model"] == "deterministic-evidence-summary"
    selected = select_review_aggregate(db, product.id)
    assert selected["average_rating"] == 4.7
    assert selected["review_count"] == 128
    assert selected["evidence_reference"].endswith(str(observation.id))


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
    assert len(selected["review_summary"]["ai_summary_lines"]) == 4
    assert "does not invent customer opinions" in selected["review_summary"]["ai_summary_text"]


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
    assert len(summary["ai_summary_lines"]) == 4
    assert "4.5/5" in summary["ai_summary_lines"][0]
    assert observation.normalized_payload["review_summary"]["summary_model"] == "deterministic-evidence-summary"


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
    assert selected["review_summary"]["summary"] == saved.value["summary"]
    assert selected["average_rating"] == 4.8
