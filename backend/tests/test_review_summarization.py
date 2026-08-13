import uuid

from app.config import settings
from app.models import (
    Brand, CanonicalProduct, CrawlJob, CrawlUrl, RawPageObservation,
    ScrapedProductObservation,
)
from app.services.review_summarization import summarize_product_reviews


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
        structured_hash=uuid.uuid4().hex, match_status="matched", adapter_name="test",
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
