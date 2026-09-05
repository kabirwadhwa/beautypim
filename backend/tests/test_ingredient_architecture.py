import uuid
import io
from unittest.mock import patch

from pypdf import PdfReader

from app.auth import create_access_token
from app.models import (
    Brand, CanonicalProduct, FieldValue, Formulation, FormulationIngredient,
    ImportJob, IngredientDefinition, ProductVariant, SourceListing,
)
from app.routes.products import get_product_detail
from app.services.formulation_resolution import (
    apply_key_ingredient_highlights, formulation_ingredient_rows,
    promote_formulation, resolve_ingredient_definition, resolve_selected_formulation,
    synchronize_current_source_formulation,
)
from app.services.ingredient_backfill import repair_legacy_ingredient_state
from app.services.business_export import build_business_row
from app.services.product_pdf import build_product_pdf
from app.services.product_improvement import product_improvement_summary


def _product(db):
    brand = Brand(id=uuid.uuid4(), name="Ingredient Brand", normalized_name=uuid.uuid4().hex)
    product = CanonicalProduct(id=uuid.uuid4(), brand=brand, product_name="Ingredient Cream",
                               normalized_name=uuid.uuid4().hex, review_status="imported")
    first = ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id, gtin="1111111111111")
    second = ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id, gtin="2222222222222")
    db.add_all([brand, product, first, second]); db.flush()
    return product, first, second


def test_trusted_alias_unicode_ambiguity_and_unknown_are_never_guessed(db):
    tocopherol = IngredientDefinition(
        id=uuid.uuid4(), name="TOCOPHEROL", normalized_name="tocopherol",
        common_name="Vitamin E", aliases=["α-Tocopherol"], source_name="Trusted glossary",
    )
    ambiguous_a = IngredientDefinition(id=uuid.uuid4(), name="FIRST", normalized_name="first", aliases=["Shared"])
    ambiguous_b = IngredientDefinition(id=uuid.uuid4(), name="SECOND", normalized_name="second", aliases=["Shared"])
    db.add_all([tocopherol, ambiguous_a, ambiguous_b]); db.flush()
    assert resolve_ingredient_definition(db, "tocopherol").definition.id == tocopherol.id
    assert resolve_ingredient_definition(db, " Vitamin   E ").method == "trusted_common_name"
    assert resolve_ingredient_definition(db, "α–Tocopherol").definition.id == tocopherol.id
    assert resolve_ingredient_definition(db, "Shared").status == "ambiguous"
    assert resolve_ingredient_definition(db, "Unknownol").status == "unresolved"


def test_selected_variant_is_deterministic_and_sibling_never_leaks(db):
    product, first, second = _product(db)
    one = promote_formulation(db, product=product, variant=first, raw_inci_text="Aqua, Firstol",
                              source_kind="verified_evidence", source_reference="verified:first").formulation
    two = promote_formulation(db, product=product, variant=second, raw_inci_text="Aqua, Secondol",
                              source_kind="verified_evidence", source_reference="verified:second").formulation
    assert resolve_selected_formulation(db, product.id, first.id).id == one.id
    assert resolve_selected_formulation(db, product.id, second.id).id == two.id
    first_detail = get_product_detail(product.id, db, None, first.id)
    assert [row.raw_inci_text for row in first_detail.formulations] == ["Aqua, Firstol"]
    assert [row.name for row in first_detail.ingredients] == ["Aqua", "Firstol"]
    assert not first_detail.key_ingredients

    exported = build_business_row(db, first_detail, include_inferred=True)
    assert exported["raw_inci"] == "Aqua, Firstol"
    assert "Secondol" not in str(exported)
    pdf_text = "\n".join(
        page.extract_text() or ""
        for page in PdfReader(io.BytesIO(build_product_pdf(first_detail))).pages
    )
    assert "Aqua, Firstol" in pdf_text
    assert "Secondol" not in pdf_text


def test_product_detail_completeness_uses_explicit_selected_variant(db):
    product, first, second = _product(db)
    first.variant_name, first.size, first.unit = "200 ml", "200", "ml"
    second.variant_name, second.size, second.unit = "50 ml", "50", "ml"
    promote_formulation(
        db, product=product, variant=first, raw_inci_text="Aqua, Firstol",
        source_kind="verified_evidence", source_reference="verified:first",
    )
    promote_formulation(
        db, product=product, variant=second, raw_inci_text="Aqua, Secondol, Thirdol",
        source_kind="verified_evidence", source_reference="verified:second",
    )

    first_detail = get_product_detail(product.id, db, None, first.id)
    second_detail = get_product_detail(product.id, db, None, second.id)

    assert first_detail.product_variant_id == first.id
    assert first_detail.completeness["identity"]["gtin"] == first.gtin
    assert first_detail.completeness["identity"]["variant"] == "200 ml"
    assert first_detail.completeness["identity"]["size"] == "200ml"
    assert first_detail.completeness["ingredient_completeness"]["total_ingredients"] == 2
    assert [row.raw_inci_text for row in first_detail.formulations] == ["Aqua, Firstol"]

    assert second_detail.product_variant_id == second.id
    assert second_detail.completeness["identity"]["gtin"] == second.gtin
    assert second_detail.completeness["identity"]["variant"] == "50 ml"
    assert second_detail.completeness["identity"]["size"] == "50ml"
    assert second_detail.completeness["ingredient_completeness"]["total_ingredients"] == 3
    assert [row.raw_inci_text for row in second_detail.formulations] == ["Aqua, Secondol, Thirdol"]
    assert first.gtin not in str(second_detail.completeness)
    assert "Firstol" not in str(second_detail.formulations)


def test_ambiguous_product_level_formulation_never_leaks_to_sibling(db):
    product, first, _ = _product(db)
    promote_formulation(
        db, product=product, variant=None, raw_inci_text="Aqua, Ambiguousol",
        source_kind="verified_evidence", source_reference="verified:product-level",
    )
    assert resolve_selected_formulation(db, product.id, first.id) is None
    detail = get_product_detail(product.id, db, None, first.id)
    assert detail.formulations == []
    assert detail.ingredients == []


def test_historical_plan_assigns_product_level_formulation_only_from_exact_source_gtin(db):
    product, first, second = _product(db)
    job = ImportJob(
        id=uuid.uuid4(), filename="ingredients.xlsx", file_hash=uuid.uuid4().hex,
        column_mapping={}, status="completed",
    )
    exact_listing = SourceListing(
        id=uuid.uuid4(), import_job_id=job.id, canonical_product_id=product.id,
        raw_data={"EAN code": f" {second.gtin}.0 "}, source_hash=uuid.uuid4().hex,
    )
    conflicting_listing = SourceListing(
        id=uuid.uuid4(), import_job_id=job.id, canonical_product_id=product.id,
        raw_data={"EAN": "9999999999999"}, source_hash=uuid.uuid4().hex,
    )
    exact = Formulation(
        id=uuid.uuid4(), canonical_product_id=product.id,
        source_listing_id=exact_listing.id, raw_inci_text="Aqua, Exactol",
        content_hash=uuid.uuid4().hex,
    )
    conflicting = Formulation(
        id=uuid.uuid4(), canonical_product_id=product.id,
        source_listing_id=conflicting_listing.id, raw_inci_text="Aqua, Unknownol",
        content_hash=uuid.uuid4().hex,
    )
    db.add_all([job, exact_listing, conflicting_listing, exact, conflicting]); db.flush()

    plan = repair_legacy_ingredient_state(db, dry_run=True)["reconciliation"]
    assignments = {
        row["formulation_id"]: row
        for row in plan["ambiguous_product_level_formulations"]["safe_assignments"]
    }
    manual = {
        row["formulation_id"]: row
        for row in plan["ambiguous_product_level_formulations"]["manual_review"]
    }
    assert assignments[str(exact.id)]["target_variant_id"] == str(second.id)
    assert "normalized exact GTIN" in assignments[str(exact.id)]["reason"]
    assert str(conflicting.id) in manual
    assert resolve_selected_formulation(db, product.id, first.id) is None


def test_explicit_exact_highlight_is_key_and_presence_is_not(db):
    product, first, _ = _product(db)
    formulation = promote_formulation(
        db, product=product, variant=first, raw_inci_text="Aqua, Glycerin",
        source_kind="customer_source", source_reference="customer_import:test",
    ).formulation
    rows = formulation_ingredient_rows(db, formulation)
    assert not any(row.is_key_ingredient for row in rows)
    assert apply_key_ingredient_highlights(
        db, formulation, ["Glycerin"], source_kind="source_data",
        evidence=[{"match_type": "exact_product", "source_reference": "feed:test",
                   "supporting_text": "Hero ingredient: Glycerin"}],
    ) == 1
    glycerin = formulation_ingredient_rows(db, formulation)[1]
    assert glycerin.is_key_ingredient and glycerin.evidence
    assert apply_key_ingredient_highlights(
        db, formulation, ["Aqua"], source_kind="ai_inference",
        evidence=[{"match_type": "exact_product"}],
    ) == 0


def test_new_exact_highlight_replaces_stale_lower_precedence_key(db):
    product, first, _ = _product(db)
    formulation = promote_formulation(
        db, product=product, variant=first, raw_inci_text="Aqua, Glycerin",
        source_kind="customer_source", source_reference="customer_import:test",
    ).formulation
    exact = [{"match_type": "exact_product", "supporting_text": "Highlighted ingredient"}]
    assert apply_key_ingredient_highlights(
        db, formulation, ["Aqua"], evidence=exact,
        source_kind="internal_corpus", replace_existing=True,
    ) == 1
    assert apply_key_ingredient_highlights(
        db, formulation, ["Glycerin"], evidence=exact,
        source_kind="source_data", replace_existing=True,
    ) == 1
    rows = formulation_ingredient_rows(db, formulation)
    assert rows[0].is_key_ingredient is False
    assert rows[1].is_key_ingredient is True


def test_human_formulation_wins_immediately_and_customer_cannot_replace(db):
    product, first, _ = _product(db)
    customer = FieldValue(id=uuid.uuid4(), canonical_product_id=product.id, field_name="ingredients",
                          value="Aqua, Customerol", source_type="source_data", review_status="confirmed",
                          is_current=True, evidence=[{"evidence_type": "explicit_customer_source"}])
    db.add(customer); db.flush()
    assert synchronize_current_source_formulation(db, product, first).status == "applied"
    customer.is_current = False
    human = FieldValue(id=uuid.uuid4(), canonical_product_id=product.id, field_name="ingredients",
                       value="Aqua, Humanol", source_type="human_edit", review_status="confirmed", is_current=True)
    db.add(human); db.flush()
    assert synchronize_current_source_formulation(db, product, first).status == "applied"
    assert resolve_selected_formulation(db, product.id, first.id).raw_inci_text == "Aqua, Humanol"
    assert promote_formulation(db, product=product, variant=first, raw_inci_text="Aqua, OlderCustomerol",
                               source_kind="customer_source", source_reference="customer_import:old").status == "rejected"


def test_human_ingredient_edit_endpoint_synchronizes_selected_variant_immediately(client, db):
    product, first, second = _product(db)
    promote_formulation(
        db, product=product, variant=first, raw_inci_text="Aqua, Customerol",
        source_kind="customer_source", source_reference="customer_import:test",
    )
    promote_formulation(
        db, product=product, variant=second, raw_inci_text="Aqua, Siblingol",
        source_kind="customer_source", source_reference="customer_import:sibling",
    )
    token = create_access_token(data={"sub": "admin@test.com"})
    response = client.put(
        f"/api/products/{product.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "field_name": "ingredients", "value": "Aqua, Humanol",
            "reason": "Correct exact package label", "product_variant_id": str(first.id),
        },
    )
    assert response.status_code == 200
    assert resolve_selected_formulation(db, product.id, first.id).raw_inci_text == "Aqua, Humanol"
    assert resolve_selected_formulation(db, product.id, second.id).raw_inci_text == "Aqua, Siblingol"


def test_completeness_dimensions_do_not_require_key_ingredients(db):
    product, first, _ = _product(db)
    definition = IngredientDefinition(id=uuid.uuid4(), name="GLYCERIN", normalized_name="glycerin",
                                      function="Humectant", source_name="Trusted glossary")
    db.add(definition); db.flush()
    promote_formulation(db, product=product, variant=first, raw_inci_text="Aqua, Glycerin",
                        source_kind="verified_evidence", source_reference="verified:test")
    summary = product_improvement_summary(db, product)
    ingredient = summary["ingredient_completeness"]
    assert ingredient["formulation_complete"] is True
    assert ingredient["total_ingredients"] == 2
    assert ingredient["resolved_ingredients"] == 1
    assert ingredient["identity_resolution_coverage"] == 50
    assert ingredient["ingredient_intelligence_coverage"] == 100
    assert ingredient["key_ingredient_evidence_status"] == "not_published_or_not_found"
    assert "key_ingredients" not in summary["missing_high_priority_fields"]
    assert "key_ingredients" not in [row["field"] for row in summary["research_objectives"]]


def test_legacy_ai_key_flags_are_quarantined_from_detail_and_completeness(db):
    product, first, _ = _product(db)
    formulation = promote_formulation(
        db, product=product, variant=first, raw_inci_text="Aqua, Glycerin",
        source_kind="customer_source", source_reference="customer_import:test",
    ).formulation
    glycerin = formulation_ingredient_rows(db, formulation)[1]
    glycerin.is_key_ingredient = True
    glycerin.key_ingredient_status = "source_supported"
    glycerin.evidence_source = "ai_inference"
    glycerin.evidence = []
    db.flush()

    detail = get_product_detail(product.id, db, None, first.id)
    assert detail.key_ingredients == []
    summary = product_improvement_summary(db, product)
    assert summary["ingredient_completeness"]["key_ingredient_count"] == 0
    assert summary["ingredient_completeness"]["key_ingredient_evidence_status"] == "not_published_or_not_found"


def test_exact_source_supported_key_highlight_remains_visible(db):
    product, first, _ = _product(db)
    formulation = promote_formulation(
        db, product=product, variant=first, raw_inci_text="Aqua, Glycerin",
        source_kind="customer_source", source_reference="customer_import:test",
    ).formulation
    assert apply_key_ingredient_highlights(
        db, formulation, ["Glycerin"], source_kind="customer_source",
        evidence=[{"match_type": "exact_gtin", "source_url": "https://example.com/product"}],
    ) == 1

    detail = get_product_detail(product.id, db, None, first.id)
    assert [row.name for row in detail.key_ingredients] == ["Glycerin"]
    summary = product_improvement_summary(db, product)
    assert summary["ingredient_completeness"]["key_ingredient_count"] == 1
    assert summary["ingredient_completeness"]["key_ingredient_evidence_status"] == "source_supported"


def test_backfill_dry_run_is_non_mutating_and_external_call_free(db):
    product, first, _ = _product(db)
    db.add(FieldValue(id=uuid.uuid4(), canonical_product_id=product.id, field_name="ingredients",
                      value="Aqua, Glycerin", source_type="human_edit", review_status="confirmed", is_current=True))
    db.flush()
    with patch("app.worker.run_ai_enrichment") as ai, \
         patch("app.routes.products._automatic_product_research") as research:
        first_run = repair_legacy_ingredient_state(db, dry_run=True)
        second_run = repair_legacy_ingredient_state(db, dry_run=True)
    assert first_run == second_run
    assert first_run["inventory"]["eligible"]["count"] == 1
    assert db.query(Formulation).count() == 0
    ai.assert_not_called(); research.assert_not_called()


def test_backfill_apply_is_idempotent_external_call_free_and_never_guesses_variant(db):
    product, _, _ = _product(db)
    db.add(FieldValue(
        id=uuid.uuid4(), canonical_product_id=product.id, field_name="ingredients",
        value="Aqua, Ambiguousol", source_type="human_edit",
        review_status="confirmed", is_current=True,
    ))
    single_brand = Brand(id=uuid.uuid4(), name="Single", normalized_name=uuid.uuid4().hex)
    single = CanonicalProduct(id=uuid.uuid4(), brand=single_brand, product_name="Single Cream",
                              normalized_name=uuid.uuid4().hex, review_status="imported")
    single_variant = ProductVariant(id=uuid.uuid4(), canonical_product_id=single.id, gtin="3333333333333")
    db.add_all([single_brand, single, single_variant]); db.flush()
    db.add(FieldValue(
        id=uuid.uuid4(), canonical_product_id=single.id, field_name="ingredients",
        value="Aqua, Safeol", source_type="human_edit", review_status="confirmed", is_current=True,
    )); db.flush()
    with patch("app.worker.run_ai_enrichment") as ai, \
         patch("app.routes.products._automatic_product_research") as research:
        first_run = repair_legacy_ingredient_state(db, dry_run=False)
        second_run = repair_legacy_ingredient_state(db, dry_run=False)
    assert str(product.id) in first_run["actions"]["ambiguous"]["ids"]
    assert resolve_selected_formulation(db, product.id, None) is None
    assert resolve_selected_formulation(db, single.id, single_variant.id).raw_inci_text == "Aqua, Safeol"
    assert first_run["actions"]["promoted"]["count"] == 1
    assert second_run["actions"]["promoted"]["count"] == 0
    assert second_run["actions"]["already_correct"]["count"] == 1
    ai.assert_not_called(); research.assert_not_called()


def test_historical_definition_reconciliation_is_deterministic_and_preserves_raw_order(db):
    product, first, _ = _product(db)
    trusted = IngredientDefinition(
        id=uuid.uuid4(), name="TOCOPHEROL", normalized_name="tocopherol",
        common_name="Vitamin E", function="Antioxidant",
        source_name="European Commission CosIng", source_record_id="COSING-1",
    )
    trusted_a = IngredientDefinition(
        id=uuid.uuid4(), name="ALPHA", normalized_name="alpha", aliases=["Shared exact alias"],
        source_name="Trusted glossary", source_record_id="TRUST-1",
    )
    trusted_b = IngredientDefinition(
        id=uuid.uuid4(), name="BETA", normalized_name="beta", aliases=["Shared exact alias"],
        source_name="Trusted glossary", source_record_id="TRUST-2",
    )
    suspected_merge = IngredientDefinition(
        id=uuid.uuid4(), name="Vitamin E", normalized_name="vitamin e",
        function="AI function", benefits="AI benefit",
    )
    suspected_unknown = IngredientDefinition(
        id=uuid.uuid4(), name="Mystery Complex", normalized_name="mystery complex",
        function="AI function", benefits="AI benefit",
    )
    suspected_ambiguous = IngredientDefinition(
        id=uuid.uuid4(), name="Shared exact alias", normalized_name="shared exact alias",
    )
    false_positive = IngredientDefinition(
        id=uuid.uuid4(), name="GLYCERIN", normalized_name="glycerin",
        function="Humectant", source_name="European Commission CosIng", source_record_id="COSING-2",
    )
    metadata_cleanup = IngredientDefinition(
        id=uuid.uuid4(), name="AQUA", normalized_name="aqua", function="AI-added function",
        benefits="AI-added benefit", possible_concerns="AI-added concern",
        source_name="European Commission CosIng", source_url="https://ec.europa.eu/growth/tools-databases/cosing/",
    )
    db.add_all([
        trusted, trusted_a, trusted_b, suspected_merge, suspected_unknown,
        suspected_ambiguous, false_positive, metadata_cleanup,
    ]); db.flush()
    formulation = promote_formulation(
        db, product=product, variant=first,
        raw_inci_text="Vitamin E, Mystery Complex, Shared exact alias, Glycerin, Aqua",
        source_kind="customer_source", source_reference="customer_import:test",
    ).formulation
    rows = formulation_ingredient_rows(db, formulation)
    for row, definition in zip(rows, [
        suspected_merge, suspected_unknown, suspected_ambiguous, false_positive, metadata_cleanup,
    ]):
        row.ingredient_definition_id = definition.id
        row.evidence_source = "ai_inference"
    db.flush()

    dry = repair_legacy_ingredient_state(db, dry_run=True)
    summary = dry["reconciliation"]["summary"]
    assert summary["SAFE_CANONICAL_MERGE"]["definitions"] == 1
    assert summary["NO_TRUSTED_MATCH"]["definitions"] == 1
    assert summary["AMBIGUOUS"]["definitions"] == 1
    assert summary["ALREADY_TRUSTED_FALSE_POSITIVE"]["definitions"] == 0
    assert summary["TRUSTED_IDENTITY_NEEDS_METADATA_CLEANUP"]["definitions"] == 1
    assert [row.ingredient_definition_id for row in rows] == [
        suspected_merge.id, suspected_unknown.id, suspected_ambiguous.id,
        false_positive.id, metadata_cleanup.id,
    ]

    applied = repair_legacy_ingredient_state(db, dry_run=False)
    db.refresh(formulation)
    refreshed = formulation_ingredient_rows(db, formulation)
    assert [row.raw_inci_name for row in refreshed] == [
        "Vitamin E", "Mystery Complex", "Shared exact alias", "Glycerin", "Aqua",
    ]
    assert [row.position for row in refreshed] == [1, 2, 3, 4, 5]
    assert refreshed[0].ingredient_definition_id == trusted.id
    assert refreshed[1].ingredient_definition_id is None
    assert refreshed[2].ingredient_definition_id is None
    assert refreshed[3].ingredient_definition_id == false_positive.id
    assert refreshed[4].ingredient_definition_id == metadata_cleanup.id
    db.refresh(metadata_cleanup)
    assert metadata_cleanup.function is None
    assert metadata_cleanup.benefits is None
    assert metadata_cleanup.possible_concerns is None
    assert applied["actions"]["references_repointed"]["count"] == 1
    assert applied["actions"]["references_unresolved"]["count"] == 2
    assert formulation.raw_inci_text == "Vitamin E, Mystery Complex, Shared exact alias, Glycerin, Aqua"


def test_historical_cleanup_quarantines_unsafe_keys_archives_legacy_intelligence_and_is_idempotent(db):
    product, first, _ = _product(db)
    formulation = promote_formulation(
        db, product=product, variant=first, raw_inci_text="Aqua, Glycerin",
        source_kind="customer_source", source_reference="customer_import:test",
    ).formulation
    aqua, glycerin = formulation_ingredient_rows(db, formulation)
    aqua.is_key_ingredient = True
    aqua.evidence_source = "ai_inference"
    aqua.evidence = []
    glycerin.is_key_ingredient = True
    glycerin.evidence_source = "customer_source"
    glycerin.evidence = [{"match_type": "exact_gtin", "source_reference": "feed:test"}]
    legacy = FieldValue(
        id=uuid.uuid4(), canonical_product_id=product.id,
        field_name="ingredients_intelligence", value={"key_ingredients": ["Aqua"]},
        source_type="ai_inference", review_status="confirmed", is_current=True,
    )
    db.add(legacy); db.flush()

    with patch("app.worker.run_ai_enrichment") as ai, \
         patch("app.routes.products._automatic_product_research") as research:
        dry = repair_legacy_ingredient_state(db, dry_run=True)
        assert aqua.is_key_ingredient is True and legacy.is_current is True
        first_run = repair_legacy_ingredient_state(db, dry_run=False)
        second_run = repair_legacy_ingredient_state(db, dry_run=False)
    db.refresh(aqua); db.refresh(glycerin); db.refresh(legacy)
    assert aqua.is_key_ingredient is False
    assert aqua.key_ingredient_status == "quarantined_legacy_unsupported"
    assert glycerin.is_key_ingredient is True
    assert legacy.is_current is False
    assert dry["reconciliation"]["key_ingredients"]["quarantined_count"] == 1
    assert dry["reconciliation"]["key_ingredients"]["retained_count"] == 1
    assert first_run["actions"]["legacy_fields_archived"]["count"] == 1
    assert second_run["actions"]["legacy_fields_archived"]["count"] == 0
    assert formulation.raw_inci_text == "Aqua, Glycerin"
    ai.assert_not_called(); research.assert_not_called()
