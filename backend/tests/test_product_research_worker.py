import uuid
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (
    Brand, CanonicalProduct, CrawlJob, FieldValue, ImportJob, ImportJobItem, ProductVariant,
    SourceListing, User,
)
from app.services.product_research_worker import (
    _claim_job, _persist_discovery_market_evidence,
    recover_product_research_jobs, run_product_research_job, start_product_research_worker,
)
from app.config import settings


def test_web_research_uses_provider_specific_concurrency(monkeypatch):
    monkeypatch.setattr(settings, "MAX_CONCURRENCY", 4)
    monkeypatch.setattr(settings, "OPENAI_WEB_RESEARCH_CONCURRENCY", 1)
    with patch("app.services.product_research_worker.threading.Thread") as thread:
        stop, workers = start_product_research_worker()
    assert len(workers) == 1
    assert thread.call_count == 1
    stop.set()


def test_restart_recovery_preserves_paid_response_id(db):
    job = CrawlJob(
        id=uuid.uuid4(), domain="product-research.internal", starting_urls=[],
        crawl_mode="single_url", status="discovering",
        configuration={
            "product_research_job": True,
            "discovery": {"provider": "openai", "response_id": "resp_keep_me", "status": "in_progress"},
        },
    )
    db.add(job)
    db.commit()

    assert recover_product_research_jobs(db) == 1
    db.refresh(job)
    assert job.status == "queued"
    assert job.configuration["discovery"]["response_id"] == "resp_keep_me"


def test_direct_improve_priority_jumps_bulk_research_backlog(db):
    bulk = CrawlJob(
        id=uuid.uuid4(), domain="product-research.internal", starting_urls=[],
        crawl_mode="single_url", status="queued",
        configuration={"product_research_job": True, "research_priority": 10},
    )
    direct = CrawlJob(
        id=uuid.uuid4(), domain="product-research.internal", starting_urls=[],
        crawl_mode="single_url", status="queued",
        configuration={"product_research_job": True, "research_priority": 100},
    )
    db.add_all([bulk, direct]); db.commit()

    claimed = _claim_job(db)

    assert claimed.id == direct.id
    assert claimed.status == "discovering"


def test_market_evidence_rejects_sibling_gtin(db):
    brand = Brand(id=uuid.uuid4(), name="Lattafa", normalized_name=f"lattafa-{uuid.uuid4()}")
    product = CanonicalProduct(
        id=uuid.uuid4(), brand=brand, product_name="Ana Abiyedh",
        normalized_name="ana abiyedh", review_status="imported",
    )
    db.add_all([brand, product]); db.flush()
    db.add(ProductVariant(
        id=uuid.uuid4(), canonical_product_id=product.id, gtin="6291106066890",
    ))
    db.commit()

    rejected = _persist_discovery_market_evidence(db, product, {"market_observations": [{
        "source_url": "https://retailer.example/coral",
        "source_domain": "retailer.example", "matched_gtin": "6290362341826",
        "matched_brand": "Lattafa", "matched_product_name": "Ana Abiyedh Coral",
        "image_url": "https://cdn.example/coral.jpg", "average_rating": 4.9,
        "review_count": 8000,
    }]}, expected_gtin="6291106066890")

    assert rejected == {"image_found": False, "review_evidence_found": False}
    assert product.image_url is None
    assert not db.query(FieldValue).filter(FieldValue.canonical_product_id == product.id).count()

    accepted = _persist_discovery_market_evidence(db, product, {"market_observations": [{
        "source_url": "https://retailer.example/original",
        "source_domain": "retailer.example", "matched_gtin": "6291106066890",
        "matched_brand": "Lattafa", "matched_product_name": "Ana Abiyedh",
        "image_url": "https://cdn.example/original.jpg", "average_rating": 4.8,
        "review_count": 12,
    }]}, expected_gtin="6291106066890")

    assert accepted == {"image_found": True, "review_evidence_found": True}
    assert product.image_url == "https://cdn.example/original.jpg"
    assert db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product.id,
        FieldValue.field_name == "review_count",
    ).one().value == 12


def test_resolved_identity_can_retain_exact_market_evidence_without_visible_gtin(db):
    brand = Brand(id=uuid.uuid4(), name="Lattafa", normalized_name=f"lattafa-{uuid.uuid4()}")
    product = CanonicalProduct(
        id=uuid.uuid4(), brand=brand, product_name="Khamrah Dukhan",
        normalized_name="khamrah dukhan", review_status="imported",
    )
    db.add_all([brand, product]); db.flush()
    db.add_all([
        ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id, gtin="6290362342373"),
        FieldValue(
            id=uuid.uuid4(), canonical_product_id=product.id, field_name="product_understanding",
            value={
                "identity_status": "resolved",
                "identity": {
                    "consumer_brand": {"value": "Lattafa"},
                    "product_family": {"value": "Khamrah Dukhan"},
                },
                "taxonomy": {"product_type": {"value": "Eau de Parfum"}},
            },
            source_type="deterministic_rule", review_status="confirmed", is_current=True,
        ),
    ])
    db.commit()

    accepted = _persist_discovery_market_evidence(db, product, {"market_observations": [{
        "source_url": "https://retailer.example/khamrah-dukhan",
        "source_domain": "retailer.example", "matched_gtin": None,
        "matched_brand": "Lattafa", "matched_product_name": "Lattafa Khamrah Dukhan",
        "matched_variant": "Eau de Parfum", "image_url": "https://cdn.example/dukhan.jpg",
        "average_rating": 4.6, "review_count": 84,
    }]}, expected_gtin="6290362342373")

    assert accepted == {"image_found": True, "review_evidence_found": True}
    rating = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product.id, FieldValue.field_name == "rating",
    ).one()
    assert rating.evidence[0]["match_scope"] == "exact_resolved_identity"


def test_background_research_persists_provider_id_and_completes(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'research-worker.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    brand = Brand(id=uuid.uuid4(), name="YSL", normalized_name="ysl")
    user = User(
        id=uuid.uuid4(), email="research@test.com", hashed_password="unused",
        role="admin", is_active=True,
    )
    product = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, product_name="Y",
        normalized_name="y", review_status="imported",
    )
    import_job = ImportJob(
        id=uuid.uuid4(), filename="ysl.csv", file_hash=uuid.uuid4().hex,
        status="completed", total_rows=1, processed_rows=1,
        column_mapping={"product_name": "name", "brand": "brand"},
    )
    listing = SourceListing(
        id=uuid.uuid4(), import_job_id=import_job.id,
        canonical_product_id=product.id, raw_data={"name": "Y", "brand": "YSL"},
        source_hash=uuid.uuid4().hex,
    )
    item = ImportJobItem(
        id=uuid.uuid4(), import_job_id=import_job.id, source_row_number=1,
        source_listing_id=listing.id, canonical_product_id=product.id,
        status="completed", match_status="new_product", enrichment_status="succeeded",
    )
    research = CrawlJob(
        id=uuid.uuid4(), domain="product-research.internal", starting_urls=[],
        crawl_mode="single_url", status="discovering", requested_by_id=user.id,
        configuration={
            "product_research_job": True, "research_product_id": str(product.id),
            "research_item_id": str(item.id), "requested_mode": "missing_only",
            "selected_fields": [], "research_objectives": ["inci"],
            "discovery": None, "result": None,
        },
    )
    db.add_all([brand, user, import_job])
    db.flush()
    db.add(product)
    db.flush()
    db.add_all([
        ProductVariant(
            id=uuid.uuid4(), canonical_product_id=product.id,
            size="3.4 oz", unit="3.4 oz",
        ),
        ProductVariant(
            id=uuid.uuid4(), canonical_product_id=product.id,
            gtin="3614271716026", size="100", unit="ml",
        ),
    ])
    db.flush()
    db.add(listing)
    db.flush()
    db.add_all([item, research])
    db.commit()
    research_id = research.id
    db.close()

    queued = {
        "provider": "openai", "response_id": "resp_ysl", "status": "queued",
        "domains": [], "model": "test", "candidates": [],
    }
    completed = {
        **queued, "status": "completed",
        "candidates": [{"url": "https://brand.example/y", "domain": "brand.example"}],
    }
    with (
        patch("app.services.product_research_worker.SessionLocal", Session),
        patch("app.services.web_discovery.start_product_source_discovery", return_value=queued) as start,
        patch("app.services.web_discovery.poll_product_source_discovery", return_value=completed) as poll,
        patch("app.routes.products._automatic_product_research", return_value={
            "candidates": 1, "sources_ingested": 1, "image_found": True,
            "review_evidence_found": True, "formulation_evidence_found": False,
            "errors": [],
        }),
        patch("app.worker.process_item_enrichment") as enrich,
    ):
        run_product_research_job(research_id)

    verify = Session()
    saved = verify.query(CrawlJob).filter(CrawlJob.id == research_id).one()
    assert saved.status == "completed"
    assert saved.configuration["discovery"]["response_id"] == "resp_ysl"
    assert saved.configuration["result"]["image_found"] is True
    assert saved.configuration["result"]["review_evidence_found"] is True
    start.assert_called_once()
    assert start.call_args.kwargs["research_objectives"] == ["inci"]
    assert start.call_args.kwargs["gtin"] == "3614271716026"
    poll.assert_called_once()
    enrich.assert_called_once()
    verify.close()
