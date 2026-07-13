import pytest
from app.services.enrichment import generate_deterministic_fallback

def test_deterministic_fallback_no_fabrications():
    fallback = generate_deterministic_fallback(
        name="Hydrating Moisturizer",
        brand="Cerave",
        description="A daily face lotion. vegan formula.",
        raw_ingredients="Aqua, Glycerin, Ceramide NP"
    )

    # 1. AI fields must remain unknown
    assert fallback["subcategory"]["value"] is None
    assert fallback["subcategory"]["value_status"] == "unknown"
    assert fallback["hydration"]["targeting_status"] == "unknown"

    # 2. Vegan detected claim
    assert fallback["vegan"]["value"] == "yes"
    assert fallback["vegan"]["claim_status"] == "explicit_brand_claim"
    assert len(fallback["vegan"]["evidence"]) == 1
    assert fallback["vegan"]["evidence"][0]["source_field"] == "description"
    assert fallback["vegan"]["evidence"][0]["supporting_text"] == "Found keyword 'vegan' in source data."

    # 3. Ingredients must be split without concentration inference
    assert len(fallback["ingredients_intelligence"]) == 3
    assert fallback["ingredients_intelligence"][0]["ingredient_name"] == "Aqua"
    assert fallback["ingredients_intelligence"][1]["ingredient_name"] == "Glycerin"
    assert fallback["ingredients_intelligence"][2]["ingredient_name"] == "Ceramide NP"
