from types import SimpleNamespace

from app.services.ingredient_analytics import calculate_ingredient_analytics


def test_inci_statistics_are_derived_not_enrichment_fields():
    ingredients = [
        SimpleNamespace(raw_inci_name="Glycerin", definition=SimpleNamespace(function="Humectant")),
        SimpleNamespace(raw_inci_name="Sodium Hyaluronate", definition=SimpleNamespace(function="Humectant")),
        SimpleNamespace(raw_inci_name="Tocopherol", definition=SimpleNamespace(function="Antioxidant")),
    ]
    result = calculate_ingredient_analytics(ingredients)
    assert result["total_ingredients"] == 3
    assert result["humectants"] == 2
    assert result["antioxidants"] == 1
