from app.schemas import BeautyProductEnrichmentSchema
from app.services.enrichment import (
    generate_deterministic_fallback,
    normalize_and_validate_enrichment,
    normalize_provider_shapes,
    prepare_provider_payload,
)


def test_provider_shape_is_normalized_to_current_contract():
    normalized = normalize_provider_shapes({
        "product_type": {"value": "Serum"},
        "target_audience": {"value": ["Dry-skin shoppers", "Simple-routine users", "Makeup wearers"]},
        "claims": [{"name": "Vegan", "value": None, "status": "unverified"}],
    })
    prepared = prepare_provider_payload(normalized, "Hydrating Serum", "Example", "Hydrating face serum.", "Aqua, Glycerin")
    validated = BeautyProductEnrichmentSchema.model_validate(prepared)
    assert validated.product_type.value == "Serum"
    assert len(validated.target_audience.value) == 3
    assert validated.claims[0].status == "unverified"


def test_fallback_has_exact_three_commercial_profiles_and_no_legacy_fields():
    fallback = generate_deterministic_fallback(
        "Hydrating Moisturizer", "Example", "A lightweight vegan face cream for dry skin.", "Aqua, Glycerin"
    )
    assert len(fallback["target_audience"]["value"]) == 3
    assert fallback["skincare"] is not None
    assert fallback["haircare"] is None
    assert fallback["makeup"] is None
    assert fallback["fragrance"] is None
    for removed in ("gender_target", "brand_origin", "country_of_manufacture", "launch_year",
                    "absorption_profile", "application_sequence", "skin_type_scores", "inci_stats"):
        assert removed not in fallback
    vegan = next(item for item in fallback["claims"] if item["name"] == "Vegan")
    assert vegan["status"] == "source_supported"


def test_category_modules_are_mutually_exclusive():
    cases = [
        ("Daily Face Serum", "Hydrating serum", "skincare"),
        ("Repair Shampoo", "For dry hair", "haircare"),
        ("Velvet Foundation", "Medium coverage makeup", "makeup"),
        ("Night Eau de Parfum", "Woody fragrance", "fragrance"),
    ]
    for name, description, expected in cases:
        result = BeautyProductEnrichmentSchema.model_validate(
            generate_deterministic_fallback(name, "Example", description, "")
        ).model_dump()
        active = [key for key in ("skincare", "haircare", "makeup", "fragrance") if result[key] is not None]
        assert active == [expected]


def test_claims_never_guess_positive_values():
    fallback = generate_deterministic_fallback("Plain Cream", "Example", "A face cream.", "")
    assert all(item["status"] == "unverified" and item["value"] is None for item in fallback["claims"])


def test_targeted_concerns_are_consolidated_labels_not_scores():
    fallback = generate_deterministic_fallback(
        "Brightening Acne Serum", "Example", "For acne, blemishes and pigmentation.", ""
    )
    values = fallback["targeted_concerns"]["values"]
    assert "Acne" in values
    assert "Pigmentation" in values
    assert "skin_type_scores" not in fallback


def test_ingredient_intelligence_exposes_only_product_useful_fields():
    fallback = generate_deterministic_fallback("Face Serum", "Example", "", "Aqua, Glycerin")
    ingredient = fallback["ingredients_intelligence"][0]
    assert ingredient["inci_position"] == 1
    for internal in ("normalized_inci_name", "common_name", "source_origin", "ingredient_group", "confidence", "evidence"):
        assert internal not in ingredient


def test_exact_retinoid_creates_conservative_warning():
    base = generate_deterministic_fallback("Night Serum", "Example", "", "Aqua, Retinyl Palmitate, Glycerin")
    result = normalize_and_validate_enrichment(base, "Aqua, Retinyl Palmitate, Glycerin")
    warning = next(item for item in result["warnings_considerations"] if item["type"] == "pregnancy")
    assert warning["source_status"] == "source_supported"
    assert "no medical conclusion" in warning["observation"].lower()


def test_false_retinol_substring_does_not_create_warning():
    base = generate_deterministic_fallback("Face Oil", "Example", "", "Coco-Caprylate, Tocopherol")
    result = normalize_and_validate_enrichment(base, "Coco-Caprylate, Tocopherol")
    assert not any(item["type"] == "pregnancy" for item in result["warnings_considerations"])
