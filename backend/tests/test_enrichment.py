import pytest
import uuid

from app.models import IngredientDefinition
from app.services.enrichment import (
    generate_deterministic_fallback,
    normalize_and_validate_enrichment,
)
from app.services.ingredient_knowledge import (
    build_ingredient_grounding_context,
    ground_fallback_ingredients,
    retrieve_ingredient_knowledge,
)

def test_deterministic_fallback_no_fabrications():
    fallback = generate_deterministic_fallback(
        name="Hydrating Moisturizer",
        brand="Cerave",
        description="A daily face lotion. vegan formula.",
        raw_ingredients="Aqua, Glycerin, Ceramide NP"
    )

    # 1. Explicit source keywords are extracted without fabricating claims
    assert fallback["subcategory"]["value"] == "moisturizer"
    assert fallback["subcategory"]["value_status"] == "explicit_source"
    assert fallback["hydration"]["targeting_status"] == "explicit"

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

    # 4. Safe catalogue defaults are populated but transparently marked inferred
    assert fallback["gender_target"]["value"] == "unisex"
    assert fallback["gender_target"]["value_status"] == "inferred"
    assert fallback["gender_target"]["confidence"] < 0.8
    assert fallback["target_audience"]["value"] == "adults"
    assert fallback["target_audience"]["value_status"] == "inferred"
    assert fallback["directions"]["source_status"] == "inferred"
    assert fallback["directions"]["text"]
    assert fallback["benefits"][0]["statement"] == "Hydration support"


def test_balanced_fallback_infers_catalogue_fields_without_inventing_sensitive_claims():
    fallback = generate_deterministic_fallback(
        name="Daily Repair Shampoo",
        brand="Example",
        description="A refreshing wash for dry hair.",
        raw_ingredients="Aqua, Sodium Cocoyl Isethionate"
    )

    assert fallback["product_type"]["value"] == "shampoo"
    assert fallback["application_area"]["value"] == "hair"
    assert fallback["gender_target"]["value"] == "unisex"
    assert fallback["target_audience"]["value"] == "adults"
    assert fallback["texture"]["value_status"] == "inferred"
    assert fallback["directions"]["source_status"] == "inferred"

    # Ethical and free-from claims still require direct source support.
    assert fallback["vegan"]["value"] == "unverified"
    assert fallback["cruelty_free"]["value"] == "unverified"
    assert fallback["paraben_free"]["value"] == "unverified"
    assert fallback["vegan"]["claim_status"] == "unverified"


def test_sparse_product_gets_complete_safe_catalogue_defaults():
    fallback = generate_deterministic_fallback(
        name="Mystery Beauty Essential",
        brand="Example",
        description="",
        raw_ingredients="",
    )

    for field in (
        "subcategory", "product_type", "gender_target", "texture",
        "application_area", "target_audience",
    ):
        assert fallback[field]["value"]
        assert fallback[field]["value_status"] != "unknown"

    assert fallback["directions"]["text"]
    assert fallback["fragrance_intelligence"]["fragrance_presence_status"] != "unknown"
    assert fallback["hydration"]["targeting_status"] == "not_targeted"


def test_cosing_exact_match_grounds_ingredient_without_inventing_claims(db):
    db.add(
        IngredientDefinition(
            id=uuid.uuid4(),
            name="GLYCERIN",
            normalized_name="glycerin",
            function="HUMECTANT, SKIN CONDITIONING",
            source_name="European Commission CosIng",
            source_url="https://example.test/cosing",
            source_record_id="123",
            regulatory_status="Active",
        )
    )
    db.flush()

    matches = retrieve_ingredient_knowledge(db, "Aqua, Glycerin, Mystery Extract")
    assert len(matches) == 1
    assert matches[0]["inci_name"] == "GLYCERIN"
    assert matches[0]["functions"] == ["HUMECTANT", "SKIN CONDITIONING"]

    context = build_ingredient_grounding_context(matches)
    assert "European Commission CosIng record 123" in context

    fallback = generate_deterministic_fallback("Product", "Brand", "", "Glycerin")
    grounded = ground_fallback_ingredients(fallback, matches)
    ingredient = grounded["ingredients_intelligence"][0]
    assert ingredient["normalized_inci_name"] == "GLYCERIN"
    assert ingredient["functions"] == ["HUMECTANT", "SKIN CONDITIONING"]
    assert ingredient["benefits"] == []
    assert ingredient["evidence"][0]["evidence_type"] == "authoritative_glossary_exact_match"


def test_ingredient_knowledge_does_not_fuzzy_match(db):
    db.add(
        IngredientDefinition(
            id=uuid.uuid4(),
            name="RETINOL",
            normalized_name="retinol",
            source_name="European Commission CosIng",
        )
    )
    db.flush()
    assert retrieve_ingredient_knowledge(db, "Retinal") == []


def test_normalization_rejects_false_retinol_and_normalizes_concern_vocabulary():
    result = normalize_and_validate_enrichment(
        {
            "fragrance": {"targeting_status": "targeted"},
            "hydration": {"targeting_status": "not_targeted"},
            "pregnancy_warning_observation": {
                "review_required": True,
                "observation_type": "retinol_present",
                "observed_items": ["retinol"],
                "review_message": "Contains retinol",
                "confidence": 0.9,
                "evidence": [{"supporting_text": "Model inference"}],
            },
        },
        "Coco-Caprylate/Caprate, Helianthus Annuus Seed Oil, Squalane, Mica, Parfum, Tocopherol",
    )

    assert result["fragrance"]["targeting_status"] == "inferred"
    assert result["hydration"]["targeting_status"] == "not_targeted"
    observation = result["pregnancy_warning_observation"]
    assert observation["review_required"] is False
    assert observation["observed_items"] == []
    assert observation["evidence"] == []


def test_normalization_requires_an_exact_retinoid_inci_item():
    result = normalize_and_validate_enrichment(
        {"pregnancy_warning_observation": {"observed_items": []}},
        "Aqua, Retinyl Palmitate, Glycerin",
    )

    observation = result["pregnancy_warning_observation"]
    assert observation["review_required"] is True
    assert observation["observed_items"] == ["retinyl palmitate"]
