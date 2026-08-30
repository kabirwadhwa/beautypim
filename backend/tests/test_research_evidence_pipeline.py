import json
import uuid
from unittest.mock import patch

from app.models import (
    Brand, CanonicalProduct, CrawlJob, FieldValue, Formulation, FormulationIngredient, ImportJob,
    ImportJobItem, ProductVariant, SourceListing, User, ValidationIssue,
)
from app.services.product_research_worker import _enforce_discovery_exclusions, _persist_discovery_evidence
from app.routes.products import ProductImproveRequest, _enqueue_product_research
from app.services.research_evidence import (
    EvidenceItem, persist_evidence_item, research_fingerprint, requested_scope, validate_evidence,
)
from app.services.research_reliability import (
    build_identity_query_plan, blank_source_memory, record_source_attempt, source_should_be_skipped,
)
from app.services.web_discovery import _parse_openai_evidence_claims


def _product(db, *, gtin="7611773160681", name="Pure Gold Radiance Cream"):
    brand = Brand(id=uuid.uuid4(), name="La Prairie", normalized_name=f"la-prairie-{uuid.uuid4()}")
    product = CanonicalProduct(
        id=uuid.uuid4(), brand=brand, product_name=name,
        normalized_name=name.lower(), review_status="imported",
    )
    variant = ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id, gtin=gtin, size="50 ml")
    db.add_all([brand, product, variant]); db.flush()
    return product, variant


def _claim(**overrides):
    value = {
        "field_name": "inci", "proposed_value": "Water, Glycerin",
        "source_url": "https://www.marionnaud.example/pure-gold", "source_title": "Pure Gold",
        "source_authority": "authorized_retailer", "matched_gtin": "7611773160681",
        "matched_brand": "La Prairie", "matched_product_name": "Pure Gold Radiance Cream",
        "matched_product_family": "Pure Gold", "matched_variant": "50 ml",
        "matched_concentration": None, "matched_shade": None, "matched_size": "50 ml",
        "evidence_type": "ingredients", "evidence_excerpt": "Ingredients: Water, Glycerin",
    }
    value.update(overrides)
    return value


@patch("app.services.web_discovery.validate_public_url")
def test_licensed_search_claim_requires_real_provider_citation(validate):
    claim = _claim()
    payload = {"output": [{"type": "message", "content": [{
        "type": "output_text", "text": json.dumps({"candidate_pages": [], "market_observations": [], "evidence_claims": [claim]})
    }]}]}
    assert _parse_openai_evidence_claims(payload, []) == []
    payload["output"].insert(0, {"type": "web_search_call", "action": {"sources": [{"url": claim["source_url"]}]}})
    parsed = _parse_openai_evidence_claims(payload, [])
    assert len(parsed) == 1
    assert parsed[0]["acquisition_method"] == "licensed_web_search"


@patch("app.services.research_evidence.validate_public_url")
def test_variant_specific_claim_rejects_sibling_and_family_scope(validate, db):
    product, variant = _product(db)
    sibling = EvidenceItem(
        field_name="inci", proposed_value="Alcohol", source_url="https://retailer.example/edp",
        source_domain="retailer.example", acquisition_method="licensed_web_search",
        matched_gtin="9999999999999", matched_brand="La Prairie",
        matched_product_name=product.product_name, matched_variant="EDP",
        evidence_excerpt="Ingredients: Alcohol",
    )
    assert validate_evidence(sibling, product, variant)[1].startswith("insufficient_identity_scope")
    family = EvidenceItem(
        field_name="inci", proposed_value="Alcohol", source_url="https://retailer.example/family",
        source_domain="retailer.example", acquisition_method="licensed_web_search",
        matched_brand="La Prairie", matched_product_name="Pure Gold",
        evidence_excerpt="Ingredients: Alcohol",
    )
    assert validate_evidence(family, product, variant)[0] is False


@patch("app.services.research_evidence.validate_public_url")
def test_cited_exact_gtin_inci_survives_blocked_crawler_as_formulation(validate, db):
    product, variant = _product(db)
    job = CrawlJob(id=uuid.uuid4(), domain="product-research.internal", starting_urls=[],
                   crawl_mode="single_url", status="crawling", configuration={})
    db.add(job); db.flush()
    discovery = {"provider": "openai", "response_id": "resp-1", "evidence_claims": [
        {**_claim(), "source_domain": "www.marionnaud.example", "acquisition_method": "licensed_web_search"}
    ]}
    result = _persist_discovery_evidence(db, job, product, variant, discovery, ["inci"])
    db.flush()
    assert result["fields_resolved"] == ["inci"]
    formulation = db.query(Formulation).filter(Formulation.product_variant_id == variant.id).one()
    assert formulation.raw_inci_text == "Water, Glycerin"
    assert db.query(FormulationIngredient).filter(FormulationIngredient.formulation_id == formulation.id).count() == 2


def test_exact_identity_queries_never_include_unrelated_corpus_analogues():
    plan = build_identity_query_plan(
        brand="La Prairie", product_name="Pure Gold Radiance Cream", gtin="7611773160681",
        corpus_candidates=[{"brand": "Clarins", "product_name": "Double Serum"},
                           {"brand": "4711", "product_name": "Original Eau de Cologne"}],
    )
    rendered = " ".join(row["query"] for row in plan)
    assert plan[0] == {"strategy": "exact_gtin", "query": "7611773160681"}
    assert "Clarins" not in rendered and "4711" not in rendered
    assert not any(row["strategy"] == "corpus_identity" for row in plan)


def test_negative_source_memory_and_domain_circuit_breaker():
    memory = blank_source_memory()
    url = "https://shop.example/product?a=1#reviews"
    memory = record_source_attempt(memory, url, outcome="failed", reason="blocked_403", max_domain_failures=2)
    assert source_should_be_skipped(memory, url) == (True, "previous_blocked_403")
    memory = record_source_attempt(memory, "https://shop.example/other", outcome="failed", reason="blocked_403", max_domain_failures=2)
    assert "shop.example" in memory["blocked_domains"]
    assert source_should_be_skipped(memory, "https://shop.example/new")[1] == "domain_circuit_open"


def test_research_fingerprint_shares_family_fields_but_isolates_variant_fields():
    product_id, first, second = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert requested_scope(["directions"])[0] == "family"
    assert research_fingerprint(product_id, first, ["directions"]) == research_fingerprint(product_id, second, ["directions"])
    assert requested_scope(["inci"])[0] == "variant"
    assert research_fingerprint(product_id, first, ["inci"]) != research_fingerprint(product_id, second, ["inci"])


@patch("app.services.research_evidence.validate_public_url")
def test_human_and_customer_source_precedence_cannot_be_overwritten(validate, db):
    product, variant = _product(db)
    job = CrawlJob(id=uuid.uuid4(), domain="product-research.internal", starting_urls=[],
                   crawl_mode="single_url", status="crawling", configuration={})
    human = FieldValue(
        id=uuid.uuid4(), canonical_product_id=product.id, field_name="description", value="Human copy",
        source_type="human_edit", review_status="confirmed", is_current=True,
    )
    db.add_all([job, human]); db.flush()
    item = EvidenceItem(
        field_name="description", proposed_value="Retail copy", source_url="https://retailer.example/product",
        source_domain="retailer.example", acquisition_method="licensed_web_search",
        matched_brand="La Prairie", matched_product_name=product.product_name,
        evidence_excerpt="Retail copy",
    )
    assert persist_evidence_item(db, job, product, variant, item) == (False, "protected_human_edit")
    human.is_current = False; db.flush()
    source = FieldValue(
        id=uuid.uuid4(), canonical_product_id=product.id, field_name="description", value="Customer copy",
        source_type="source_data", review_status="confirmed", is_current=True,
        evidence=[{"evidence_type": "explicit_customer_source", "import_job_id": str(uuid.uuid4())}],
    )
    db.add(source); db.flush()
    assert persist_evidence_item(db, job, product, variant, item) == (False, "protected_customer_source")


def test_alternative_pass_enforces_url_and_domain_exclusions():
    discovery = {
        "candidates": [
            {"url": "https://blocked.example/a"}, {"url": "https://new.example/a"},
        ],
        "evidence_claims": [
            {"source_url": "https://attempted.example/old"}, {"source_url": "https://new.example/a"},
        ],
        "market_observations": [{"source_url": "https://blocked.example/b"}],
    }
    filtered, rejected = _enforce_discovery_exclusions(discovery, {
        "attempted_urls": ["https://attempted.example/old"], "blocked_domains": ["blocked.example"],
    })
    assert filtered["candidates"] == [{"url": "https://new.example/a"}]
    assert filtered["evidence_claims"] == [{"source_url": "https://new.example/a"}]
    assert {row["reason"] for row in rejected} == {"excluded_url", "blocked_domain"}


@patch("app.services.research_evidence.validate_public_url")
def test_contradictory_exact_evidence_creates_review_state(validate, db):
    product, variant = _product(db)
    job = CrawlJob(id=uuid.uuid4(), domain="product-research.internal", starting_urls=[],
                   crawl_mode="single_url", status="crawling", configuration={})
    db.add_all([job, FieldValue(
        id=uuid.uuid4(), canonical_product_id=product.id, field_name="description",
        value="Official description A", source_type="source_data", review_status="inferred",
        is_current=True, evidence=[{"source_authority": "official_brand"}],
    )]); db.flush()
    item = EvidenceItem(
        field_name="description", proposed_value="Retail description B",
        source_url="https://retailer.example/product", source_domain="retailer.example",
        acquisition_method="licensed_web_search", source_authority="retailer",
        matched_gtin=variant.gtin, matched_brand="La Prairie", matched_product_name=product.product_name,
        evidence_excerpt="Retail description B",
    )
    assert persist_evidence_item(db, job, product, variant, item)[1] == "conflicting_evidence_review_required"
    assert db.query(ValidationIssue).filter(ValidationIssue.issue_type == "research_evidence_conflict").count() == 1


def test_concurrent_identical_enqueue_reuses_one_active_job(db):
    product, variant = _product(db)
    user = User(id=uuid.uuid4(), email=f"fingerprint-{uuid.uuid4()}@test.com", hashed_password="x", role="admin", is_active=True)
    import_job = ImportJob(id=uuid.uuid4(), filename="product.csv", file_hash=uuid.uuid4().hex,
                           status="completed", column_mapping={}, total_rows=1, processed_rows=1)
    listing = SourceListing(id=uuid.uuid4(), import_job_id=import_job.id, canonical_product_id=product.id,
                            product_variant_id=variant.id, raw_data={}, source_hash=uuid.uuid4().hex)
    item = ImportJobItem(id=uuid.uuid4(), import_job_id=import_job.id, source_row_number=1,
                         source_listing_id=listing.id, canonical_product_id=product.id,
                         product_variant_id=variant.id, status="completed", match_status="exact_match")
    db.add_all([user, import_job]); db.flush(); db.add(listing); db.flush(); db.add(item); db.flush()
    first = _enqueue_product_research(db, product, item, ProductImproveRequest(), user, ["inci"])
    second = _enqueue_product_research(db, product, item, ProductImproveRequest(), user, ["inci"])
    assert first.id == second.id
    assert db.query(CrawlJob).filter(CrawlJob.domain == "product-research.internal").count() == 1
