from app.services.category_completeness import build_gap_plan, category_module, evaluate_completeness, quality_gate
from app.services.product_understanding import (
    enforce_evidence_scope, infer_module, is_placeholder, resolve_product_understanding, semantic_issues,
    understanding_contract_changed,
)
from app.services.product_pdf import build_product_pdf
from pypdf import PdfReader
from io import BytesIO
import uuid
from app.models import Brand, CanonicalProduct, FieldValue


def _exact_armani():
    return {
        "match_level": "exact_product",
        "exact_matches": [{
            "brand": "Armani Beauty", "product_name": "Lip Maestro",
            "variant_name": "405 Sultan", "gtin": "3605522075283",
            "category": "Makeup", "subcategory": "Lips", "product_type": "Liquid Lipstick",
            "fields": {}, "conflicts": [],
        }],
        "family_matches": [], "comparables": [],
    }


def test_placeholders_are_not_product_facts():
    assert all(is_placeholder(value) for value in ("STD", "C", "BOTH", "N/A", "Unknown"))
    assert not is_placeholder("Liquid Lipstick")


def test_unknown_never_defaults_to_skincare():
    snapshot = {"category": "STD", "product_type": "C", "product_name": "Opaque item"}
    assert category_module(snapshot) == "unknown"
    result = evaluate_completeness(snapshot)
    assert result["category_module"] == "unknown"
    assert result["overall_completeness"] <= 55
    assert build_gap_plan(snapshot)["identity_resolution_required"] is True
    plan = build_gap_plan(snapshot)
    assert plan["phase"] == "identity_resolution"
    assert all(item["objective_type"] == "identity" for item in plan["research_objectives"])
    assert plan["blocked_objectives"]


def test_partial_identity_cannot_score_as_complete():
    snapshot = {
        "brand": "Maybe", "product_name": "Generic Product", "category": "Makeup",
        "product_type": "Lipstick", "description": "Complete prose", "image_url": "https://x/y.jpg",
        "target_audience": ["One", "Two", "Three"], "benefits": ["Benefit"],
        "directions": "Apply", "product_understanding": {"identity_status": "partial", "category_module": "makeup"},
    }
    assert evaluate_completeness(snapshot)["overall_completeness"] <= 75


def test_multilingual_taxonomy_signals():
    assert infer_module("Maquillage", "Lèvres") == "makeup"
    assert infer_module("Hautpflege", "Gesicht Serum") == "skincare"
    assert infer_module("Parfum", "Eau de toilette") == "fragrance"
    assert infer_module("Haar", "Shampoo") == "haircare"


def test_common_body_and_bath_products_receive_safe_beauty_module():
    assert infer_module("Body Care", "Body Milk") == "skincare"
    assert infer_module("Bath & Shower", "Shower Gel") == "skincare"
    assert infer_module("Hand Care", "Hydrating Hand Cream") == "skincare"


def test_exact_beauty_accessory_keeps_identity_and_resolves_accessory_taxonomy(monkeypatch):
    monkeypatch.setattr("app.services.product_understanding.retrieve_corpus_evidence", lambda *a, **k: {
        "match_level": "exact_product", "exact_matches": [{
            "brand": "Shiseido", "product_name": "Eyelash Curler Pad",
            "gtin": "729238500976", "fields": {}, "conflicts": [],
        }], "family_matches": [], "comparables": [],
    })
    result = resolve_product_understanding(None, raw_data={
        "Brand": "Shiseido", "Product name": "Eyelash Curler Pad", "EAN": "729238500976",
    })
    assert result["identity_status"] == "resolved"
    assert result["taxonomy_status"] == "resolved"
    assert result["category_module"] == "beauty_accessory"
    assert result["taxonomy"]["category"]["value"] == "Beauty Tools & Accessories"
    assert result["taxonomy"]["subcategory"]["value"] == "Eye Tools & Accessories"
    assert result["taxonomy"]["product_type"]["value"] == "Eyelash Curler Refill Pad"
    assert result["taxonomy"]["application_area"]["value"] == "Eyes"

    quality = evaluate_completeness({
        "brand": "Shiseido", "product_name": "Eyelash Curler Pad", "gtin": "729238500976",
        "category": "Beauty Tools & Accessories", "subcategory": "Eye Tools & Accessories",
        "product_type": "Eyelash Curler Refill Pad", "product_understanding": result,
    })
    assert quality["category_module"] == "beauty_accessory"
    assert quality["taxonomy_status"] == "resolved"
    assert "inci" not in quality["field_states"]
    assert "shade_colour" not in quality["field_states"]


def test_missing_only_cannot_leave_stale_product_understanding_contract():
    stale = {
        "contract_version": "1.0", "identity_status": "resolved",
        "category_module": "unknown", "taxonomy_status": None,
    }
    resolved = {
        "contract_version": "1.1", "identity_status": "resolved",
        "category_module": "beauty_accessory", "taxonomy_status": "resolved",
    }
    assert understanding_contract_changed(stale, resolved) is True
    assert understanding_contract_changed(resolved, resolved) is False


def test_enterprise_row_resolves_brand_family_variant_and_makeup(monkeypatch):
    monkeypatch.setattr("app.services.product_understanding.retrieve_corpus_evidence", lambda *a, **k: _exact_armani())
    raw = {
        "Supplier": "L'OREAL GMBH (DÜFTE & CO.)", "Brand": "LIP MAESTRO",
        "Article description": "ARM LIP 405 SULTAN MAESTRO", "BGB Subgroup": "MAKEUP",
        "BGB Typegroup": "LIPPEN", "SKU type": "STD", "EAN": "3605522075283",
        "Base Product Code": "143ARMSO19",
    }
    mapping = {}
    result = resolve_product_understanding(None, raw_data=raw, mapping=mapping)
    assert result["identity_status"] == "resolved"
    assert result["identity"]["consumer_brand"]["value"] == "Armani Beauty"
    assert result["identity"]["product_family"]["value"] == "Lip Maestro"
    assert result["identity"]["variant"]["value"] == "405 Sultan"
    assert result["taxonomy"]["category"]["value"] == "Makeup"
    assert result["taxonomy"]["subcategory"]["value"] == "Lips"
    assert result["taxonomy"]["product_type"]["value"] == "Liquid Lipstick"
    assert result["category_module"] == "makeup"
    assert result["source_interpretation"]["supplier"] == "L'OREAL GMBH (DÜFTE & CO.)"
    assert result["source_interpretation"]["source_subcategory"] == "LIPPEN"


def test_comparable_does_not_become_exact_fact(monkeypatch):
    monkeypatch.setattr("app.services.product_understanding.retrieve_corpus_evidence", lambda *a, **k: {
        "match_level": "comparable", "exact_matches": [], "family_matches": [],
        "comparables": [{"brand": "Other", "product_name": "Other lipstick", "product_type": "Lipstick"}],
    })
    result = resolve_product_understanding(None, raw_data={"name": "Mystery", "brand": "Supplier GmbH"})
    assert result["identity"]["product_family"]["value"] == "Mystery"
    assert result["identity"]["consumer_brand"]["value"] is None
    assert result["source_interpretation"]["supplier"] == "Supplier GmbH"
    assert result["match_type"] == "comparable"
    assert result["category_module"] == "unknown"


def test_strict_claim_gate_and_module_contradiction():
    payload = {"claims": [{"name": "Vegan", "value": "Yes", "status": "inferred", "evidence": []}]}
    cleaned, rejected = quality_gate(payload, "makeup")
    assert cleaned["claims"][0]["value"] == "Unknown"
    assert "unsupported_claim:vegan" in rejected
    understanding = {"category_module": "makeup", "identity_status": "resolved", "identity": {}, "taxonomy": {}}
    issues = semantic_issues(understanding, {"skincare": {"skin_types": ["Dry"]}})
    assert any(item["type"] == "category_module_contradiction" for item in issues)


def test_armani_pigment_language_is_not_skincare_pigmentation():
    payload = {"targeted_concerns": {"values": ["Pigmentation"]}, "claims": []}
    cleaned, rejected = quality_gate(payload, "makeup", "high pigment colour payoff lipstick")
    assert cleaned["targeted_concerns"]["values"] == ["Colour payoff"]
    assert any("makeup_concern_collision" in item for item in rejected)


def test_evidence_scope_blocks_comparable_exact_facts():
    understanding = {"match_type": "comparable", "identity": {}, "taxonomy": {}}
    payload = {
        "ingredients_intelligence": [{"ingredient_name": "Invented"}],
        "makeup": {"shade_colour": {"value": "405 Sultan", "evidence": [{"match_type": "comparable"}]}},
        "claims": [{"name": "Clinically Tested", "value": "Yes", "status": "inferred", "evidence": []}],
        "warnings_considerations": [{"type": "regulatory", "observation": "Approved", "evidence": []}],
    }
    cleaned, rejected = enforce_evidence_scope(payload, understanding)
    assert cleaned["ingredients_intelligence"] == []
    assert cleaned["makeup"]["shade_colour"] is None
    assert cleaned["claims"][0]["value"] == "Unknown"
    assert cleaned["warnings_considerations"] == []
    assert len(rejected) == 4


def test_adversarial_categories_do_not_cross_contaminate(monkeypatch):
    monkeypatch.setattr("app.services.product_understanding.retrieve_corpus_evidence", lambda *a, **k: {
        "match_level": "unmatched", "exact_matches": [], "family_matches": [], "comparables": [],
    })
    cases = [
        ({"Article description": "Fresh Eau de Parfum 50 ml", "BGB Subgroup": "PARFUM"}, "fragrance"),
        ({"Article description": "Repair Shampoo", "BGB Subgroup": "HAARE"}, "haircare"),
        ({"Article description": "Barrier Face Serum", "BGB Subgroup": "HAUTPFLEGE"}, "skincare"),
        ({"Article description": "Velvet Foundation", "BGB Subgroup": "MAKEUP"}, "makeup"),
    ]
    for raw, expected in cases:
        result = resolve_product_understanding(None, raw_data=raw)
        assert result["category_module"] == expected
        issues = semantic_issues(result, {expected: {}})
        assert not any(item["type"] == "category_module_contradiction" for item in issues)


def test_armani_contract_drives_makeup_pdf_not_skincare(monkeypatch):
    monkeypatch.setattr("app.services.product_understanding.retrieve_corpus_evidence", lambda *a, **k: _exact_armani())
    result = resolve_product_understanding(None, raw_data={
        "EAN": "3605522075283", "Article description": "ARM LIP 405 SULTAN MAESTRO",
        "Brand": "LIP MAESTRO", "Supplier": "L'OREAL GMBH (DÜFTE & CO.)",
        "BGB Subgroup": "MAKEUP", "BGB Typegroup": "LIPPEN", "SKU type": "STD",
    })
    pdf = build_product_pdf({
        "product_name": "Lip Maestro", "brand_name": "Armani Beauty", "product_category": "Makeup",
        "gtin": "3605522075283", "variants": [{"variant_name": "405 Sultan", "gtin": "3605522075283"}],
        "formulations": [], "market_observations": [], "field_values": [
            {"field_name": "product_understanding", "value": result, "is_current": True},
            {"field_name": "product_type", "value": "Liquid Lipstick", "is_current": True},
            {"field_name": "makeup", "value": {"shade_colour": "405 Sultan", "finish": "Velvet"}, "is_current": True},
        ],
    })
    text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages).upper()
    assert "MAKEUP PROFILE" in text
    assert "SKINCARE PROFILE" not in text
    assert "STD" not in text


def test_human_category_override_survives_exact_conflicting_evidence(db, monkeypatch):
    brand = Brand(id=uuid.uuid4(), name="Human Brand", normalized_name=uuid.uuid4().hex)
    product = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Human Product", normalized_name=uuid.uuid4().hex)
    override = FieldValue(
        id=uuid.uuid4(), canonical_product_id=product.id, field_name="category", value="Makeup",
        source_type="human_edit", source_reference="user:test", confidence_score=1,
        review_status="confirmed", is_current=True,
    )
    db.add_all([brand, product, override]); db.flush()
    monkeypatch.setattr("app.services.product_understanding.retrieve_corpus_evidence", lambda *a, **k: {
        "match_level": "exact_product", "exact_matches": [{
            "brand": "Human Brand", "product_name": "Human Product", "category": "Skincare",
            "subcategory": "Serum", "product_type": "Face Serum", "fields": {}, "conflicts": [],
        }], "family_matches": [], "comparables": [],
    })
    result = resolve_product_understanding(db, raw_data={"ean": "1234567890123"}, product=product)
    assert result["taxonomy"]["category"]["value"] == "Makeup"
    assert result["category_module"] == "makeup"
    assert any(item.get("resolution") == "human_value_preserved" for item in result["conflicts"])
