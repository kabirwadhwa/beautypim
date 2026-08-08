"""Derived formulation analytics; values are never persisted as product attributes."""

from collections import Counter
from typing import Any


FUNCTION_GROUPS = {
    "allergens": ("allergen",),
    "fragrance_ingredients": ("fragrance", "perfuming"),
    "plant_extracts": ("plant extract", "botanical"),
    "peptides": ("peptide",),
    "antioxidants": ("antioxidant",),
    "humectants": ("humectant",),
    "emollients_oils": ("emollient", "oil"),
    "preservatives": ("preservative",),
}


def calculate_ingredient_analytics(ingredients: list[Any]) -> dict[str, int]:
    """Calculate counts from ordered ingredient relationships/definitions on demand."""
    counts = Counter(total_ingredients=len(ingredients))
    for ingredient in ingredients:
        definition = getattr(ingredient, "definition", None)
        function = getattr(definition, "function", None) or getattr(ingredient, "function", None) or ""
        name = getattr(ingredient, "raw_inci_name", None) or getattr(ingredient, "ingredient_name", None) or ""
        searchable = f"{name} {function}".lower()
        for group, terms in FUNCTION_GROUPS.items():
            if any(term in searchable for term in terms):
                counts[group] += 1
    return {"total_ingredients": counts["total_ingredients"], **{
        group: counts[group] for group in FUNCTION_GROUPS
    }}
