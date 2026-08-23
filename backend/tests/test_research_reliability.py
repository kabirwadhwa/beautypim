from app.services.research_reliability import (
    build_identity_query_plan, clean_enterprise_product_name,
    classify_research_error, evaluate_research_outcome, public_business_status,
)


def _snapshot(**overrides):
    value = {
        "completeness": 40, "identity_status": "complete", "identity_completeness": 100,
        "category_module": "fragrance", "fields": {},
        "missing_high_priority_fields": ["top_notes"], "image_present": False,
        "review_evidence_present": False, "formulation_count": 0,
    }
    value.update(overrides)
    return value


def test_enterprise_identity_plan_keeps_raw_and_uses_bounded_safe_queries():
    plan = build_identity_query_plan(
        brand="KHAMRAH", product_name="LAT KHAMRAH DUKH", gtin="6290362342373",
        product_format="Perfume", category="Duft",
        raw_data={"Article description": "LAT KHAMRAH DUKH", "Supplier": "Lattafa Perfumes"},
    )
    assert plan[0] == {"strategy": "exact_gtin", "query": "6290362342373"}
    assert any(item["strategy"] == "supplier_clean_name" and "KHAMRAH DUKH" in item["query"] for item in plan)
    assert plan[-1]["query"] == "LAT KHAMRAH DUKH"
    assert len(plan) <= 7


def test_ambiguous_prefixes_are_not_asserted_as_brands():
    assert clean_enterprise_product_name("M DG SHOWER GEL FLORAL", context="body bath") == "Shower Gel FLORAL"
    assert clean_enterprise_product_name("CL FDT. L1C DOUBL", context="makeup teint") == "Foundation"


def test_normal_completion_without_change_is_not_success():
    result = evaluate_research_outcome(_snapshot(), _snapshot(), result={"sources_ingested": 0}, errors=[])
    assert result["business_outcome"] == "no_material_improvement"
    assert public_business_status(result["business_outcome"]) == "NO_EVIDENCE_FOUND"


def test_blocked_page_does_not_downgrade_materially_improved_product():
    after = _snapshot(completeness=82, fields={"top_notes": ["Bergamot"]},
                      missing_high_priority_fields=[], image_present=True)
    result = evaluate_research_outcome(
        _snapshot(), after, result={"sources_ingested": 2}, errors=["ebay.example: HTTP 403"],
    )
    assert result["business_outcome"] == "improved"
    assert result["sources_blocked"] == 1


def test_unresolved_identity_is_never_reported_improved():
    after = _snapshot(completeness=60, identity_status="unresolved", category_module="unknown",
                      fields={"description": "Generic description"})
    result = evaluate_research_outcome(_snapshot(identity_status="unresolved"), after,
                                       result={"identity_unresolved": True}, errors=[])
    assert result["business_outcome"] == "needs_identity_resolution"


def test_failure_classification_distinguishes_provider_and_source_failures():
    assert classify_research_error("HTTP 429 tokens per min") == "rate_limited"
    assert classify_research_error("Retailer HTTP 403") == "source_blocked"
    assert classify_research_error("connection timed out") == "transient"
