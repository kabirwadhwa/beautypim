import pytest
import uuid

from app.models import IngredientDefinition
from app.services.enrichment import generate_deterministic_fallback
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
