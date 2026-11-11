"""Tests for AI token/cost accounting, pricing resolution and budget control."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AiUsageEvent, Brand, CanonicalProduct, EnrichmentRun, ImportJob,
    ImportJobItem, ProductVariant, SourceListing,
)
from app.services import ai_pricing
from app.services.ai_pricing import (
    estimate_cost, normalize_gemini_usage, normalize_openai_usage,
    resolve_pricing,
)
from app.services.ai_usage import (
    backfill_usage_from_enrichment_runs, job_budget_exceeded, record_ai_usage,
    response_elapsed_ms, usage_for_import_job, usage_for_product,
    usage_for_research_jobs, usage_summary,
)


@pytest.fixture(autouse=True)
def _clean_pricing_caches():
    ai_pricing.reset_pricing_caches()
    yield
    ai_pricing.reset_pricing_caches()


# --------------------------------------------------------------------------
# Pricing resolution
# --------------------------------------------------------------------------

def test_pricing_reproduces_the_previously_hardcoded_rates():
    """The inline arithmetic this module replaced must stay reproducible.

    Historical rows were costed with these exact rates, so changing them would
    make old and new figures silently incomparable.
    """
    openai_cost, source = estimate_cost("gpt-4o-mini", 1000, 1000)
    assert source == "reference"
    assert openai_cost == Decimal("0.000750")  # (1000*0.00015 + 1000*0.0006)/1000

    gemini_cost, _ = estimate_cost("gemini-1.5-flash", 1000, 1000)
    assert gemini_cost == Decimal("0.000375")  # (1000*0.000075 + 1000*0.0003)/1000


def test_dated_model_snapshots_resolve_to_their_family_rate():
    """Providers append dates to model ids; those must not become unpriced."""
    pinned, source = estimate_cost("gpt-4o-mini-2024-07-18", 1000, 1000)
    assert source == "reference"
    assert pinned == Decimal("0.000750")


def test_longest_prefix_wins_so_mini_is_not_priced_as_full():
    """gpt-5-mini must not resolve against the gpt-5 entry."""
    assert resolve_pricing("gpt-5-mini").input_per_million == 0.25
    assert resolve_pricing("gpt-5").input_per_million == 1.25


def test_unknown_model_yields_no_cost_rather_than_zero():
    """A missing rate must read as 'not estimated', never as free."""
    cost, source = estimate_cost("some-unreleased-model", 5000, 5000)
    assert cost is None
    assert source == "unknown"


def test_operator_override_takes_precedence(monkeypatch):
    monkeypatch.setattr(
        settings, "AI_MODEL_PRICING_JSON",
        '{"gpt-4o-mini": {"input": 1.0, "output": 2.0}}',
    )
    ai_pricing.reset_pricing_caches()
    cost, source = estimate_cost("gpt-4o-mini", 1_000_000, 1_000_000)
    assert source == "override"
    assert cost == Decimal("3.000000")


def test_malformed_override_is_ignored_not_fatal(monkeypatch):
    """A bad config value must not take the application down."""
    monkeypatch.setattr(settings, "AI_MODEL_PRICING_JSON", "{not valid json")
    ai_pricing.reset_pricing_caches()
    cost, source = estimate_cost("gpt-4o-mini", 1000, 1000)
    assert source == "reference"
    assert cost == Decimal("0.000750")


def test_cached_prompt_tokens_bill_at_the_cached_rate():
    full, _ = estimate_cost("gpt-4o-mini", 1_000_000, 0)
    cached, _ = estimate_cost(
        "gpt-4o-mini", 1_000_000, 0, cached_prompt_tokens=1_000_000,
    )
    assert full == Decimal("0.150000")
    assert cached == Decimal("0.075000")  # half, at the cached rate
    assert cached < full


def test_cached_tokens_cannot_exceed_prompt_tokens():
    """A provider over-reporting cache hits must not produce a negative charge."""
    cost, _ = estimate_cost("gpt-4o-mini", 100, 0, cached_prompt_tokens=10_000)
    assert cost is not None and cost >= 0


def test_reasoning_tokens_are_charged_at_the_output_rate():
    """Thinking tokens are billed but excluded from the visible completion."""
    without, _ = estimate_cost("gemini-1.5-flash", 0, 1_000_000)
    with_thinking, _ = estimate_cost(
        "gemini-1.5-flash", 0, 1_000_000, reasoning_tokens=1_000_000,
    )
    assert with_thinking == without * 2


def test_web_search_calls_add_tool_cost(monkeypatch):
    monkeypatch.setattr(settings, "AI_WEB_SEARCH_CALL_USD", 0.01)
    cost, _ = estimate_cost("gpt-4o-mini", 0, 0, web_search_calls=3)
    assert cost == Decimal("0.030000")


# --------------------------------------------------------------------------
# Usage payload parsing
# --------------------------------------------------------------------------

def test_gemini_thinking_tokens_are_captured_separately():
    """candidatesTokenCount excludes thoughtsTokenCount, which is still billed."""
    counts = normalize_gemini_usage({
        "promptTokenCount": 1200,
        "candidatesTokenCount": 300,
        "thoughtsTokenCount": 900,
        "cachedContentTokenCount": 200,
    })
    assert counts == {
        "prompt_tokens": 1200, "completion_tokens": 300,
        "cached_prompt_tokens": 200, "reasoning_tokens": 900,
    }


def test_openai_responses_and_chat_usage_shapes_both_parse():
    """Chat Completions and the Responses API name these fields differently."""
    chat = normalize_openai_usage({"prompt_tokens": 10, "completion_tokens": 5})
    responses = normalize_openai_usage({"input_tokens": 10, "output_tokens": 5})
    assert chat["prompt_tokens"] == responses["prompt_tokens"] == 10
    assert chat["completion_tokens"] == responses["completion_tokens"] == 5


def test_usage_detail_subfields_parse():
    counts = normalize_openai_usage({
        "input_tokens": 100, "output_tokens": 50,
        "input_tokens_details": {"cached_tokens": 80},
        "output_tokens_details": {"reasoning_tokens": 40},
    })
    assert counts["cached_prompt_tokens"] == 80
    assert counts["reasoning_tokens"] == 40


def test_missing_or_garbage_usage_is_treated_as_zero():
    for value in (None, {}, "nonsense", 42, []):
        counts = normalize_openai_usage(value)
        assert counts["prompt_tokens"] == 0
        assert counts["completion_tokens"] == 0


def test_response_elapsed_never_raises():
    """Timing capture must not be able to fail a provider call."""
    class NoElapsed:
        pass

    class BadElapsed:
        elapsed = "not a timedelta"

    assert response_elapsed_ms(NoElapsed()) is None
    assert response_elapsed_ms(BadElapsed()) is None
    assert response_elapsed_ms(None) is None


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------

def _make_product(db: Session, name: str = "Test Serum") -> CanonicalProduct:
    brand = Brand(id=uuid.uuid4(), name=f"Brand {uuid.uuid4().hex[:6]}",
                  normalized_name=uuid.uuid4().hex)
    db.add(brand)
    db.flush()
    product = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, product_name=name,
        normalized_name=name.lower().replace(" ", ""), review_status="imported",
    )
    db.add(product)
    db.flush()
    return product


def test_record_ai_usage_persists_tokens_and_cost(db: Session):
    product = _make_product(db)
    event = record_ai_usage(
        db, operation="enrichment", provider="OpenAI", model="gpt-4o-mini",
        prompt_tokens=1000, completion_tokens=500,
        canonical_product_id=product.id,
    )
    assert event is not None
    stored = db.query(AiUsageEvent).filter(AiUsageEvent.id == event.id).first()
    assert stored.total_tokens == 1500
    assert stored.pricing_source == "reference"
    assert float(stored.estimated_cost_usd) == pytest.approx(0.00045, rel=1e-6)


def test_total_tokens_includes_reasoning_tokens(db: Session):
    event = record_ai_usage(
        db, operation="enrichment", provider="Google Gemini",
        model="gemini-1.5-flash", prompt_tokens=100, completion_tokens=50,
        reasoning_tokens=400,
    )
    assert event.total_tokens == 550


def test_failed_calls_are_still_recorded(db: Session):
    """A failed provider call is billed for what it consumed."""
    product = _make_product(db)
    record_ai_usage(
        db, operation="enrichment", provider="OpenAI", model="gpt-4o-mini",
        prompt_tokens=800, completion_tokens=0, status="failed",
        canonical_product_id=product.id, detail={"error": "schema validation"},
    )
    usage = usage_for_product(db, product.id)
    assert usage["totals"]["call_count"] == 1
    assert usage["totals"]["prompt_tokens"] == 800


def test_unknown_operation_is_coerced_to_other(db: Session):
    """The operation CHECK constraint must never be violated by a caller typo."""
    event = record_ai_usage(
        db, operation="not_a_real_operation", provider="OpenAI",
        model="gpt-4o-mini", prompt_tokens=10, completion_tokens=10,
    )
    assert event is not None and event.operation == "other"


def test_unpriced_model_records_tokens_with_no_cost(db: Session):
    product = _make_product(db)
    record_ai_usage(
        db, operation="web_research", provider="OpenAI",
        model="brand-new-unpriced-model", prompt_tokens=5000,
        completion_tokens=100, canonical_product_id=product.id,
    )
    usage = usage_for_product(db, product.id)
    assert usage["totals"]["total_tokens"] == 5100
    assert usage["totals"]["has_unpriced_calls"] is True
    assert usage["totals"]["estimated_cost_usd"] == 0.0


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def test_product_usage_aggregates_every_operation(db: Session):
    """Enrichment, research, review and assistant spend all roll up together."""
    product = _make_product(db)
    for operation, prompt, completion in (
        ("enrichment", 1000, 500),
        ("web_research", 20000, 2000),
        ("review_summary", 3000, 400),
    ):
        record_ai_usage(
            db, operation=operation, provider="OpenAI", model="gpt-4o-mini",
            prompt_tokens=prompt, completion_tokens=completion,
            canonical_product_id=product.id,
        )

    usage = usage_for_product(db, product.id)
    assert usage["totals"]["call_count"] == 3
    assert usage["totals"]["prompt_tokens"] == 24000
    assert usage["totals"]["completion_tokens"] == 2900
    assert usage["totals"]["total_tokens"] == 26900
    assert usage["totals"]["estimated_cost_usd"] > 0

    operations = {row["operation"] for row in usage["by_operation"]}
    assert operations == {"enrichment", "web_research", "review_summary"}
    # Ordered most expensive first, so the biggest cost driver reads first.
    assert usage["by_operation"][0]["operation"] == "web_research"
    assert len(usage["recent_events"]) == 3


def test_product_usage_is_scoped_to_that_product(db: Session):
    first, second = _make_product(db, "First"), _make_product(db, "Second")
    record_ai_usage(db, operation="enrichment", provider="OpenAI",
                    model="gpt-4o-mini", prompt_tokens=1000,
                    completion_tokens=0, canonical_product_id=first.id)
    record_ai_usage(db, operation="enrichment", provider="OpenAI",
                    model="gpt-4o-mini", prompt_tokens=7777,
                    completion_tokens=0, canonical_product_id=second.id)
    assert usage_for_product(db, first.id)["totals"]["prompt_tokens"] == 1000
    assert usage_for_product(db, second.id)["totals"]["prompt_tokens"] == 7777


def test_empty_product_usage_is_zero_not_an_error(db: Session):
    product = _make_product(db)
    usage = usage_for_product(db, product.id)
    assert usage["totals"]["total_tokens"] == 0
    assert usage["totals"]["call_count"] == 0
    assert usage["recent_events"] == []


def test_import_job_usage_reports_cost_per_product(db: Session):
    job = ImportJob(id=uuid.uuid4(), filename="f.csv", file_hash="h",
                    status="processing", column_mapping={})
    db.add(job)
    db.flush()
    for _ in range(2):
        product = _make_product(db)
        record_ai_usage(
            db, operation="enrichment", provider="OpenAI", model="gpt-4o-mini",
            prompt_tokens=1_000_000, completion_tokens=0,
            canonical_product_id=product.id, import_job_id=job.id,
        )
    usage = usage_for_import_job(db, job.id)
    assert usage["products_touched"] == 2
    assert usage["totals"]["estimated_cost_usd"] == pytest.approx(0.30, rel=1e-6)
    assert usage["cost_per_product_usd"] == pytest.approx(0.15, rel=1e-6)


def test_research_jobs_usage_aggregates_a_bulk_selection(db: Session):
    """Bulk Improve creates one research job per product; the total is the sum."""
    from app.models import CrawlJob

    job_ids = []
    for index in range(3):
        crawl = CrawlJob(
            id=uuid.uuid4(), domain=f"example{index}.com",
            starting_urls=["https://example.com"], crawl_mode="single_url",
            status="completed", configuration={},
        )
        db.add(crawl)
        db.flush()
        job_ids.append(crawl.id)
        product = _make_product(db)
        record_ai_usage(
            db, operation="web_research", provider="OpenAI", model="gpt-4o-mini",
            prompt_tokens=10_000, completion_tokens=1_000,
            canonical_product_id=product.id, research_job_id=crawl.id,
            web_search_calls=1,
        )

    usage = usage_for_research_jobs(db, job_ids)
    assert usage["job_count"] == 3
    assert usage["products_touched"] == 3
    assert usage["totals"]["total_tokens"] == 33_000
    assert usage["totals"]["web_search_calls"] == 3
    assert usage["cost_per_product_usd"] > 0

    # A subset must report only its own share.
    assert usage_for_research_jobs(db, job_ids[:1])["totals"]["total_tokens"] == 11_000


def test_research_jobs_usage_with_no_jobs_is_empty(db: Session):
    usage = usage_for_research_jobs(db, [])
    assert usage["totals"]["total_tokens"] == 0
    assert usage["totals"]["call_count"] == 0


def test_usage_summary_window_filters_by_date(db: Session):
    product = _make_product(db)
    old = AiUsageEvent(
        id=uuid.uuid4(), operation="enrichment", provider="OpenAI",
        model="gpt-4o-mini", status="success", canonical_product_id=product.id,
        prompt_tokens=999, completion_tokens=0, total_tokens=999,
        estimated_cost_usd=Decimal("0.001"), pricing_source="reference",
        created_at=datetime.now(timezone.utc) - timedelta(days=40),
    )
    db.add(old)
    record_ai_usage(db, operation="enrichment", provider="OpenAI",
                    model="gpt-4o-mini", prompt_tokens=100, completion_tokens=0,
                    canonical_product_id=product.id)

    everything = usage_summary(db)
    recent = usage_summary(db, since=datetime.now(timezone.utc) - timedelta(days=7))
    assert everything["totals"]["total_tokens"] == 1099
    assert recent["totals"]["total_tokens"] == 100
    assert any(row["model"] == "gpt-4o-mini" for row in everything["by_model"])


# --------------------------------------------------------------------------
# Budget enforcement
# --------------------------------------------------------------------------

def test_budget_not_exceeded_below_the_limit(db: Session, monkeypatch):
    monkeypatch.setattr(settings, "MAX_JOB_COST_LIMIT", 10.0)
    job = ImportJob(id=uuid.uuid4(), filename="f.csv", file_hash="h",
                    status="processing", column_mapping={})
    db.add(job)
    db.flush()
    record_ai_usage(db, operation="enrichment", provider="OpenAI",
                    model="gpt-4o-mini", prompt_tokens=1000,
                    completion_tokens=0, import_job_id=job.id)
    exceeded, spent, limit = job_budget_exceeded(db, import_job_id=job.id)
    assert exceeded is False
    assert limit == 10.0
    assert spent > 0


def test_budget_exceeded_at_the_limit(db: Session, monkeypatch):
    monkeypatch.setattr(settings, "MAX_JOB_COST_LIMIT", 0.10)
    job = ImportJob(id=uuid.uuid4(), filename="f.csv", file_hash="h",
                    status="processing", column_mapping={})
    db.add(job)
    db.flush()
    # 1M prompt tokens at $0.15/M exceeds a $0.10 cap.
    record_ai_usage(db, operation="enrichment", provider="OpenAI",
                    model="gpt-4o-mini", prompt_tokens=1_000_000,
                    completion_tokens=0, import_job_id=job.id)
    exceeded, spent, limit = job_budget_exceeded(db, import_job_id=job.id)
    assert exceeded is True
    assert spent == pytest.approx(0.15, rel=1e-6)


def test_zero_limit_disables_the_cap(db: Session, monkeypatch):
    """A 0 limit must mean 'no cap', not 'everything is over budget'."""
    monkeypatch.setattr(settings, "MAX_JOB_COST_LIMIT", 0.0)
    job = ImportJob(id=uuid.uuid4(), filename="f.csv", file_hash="h",
                    status="processing", column_mapping={})
    db.add(job)
    db.flush()
    record_ai_usage(db, operation="enrichment", provider="OpenAI",
                    model="gpt-4o-mini", prompt_tokens=50_000_000,
                    completion_tokens=0, import_job_id=job.id)
    exceeded, _, limit = job_budget_exceeded(db, import_job_id=job.id)
    assert exceeded is False
    assert limit is None


def test_budget_is_scoped_per_job(db: Session, monkeypatch):
    """One expensive job must not halt an unrelated one."""
    monkeypatch.setattr(settings, "MAX_JOB_COST_LIMIT", 0.10)
    spender = ImportJob(id=uuid.uuid4(), filename="a.csv", file_hash="a",
                        status="processing", column_mapping={})
    quiet = ImportJob(id=uuid.uuid4(), filename="b.csv", file_hash="b",
                      status="processing", column_mapping={})
    db.add_all([spender, quiet])
    db.flush()
    record_ai_usage(db, operation="enrichment", provider="OpenAI",
                    model="gpt-4o-mini", prompt_tokens=1_000_000,
                    completion_tokens=0, import_job_id=spender.id)
    assert job_budget_exceeded(db, import_job_id=spender.id)[0] is True
    assert job_budget_exceeded(db, import_job_id=quiet.id)[0] is False


# --------------------------------------------------------------------------
# Backfill from historical enrichment runs
# --------------------------------------------------------------------------

def _enrichment_run(db: Session, product, prompt, completion, cost) -> EnrichmentRun:
    run = EnrichmentRun(
        id=uuid.uuid4(), canonical_product_id=product.id, provider="OpenAI",
        model="gpt-4o-mini", model_version="1.0", prompt_version="4.0",
        schema_version="3.0", status="success", prompt_tokens=prompt,
        completion_tokens=completion, estimated_cost=cost, attempt_number=1,
    )
    db.add(run)
    db.flush()
    return run


def test_backfill_imports_historical_runs(db: Session):
    product = _make_product(db)
    _enrichment_run(db, product, 1000, 500, Decimal("0.00045"))
    _enrichment_run(db, product, 2000, 300, Decimal("0.00048"))
    db.commit()

    imported = backfill_usage_from_enrichment_runs(db)
    assert imported == 2
    usage = usage_for_product(db, product.id)
    assert usage["totals"]["total_tokens"] == 3800
    assert usage["totals"]["estimated_cost_usd"] == pytest.approx(0.00093, rel=1e-6)


def test_backfill_is_idempotent(db: Session):
    """Running it twice must not double-count historical spend."""
    product = _make_product(db)
    _enrichment_run(db, product, 1000, 500, Decimal("0.00045"))
    db.commit()

    assert backfill_usage_from_enrichment_runs(db) == 1
    assert backfill_usage_from_enrichment_runs(db) == 0
    assert usage_for_product(db, product.id)["totals"]["call_count"] == 1


def test_backfill_skips_runs_with_no_usage_data(db: Session):
    """A deterministic-fallback run with no tokens and no cost is not spend."""
    product = _make_product(db)
    run = EnrichmentRun(
        id=uuid.uuid4(), canonical_product_id=product.id,
        provider="Deterministic Fallback", model="None", model_version="None",
        prompt_version="4.0", schema_version="3.0", status="success",
        prompt_tokens=None, completion_tokens=None, estimated_cost=None,
        attempt_number=1,
    )
    db.add(run)
    db.commit()
    assert backfill_usage_from_enrichment_runs(db) == 0


def test_calibration_prices_an_unknown_model_from_history(db: Session):
    """A model with no configured rate is estimated from its own past runs."""
    product = _make_product(db)
    run = EnrichmentRun(
        id=uuid.uuid4(), canonical_product_id=product.id, provider="OpenAI",
        model="custom-finetune-v1", model_version="1.0", prompt_version="4.0",
        schema_version="3.0", status="success", prompt_tokens=500_000,
        completion_tokens=500_000, estimated_cost=Decimal("1.0"),
        attempt_number=1,
    )
    db.add(run)
    db.commit()

    ai_pricing.reset_pricing_caches()
    # 1M tokens cost $1.00 historically, so ~$1.00 per 1M blended.
    cost, source = estimate_cost("custom-finetune-v1", 500_000, 500_000, db=db)
    assert source == "calibrated"
    assert float(cost) == pytest.approx(1.0, rel=1e-3)


# --------------------------------------------------------------------------
# API surface
# --------------------------------------------------------------------------

def _auth(client, email="admin@test.com"):
    response = client.post(
        "/api/auth/token",
        data={"username": email, "password": "securepassword123"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_usage_summary_endpoint(client, db: Session):
    product = _make_product(db)
    record_ai_usage(db, operation="enrichment", provider="OpenAI",
                    model="gpt-4o-mini", prompt_tokens=1000,
                    completion_tokens=200, canonical_product_id=product.id)
    response = client.get("/api/ai-usage/summary", headers=_auth(client))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["totals"]["total_tokens"] == 1200
    # The response must say these are estimates, not invoice figures.
    assert "estimate" in body["estimate_note"].lower()
    assert body["pricing"]


def test_product_usage_endpoint(client, db: Session):
    product = _make_product(db)
    record_ai_usage(db, operation="web_research", provider="OpenAI",
                    model="gpt-4o-mini", prompt_tokens=5000,
                    completion_tokens=500, canonical_product_id=product.id)
    response = client.get(
        f"/api/ai-usage/products/{product.id}", headers=_auth(client),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["totals"]["total_tokens"] == 5500
    assert body["recent_events"][0]["operation"] == "web_research"


def test_product_usage_endpoint_404s_for_unknown_product(client):
    response = client.get(
        f"/api/ai-usage/products/{uuid.uuid4()}", headers=_auth(client),
    )
    assert response.status_code == 404


def test_usage_endpoints_require_authentication(client, db: Session):
    product = _make_product(db)
    assert client.get("/api/ai-usage/summary").status_code == 401
    assert client.get(f"/api/ai-usage/products/{product.id}").status_code == 401
    assert client.get("/api/ai-usage/pricing").status_code == 401


def test_backfill_endpoint_requires_editor_or_admin(client, db: Session):
    """A viewer must not be able to trigger a write operation."""
    viewer = _auth(client, "viewer@test.com")
    assert client.post("/api/ai-usage/backfill", headers=viewer).status_code == 403
    admin = _auth(client, "admin@test.com")
    assert client.post("/api/ai-usage/backfill", headers=admin).status_code == 200


def test_pricing_endpoint_discloses_provenance(client):
    response = client.get("/api/ai-usage/pricing", headers=_auth(client))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["override_env_var"] == "AI_MODEL_PRICING_JSON"
    for entry in body["models"]:
        assert entry["pricing_source"] in {
            "override", "reference", "calibrated", "unknown",
        }
        assert entry["pricing_source_label"]
        assert entry["is_estimate"] is True


def test_research_jobs_aggregate_endpoint_validates_input(client):
    headers = _auth(client)
    assert client.post(
        "/api/ai-usage/research-jobs/aggregate", json={"research_job_ids": []},
        headers=headers,
    ).status_code == 422
    assert client.post(
        "/api/ai-usage/research-jobs/aggregate",
        json={"research_job_ids": ["not-a-uuid"]}, headers=headers,
    ).status_code == 422
    assert client.post(
        "/api/ai-usage/research-jobs/aggregate",
        json={"research_job_ids": [str(uuid.uuid4()) for _ in range(101)]},
        headers=headers,
    ).status_code == 422
