from app.services.category_completeness import build_gap_plan, evaluate_completeness, quality_gate


def base_product(category="Skincare", product_type="Moisturiser"):
    return {
        "brand": "Test Brand", "product_name": "Test Product", "gtin": "1234567890123",
        "size": "50 ml", "category": category, "product_type": product_type,
        "description": "A specific product description.", "image_url": "https://example.com/product.jpg",
        "target_audience": {"value": ["Need profile", "Taste profile", "Lifestyle profile"]},
        "product_positioning": {"value": "Everyday barrier-supporting moisturiser"},
        "benefits": [{"statement": "Supports lasting hydration"}],
        "directions": {"text": "Apply to clean skin."},
        "sensory_description": {"value": "Lightweight cream"}, "claims": [],
    }


def test_fragrance_completeness_activates_fragrance_fields_and_not_applicable_concerns():
    product = base_product("Perfume", "Eau de Toilette")
    product["fragrance"] = {"concentration": "Eau de Toilette", "fragrance_family": "Woody Aromatic"}
    result = evaluate_completeness(product)
    assert result["category_module"] == "fragrance"
    assert "top_notes" in result["missing_high_priority_fields"]
    assert "heart_notes" in result["missing_high_priority_fields"]
    assert "base_notes" in result["missing_high_priority_fields"]
    assert "inci" in result["missing_high_priority_fields"]
    assert result["field_states"]["targeted_concerns"]["state"] == "not_applicable"
    assert result["field_states"]["inci"]["state"] == "not_researched"


def test_research_completion_is_distinct_from_information_completeness():
    product = base_product("Perfume", "Eau de Toilette")
    product["fragrance"] = {"concentration": "Eau de Toilette"}
    metadata = {name: {"researched": True} for name in (
        "fragrance_family", "top_notes", "heart_notes", "base_notes", "longevity", "sillage_projection",
        "seasonal_fit", "occasion_fit", "inci", "claims",
    )}
    result = evaluate_completeness(product, metadata)
    assert result["research_completeness"] == 100
    assert result["overall_completeness"] < 100
    assert result["field_states"]["top_notes"]["state"] == "not_found"


def test_gap_plan_is_targeted_and_fact_fields_require_direct_evidence():
    product = base_product("Perfume", "Eau de Toilette")
    product["fragrance"] = {"concentration": "Eau de Toilette", "fragrance_family": "Woody Aromatic"}
    plan = build_gap_plan(product)
    objectives = {item["field"]: item for item in plan["research_objectives"]}
    assert objectives["top_notes"]["requires_direct_evidence"] is True
    assert objectives["inci"]["requires_direct_evidence"] is True
    assert objectives["longevity"]["requires_direct_evidence"] is False


def test_quality_gate_rejects_sensory_claim_and_fixes_fragrance_directions():
    payload = {
        "product_type": {"value": "Eau de Toilette"},
        "claims": [{"name": "Fresh and clean fragrance", "status": "source_supported"}],
        "directions": {"text": "Use morning and evening as routine step 4."},
        "target_audience": {"value": ["Men who like fragrance", "People who like perfume", "Beauty lovers"]},
    }
    cleaned, rejected = quality_gate(payload, "fragrance")
    assert cleaned["claims"] == []
    assert "pulse points" in cleaned["directions"]["text"]
    assert cleaned["targeted_concerns"]["value_status"] == "not_applicable"
    assert "target_audience" in rejected


def test_category_specific_modules_do_not_cross_contaminate():
    skincare = evaluate_completeness({**base_product(), "skincare": {}})
    haircare = evaluate_completeness({**base_product("Haircare", "Shampoo"), "haircare": {}})
    makeup = evaluate_completeness({**base_product("Makeup", "Foundation"), "makeup": {}})
    assert "skin_types" in skincare["field_states"] and "top_notes" not in skincare["field_states"]
    assert "hair_types" in haircare["field_states"] and "skin_types" not in haircare["field_states"]
    assert "coverage" in makeup["field_states"] and "hair_types" not in makeup["field_states"]


def test_ysl_y_acceptance_gap_plan_is_fragrance_specific():
    ysl = {
        "brand": "YSL", "product_name": "YSL Y", "category": "Perfume",
        "product_type": "Eau de toilette", "size": "100 ml", "gtin": "3614271716026",
        "description": "A fresh woody aromatic fragrance.", "image_url": "https://example.com/ysl-y.jpg",
        "fragrance": {"concentration": "Eau de Toilette", "fragrance_family": "Woody Aromatic"},
        "target_audience": {"value": [
            "Professionals seeking a versatile fresh woody fragrance for regular daytime wear.",
            "Fragrance buyers who prefer aromatic freshness balanced by a deeper woody character.",
            "Consumers looking for a polished scent that transitions from office to evening occasions.",
        ]},
        "benefits": [{"statement": "Balances aromatic freshness with a woody signature."}],
        "directions": {"text": "Spray onto pulse points such as wrists and neck."},
        "sensory_description": {"value": "Fresh aromatic opening with a woody dry-down."},
        "product_positioning": {"value": "Versatile fresh woody signature scent."},
    }
    plan = build_gap_plan(ysl)
    assert plan["category_module"] == "fragrance"
    assert {"top_notes", "heart_notes", "base_notes", "longevity", "sillage_projection"}.issubset(
        set(plan["missing_high_priority_fields"])
    )
    assert "inci" in plan["missing_high_priority_fields"]
    assert plan["field_states"]["targeted_concerns"]["state"] == "not_applicable"
