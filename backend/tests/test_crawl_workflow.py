import uuid
import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from app.models import (
    Brand, CanonicalProduct, CrawlConflict, CrawlJob, CrawlUrl, FieldValue,
    Formulation, ProductVariant, RawPageObservation, ScrapedProductObservation,
    SourcePrice,
)
from app.scraping.adapters.generic import GenericJsonLdAdapter
from app.scraping.persistence import persist_product
from app.scraping.runner import enqueue_url
from app.scraping.schemas import CrawlConfiguration, ScrapedProduct
from app.scraping.worker import recover_stale_jobs, schedule_due_recrawls
from app.scraping.fetcher import FetchResult
from app.scraping.runner import run_crawl_job
from app.services.review_aggregate import select_review_aggregate
from app.services.review_summarization import summarize_product_reviews
from pathlib import Path


def token(client, email):
    response = client.post("/api/auth/token", data={
        "username": email, "password": "securepassword123",
    })
    return response.json()["access_token"]


@patch("app.routes.crawls.validate_public_url", return_value="shop.example.com")
def test_crawl_api_permissions_controls_and_rate_limit(_, client):
    admin = {"Authorization": f"Bearer {token(client, 'admin@test.com')}"}
    editor = {"Authorization": f"Bearer {token(client, 'editor@test.com')}"}
    viewer = {"Authorization": f"Bearer {token(client, 'viewer@test.com')}"}
    payload = {
        "domain": "shop.example.com",
        "starting_urls": ["https://shop.example.com/category/skin"],
        "crawl_mode": "full_domain",
    }
    assert client.post("/api/crawl-jobs", json=payload, headers=viewer).status_code == 403
    assert client.post("/api/crawl-jobs", json=payload, headers=editor).status_code == 403
    created = client.post("/api/crawl-jobs", json=payload, headers=admin)
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    assert client.post(f"/api/crawl-jobs/{job_id}/pause", headers=admin).status_code == 200
    assert client.post(f"/api/crawl-jobs/{job_id}/resume", headers=admin).status_code == 200
    assert client.post(f"/api/crawl-jobs/{job_id}/cancel", headers=admin).status_code == 200
    assert client.get(f"/api/crawl-jobs/{job_id}", headers=viewer).status_code == 200


@patch("app.scraping.runner.validate_public_url", return_value="shop.example.com")
def test_frontier_dedup_depth_patterns_and_limits(_, db):
    config = CrawlConfiguration(
        domain="shop.example.com", crawl_mode="full_domain",
        maximum_crawl_depth=1, maximum_discovered_urls=2,
        denied_url_patterns=[r"/blog/"],
    )
    job = CrawlJob(
        id=uuid.uuid4(), domain=config.domain, starting_urls=[],
        crawl_mode=config.crawl_mode, status="queued",
        configuration=config.model_dump(mode="json"),
    )
    db.add(job); db.flush()
    assert enqueue_url(db, job, "https://shop.example.com/product/a?utm_source=x")
    assert not enqueue_url(db, job, "https://shop.example.com/product/a")
    assert not enqueue_url(db, job, "https://shop.example.com/blog/story")
    assert not enqueue_url(db, job, "https://shop.example.com/product/deep", depth=2)
    assert enqueue_url(db, job, "https://shop.example.com/product/b")
    assert not enqueue_url(db, job, "https://shop.example.com/product/c")
    assert db.query(CrawlUrl).filter(CrawlUrl.crawl_job_id == job.id).count() == 2


def test_persistence_is_idempotent_and_preserves_approved_conflict(db):
    brand = Brand(id=uuid.uuid4(), name="Lunar Atelier", normalized_name="lunar atelier")
    db.add(brand); db.flush()
    canonical = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, product_name="Moon Glass Barrier Serum",
        normalized_name="moon glass barrier serum", review_status="approved",
    )
    db.add(canonical); db.flush()
    db.add(FieldValue(
        id=uuid.uuid4(), canonical_product_id=canonical.id,
        field_name="description", value="Approved description",
        source_type="human_edit", review_status="confirmed", is_current=True,
    ))
    config = CrawlConfiguration(
        domain="shop.example.com", crawl_mode="single_url",
        starting_urls=["https://shop.example.com/product/moon"],
    )
    job = CrawlJob(
        id=uuid.uuid4(), domain=config.domain,
        starting_urls=["https://shop.example.com/product/moon"],
        crawl_mode=config.crawl_mode, status="parsing",
        configuration=config.model_dump(mode="json"),
    )
    url = CrawlUrl(
        id=uuid.uuid4(), crawl_job_id=job.id,
        url=config.starting_urls[0], normalized_url=config.starting_urls[0],
        state="fetching", depth=0,
    )
    db.add_all([job, url]); db.flush()
    # A previous exact retailer aggregate must not turn a later retailer's
    # independently valid aggregate/review texts into an identity conflict.
    db.add_all([
        FieldValue(
            id=uuid.uuid4(), canonical_product_id=canonical.id,
            field_name="rating", value=4.9, source_type="source_data",
            source_reference="https://other-retailer.example/signature",
            confidence_score=1, review_status="inferred", is_current=True,
        ),
        FieldValue(
            id=uuid.uuid4(), canonical_product_id=canonical.id,
            field_name="review_count", value=100, source_type="source_data",
            source_reference="https://other-retailer.example/signature",
            confidence_score=1, review_status="inferred", is_current=True,
        ),
    ])
    raw = RawPageObservation(
        id=uuid.uuid4(), crawl_job_id=job.id, crawl_url_id=url.id,
        source_url=url.url, final_url=url.url, http_status=200,
        content_hash="a" * 64, response_size=100, parser_version="1.0.0",
    )
    db.add(raw); db.flush()
    product = ScrapedProduct(
        source_name="Example", source_domain=config.domain,
        source_url=url.url, canonical_url=url.url,
        scraped_at=datetime.now(timezone.utc), brand=brand.name,
        product_name=canonical.product_name, gtin="3760000012345",
        description="New retailer description", price=Decimal("42.90"),
        currency="EUR", ingredient_text_raw="Aqua, Niacinamide, Ceramide NP",
        parser_version="1.0.0",
    )
    adapter = GenericJsonLdAdapter()
    first = persist_product(db, job, raw, product, adapter)
    second = persist_product(db, job, raw, product, adapter)
    db.commit()
    assert first.id == second.id
    assert db.query(ScrapedProductObservation).count() == 1
    conflict = db.query(CrawlConflict).filter(CrawlConflict.field_name == "description").one()
    assert conflict.current_value == "Approved description"
    assert db.query(FieldValue).filter(
        FieldValue.canonical_product_id == canonical.id,
        FieldValue.field_name == "description",
        FieldValue.is_current.is_(True),
    ).one().value == "Approved description"


def test_product_research_attaches_observation_and_image_to_requested_product(db):
    brand = Brand(id=uuid.uuid4(), name="Burberry", normalized_name="burberry")
    db.add(brand); db.flush()
    canonical = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, product_name="Burberry Goddess",
        normalized_name="burberry goddess", review_status="imported",
    )
    db.add(canonical); db.flush()
    config = CrawlConfiguration(
        domain="brand.example", crawl_mode="single_url",
        starting_urls=["https://brand.example/products/goddess"],
    ).model_dump(mode="json")
    config["research_product_id"] = str(canonical.id)
    job = CrawlJob(
        id=uuid.uuid4(), domain="brand.example",
        starting_urls=config["starting_urls"], crawl_mode="single_url",
        status="parsing", configuration=config,
    )
    url = CrawlUrl(
        id=uuid.uuid4(), crawl_job_id=job.id, url=config["starting_urls"][0],
        normalized_url=config["starting_urls"][0], state="fetching", depth=0,
    )
    db.add_all([job, url]); db.flush()
    raw = RawPageObservation(
        id=uuid.uuid4(), crawl_job_id=job.id, crawl_url_id=url.id,
        source_url=url.url, final_url=url.url, http_status=200,
        content_hash="b" * 64, response_size=100, parser_version="1.0.0",
    )
    db.add(raw); db.flush()
    scraped = ScrapedProduct(
        source_name="Official Brand", source_domain="brand.example",
        source_url=url.url, canonical_url=url.url,
        scraped_at=datetime.now(timezone.utc), brand="BURBERRY",
        product_name="Goddess Eau de Parfum for Women",
        description="A vanilla-led eau de parfum.",
        gtin="3614226905018", variant_name="100 ml", size="100", unit="ml",
        image_urls=["https://cdn.brand.example/goddess.jpg"],
        parser_version="1.0.0",
    )
    observation = persist_product(db, job, raw, scraped, GenericJsonLdAdapter())
    db.commit(); db.refresh(canonical)

    assert observation.canonical_product_id == canonical.id
    assert observation.match_status == "matched"
    assert canonical.image_url == "https://cdn.brand.example/goddess.jpg"
    variant = db.query(ProductVariant).filter(ProductVariant.canonical_product_id == canonical.id).one()
    assert variant.gtin == "3614226905018"
    assert variant.variant_name == "100 ml"
    assert variant.size == "100"
    assert variant.unit == "ml"
    assert db.query(Formulation).filter(Formulation.canonical_product_id == canonical.id).count() == 0
    assert db.query(CanonicalProduct).filter(CanonicalProduct.product_name.like("Goddess Eau%")).count() == 0


def test_review_text_survives_parse_persistence_selector_and_summary(db, monkeypatch):
    """Regression: written reviews must not disappear between crawl and synthesis."""
    from app.config import settings
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    brand = Brand(id=uuid.uuid4(), name="La Prairie", normalized_name="la prairie")
    canonical = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, product_name="Pure Gold Radiance Cream",
        normalized_name="pure gold radiance cream", review_status="imported",
    )
    db.add_all([brand, canonical]); db.flush()
    source_url = "https://retailer.example/pure-gold/reviews"
    config = CrawlConfiguration(
        domain="retailer.example", crawl_mode="single_url", starting_urls=[source_url],
    ).model_dump(mode="json")
    config["research_product_id"] = str(canonical.id)
    job = CrawlJob(
        id=uuid.uuid4(), domain="retailer.example", starting_urls=[source_url],
        crawl_mode="single_url", status="parsing", configuration=config,
    )
    crawl_url = CrawlUrl(
        id=uuid.uuid4(), crawl_job_id=job.id, url=source_url,
        normalized_url=source_url, state="fetching", depth=0,
    )
    db.add_all([job, crawl_url]); db.flush()
    raw = RawPageObservation(
        id=uuid.uuid4(), crawl_job_id=job.id, crawl_url_id=crawl_url.id,
        source_url=source_url, final_url=source_url, http_status=200,
        content_hash=uuid.uuid4().hex, response_size=100, parser_version="test",
    )
    db.add(raw); db.flush()
    html = """
    <script type="application/ld+json">{"@type":"Product","name":"Pure Gold Radiance Cream",
      "brand":{"name":"La Prairie"},"aggregateRating":{"ratingValue":"5","reviewCount":"3"},
      "review":[{"@type":"Review","reviewBody":"The cream feels rich yet comfortable and leaves a luminous finish.",
                 "reviewRating":{"ratingValue":"5"}}]}</script>
    """
    adapter = GenericJsonLdAdapter()
    scraped = adapter.parse(html, source_url)
    observation = persist_product(db, job, raw, scraped, adapter)
    db.commit()

    assert len(observation.normalized_payload["review_samples"]) == 1
    assert observation.normalized_payload["review_summary"]["review_sample_count"] == 1
    selected = select_review_aggregate(db, canonical.id)
    assert selected["review_sample_count"] == 1
    assert selected["review_summary"]["review_samples"][0]["text"] == (
        "The cream feels rich yet comfortable and leaves a luminous finish."
    )
    summarized = summarize_product_reviews(db, canonical.id)
    assert summarized["review_sample_count"] == 1
    assert summarized["ai_summary_text"]


def test_product_research_rejects_conflicting_fragrance_edition(db):
    brand = Brand(id=uuid.uuid4(), name="Dior", normalized_name="dior")
    db.add(brand); db.flush()
    canonical = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, product_name="Dior Sauvage",
        normalized_name="dior sauvage", review_status="imported",
    )
    db.add(canonical); db.flush()
    config = CrawlConfiguration(
        domain="retailer.example", crawl_mode="single_url",
        starting_urls=["https://retailer.example/sauvage-parfum"],
    ).model_dump(mode="json")
    config.update({
        "research_product_id": str(canonical.id),
        "research_expected_format": "Perfume Eau de Toilette",
    })
    job = CrawlJob(
        id=uuid.uuid4(), domain="retailer.example", starting_urls=config["starting_urls"],
        crawl_mode="single_url", status="parsing", configuration=config,
    )
    url = CrawlUrl(
        id=uuid.uuid4(), crawl_job_id=job.id, url=config["starting_urls"][0],
        normalized_url=config["starting_urls"][0], state="fetching", depth=0,
    )
    db.add_all([job, url]); db.flush()
    raw = RawPageObservation(
        id=uuid.uuid4(), crawl_job_id=job.id, crawl_url_id=url.id,
        source_url=url.url, final_url=url.url, http_status=200,
        content_hash="c" * 64, response_size=100, parser_version="1.0.0",
    )
    db.add(raw); db.flush()
    scraped = ScrapedProduct(
        source_name="Retailer", source_domain="retailer.example",
        source_url=url.url, canonical_url=url.url,
        scraped_at=datetime.now(timezone.utc), brand="Dior",
        product_name="Sauvage Parfum - 1.0 oz", size="1.0 oz",
        parser_version="1.0.0",
    )

    with pytest.raises(ValueError, match="conflicting product edition"):
        persist_product(db, job, raw, scraped, GenericJsonLdAdapter())

    assert db.query(ScrapedProductObservation).count() == 0


def test_unresolved_fragrance_research_keeps_image_and_reviews_but_blocks_exact_facts(db):
    brand = Brand(id=uuid.uuid4(), name="Maison Test", normalized_name="maisontest")
    canonical = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, product_name="Signature Scent",
        normalized_name="signature scent", review_status="imported",
    )
    db.add_all([brand, canonical]); db.flush()
    config = CrawlConfiguration(
        domain="retailer.example", crawl_mode="single_url",
        starting_urls=["https://retailer.example/signature-scent"],
    ).model_dump(mode="json")
    config.update({
        "research_product_id": str(canonical.id),
        "research_product_name": canonical.product_name,
        "research_expected_format": "Fragrance",
        "research_safe_fields_only": True,
    })
    job = CrawlJob(
        id=uuid.uuid4(), domain="retailer.example", starting_urls=config["starting_urls"],
        crawl_mode="single_url", status="parsing", configuration=config,
    )
    url = CrawlUrl(
        id=uuid.uuid4(), crawl_job_id=job.id, url=config["starting_urls"][0],
        normalized_url=config["starting_urls"][0], state="fetching", depth=0,
    )
    db.add_all([job, url]); db.flush()
    raw = RawPageObservation(
        id=uuid.uuid4(), crawl_job_id=job.id, crawl_url_id=url.id,
        source_url=url.url, final_url=url.url, http_status=200,
        content_hash="d" * 64, response_size=100, parser_version="1.0.0",
    )
    db.add(raw); db.flush()
    scraped = ScrapedProduct(
        source_name="Retailer", source_domain="retailer.example",
        source_url=url.url, canonical_url=url.url,
        scraped_at=datetime.now(timezone.utc), brand="Maison Test",
        product_name="Signature Scent Eau de Parfum", gtin="1234567890123",
        variant_name="EDP 100 ml", size="100", unit="ml", price=Decimal("79.00"),
        image_urls=["https://retailer.example/signature.jpg"],
        ingredient_text_raw="Alcohol, Parfum, Limonene",
        claims=["Long-lasting"], rating=4.6, review_count=321,
        review_summary={"average_rating": 4.6, "review_count": 321},
        parser_version="1.0.0",
    )

    persist_product(db, job, raw, scraped, GenericJsonLdAdapter())
    db.commit(); db.refresh(canonical)

    assert canonical.image_url == "https://retailer.example/signature.jpg"
    saved_fields = {row.field_name for row in db.query(FieldValue).filter(
        FieldValue.canonical_product_id == canonical.id,
    ).all()}
    # Market/review values remain append-only observations rather than
    # conflicting canonical product fields.
    assert "review_summary" not in saved_fields
    observation = db.query(ScrapedProductObservation).filter(
        ScrapedProductObservation.canonical_product_id == canonical.id,
    ).one()
    assert observation.match_status == "matched"
    assert float(observation.normalized_payload["rating"]) == 4.6
    assert observation.normalized_payload["review_count"] == 321
    assert db.query(CrawlConflict).filter(
        CrawlConflict.scraped_product_id == observation.id,
    ).count() == 0
    assert "claims" not in saved_fields
    variant = db.query(ProductVariant).filter(ProductVariant.canonical_product_id == canonical.id).one()
    assert variant.gtin is None and variant.variant_name is None and variant.size is None
    assert db.query(Formulation).filter(Formulation.canonical_product_id == canonical.id).count() == 0
    assert db.query(SourcePrice).count() == 0


def test_worker_recovers_interrupted_frontier(db):
    config = CrawlConfiguration(domain="shop.example.com", crawl_mode="full_domain")
    job = CrawlJob(
        id=uuid.uuid4(), domain=config.domain, starting_urls=[],
        crawl_mode=config.crawl_mode, status="crawling",
        configuration=config.model_dump(mode="json"),
        heartbeat_at=datetime.utcnow() - timedelta(hours=1),
    )
    item = CrawlUrl(
        id=uuid.uuid4(), crawl_job_id=job.id, url="https://shop.example.com/p/a",
        normalized_url="https://shop.example.com/p/a", state="fetching", depth=0,
    )
    db.add_all([job, item]); db.commit()
    recover_stale_jobs(db)
    db.refresh(job); db.refresh(item)
    assert job.status == "queued"
    assert item.state == "queued"


@patch("app.scraping.runner.time.sleep")
@patch("app.scraping.runner.validate_public_url", return_value="shop.example.com")
@patch("app.scraping.runner.fetch")
def test_complete_single_url_vertical_slice(mock_fetch, _, __, db):
    html = (Path(__file__).parent / "fixtures" / "crawl" / "generic-product.html").read_bytes()
    mock_fetch.side_effect = [
        FetchResult(
            "https://shop.example.com/robots.txt", "https://shop.example.com/robots.txt",
            200, {"content-type": "text/plain"}, b"User-agent: *\nAllow: /\n",
        ),
        FetchResult(
            "https://shop.example.com/product/moon-serum",
            "https://shop.example.com/product/moon-serum", 200,
            {"content-type": "text/html", "etag": '"fixture-v1"'}, html,
        ),
    ]
    config = CrawlConfiguration(
        domain="shop.example.com", crawl_mode="single_url",
        starting_urls=["https://shop.example.com/product/moon-serum"],
        request_delay_seconds=0.25, use_sitemap=False,
    )
    job = CrawlJob(
        id=uuid.uuid4(), domain=config.domain, starting_urls=config.starting_urls,
        crawl_mode=config.crawl_mode, status="queued",
        configuration=config.model_dump(mode="json"),
    )
    db.add(job); db.commit()
    run_crawl_job(db, job.id)
    db.refresh(job)
    assert job.status == "completed"
    assert job.pages_fetched == 1
    assert job.product_pages_found == 1
    assert job.products_persisted == 1
    observation = db.query(ScrapedProductObservation).one()
    assert observation.normalized_payload["product_name"] == "Moon Glass Barrier Serum"
    assert observation.source_url == "https://shop.example.com/product/moon-serum"
    assert observation.canonical_product_id is not None


def test_scheduled_product_recrawl_requeues_only_product_pages(db):
    config = CrawlConfiguration(
        domain="shop.example.com", crawl_mode="full_domain",
        rescrape_interval_hours=1, recrawl_strategy="product_pages_only",
    )
    job = CrawlJob(
        id=uuid.uuid4(), domain=config.domain, starting_urls=[],
        crawl_mode=config.crawl_mode, status="completed",
        configuration=config.model_dump(mode="json"),
        completed_at=datetime.utcnow() - timedelta(hours=2),
    )
    product = CrawlUrl(
        id=uuid.uuid4(), crawl_job_id=job.id, url="https://shop.example.com/p/a",
        normalized_url="https://shop.example.com/p/a", state="completed",
        page_type="product", depth=1,
    )
    category = CrawlUrl(
        id=uuid.uuid4(), crawl_job_id=job.id, url="https://shop.example.com/c/skin",
        normalized_url="https://shop.example.com/c/skin", state="completed",
        page_type="category", depth=0,
    )
    db.add_all([job, product, category]); db.commit()
    schedule_due_recrawls(db)
    db.refresh(job); db.refresh(product); db.refresh(category)
    assert job.status == "queued"
    assert product.state == "queued"
    assert category.state == "completed"
