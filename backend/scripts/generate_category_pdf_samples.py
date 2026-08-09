"""Generate deterministic visual-QA dossiers for all supported category modules."""
from pathlib import Path

from app.services.product_pdf import build_product_pdf

OUT = Path(__file__).resolve().parents[2] / "output" / "pdf"
OUT.mkdir(parents=True, exist_ok=True)


def field(name, value):
    return {"field_name": name, "value": value, "is_current": True}


base = {
    "brand_name": "Beauty PIM Lab", "gtin": "3614271716026", "image_url": None,
    "variants": [{"size": "100", "unit": "ml", "gtin": "3614271716026"}],
    "formulations": [], "key_ingredients": [],
}
profiles = {"value": [
    "Shoppers seeking a clearly defined product benefit for their everyday routine.",
    "Consumers drawn to the documented texture, finish or sensory character of this formula.",
    "Busy users wanting a polished product that fits naturally into relevant daily occasions.",
]}

samples = {
    "fragrance": {
        **base, "product_name": "Y Eau de Toilette", "product_category": "Perfume",
        "field_values": [field("product_type", "Eau de Toilette"), field("target_audience", profiles),
            field("product_positioning", "A versatile fresh woody signature scent for office-to-evening wear."),
            field("benefits", [{"statement": "Balances aromatic freshness with a structured woody dry-down."}]),
            field("directions", {"text": "Spray onto pulse points such as the wrists and neck. Reapply as desired."}),
            field("sensory_description", "Fresh aromatic opening with a polished woody dry-down."),
            field("claims", []), field("fragrance", {"concentration": "Eau de Toilette", "fragrance_family": "Woody Aromatic",
                "top_notes": ["Bergamot", "Ginger"], "heart_notes": ["Sage", "Juniper berry"],
                "base_notes": ["Cedarwood", "Vetiver"], "longevity": "Moderate", "sillage_projection": "Moderate",
                "seasonal_fit": ["Spring", "Summer", "Early autumn"], "occasion_fit": ["Office", "Daytime", "Social evening"]})],
    },
    "skincare": {
        **base, "product_name": "Barrier Cloud Cream", "product_category": "Skincare",
        "formulations": [{"raw_inci_text": "Aqua, Glycerin, Ceramide NP, Squalane, Panthenol"}],
        "key_ingredients": [{"ingredient_name": "Ceramide NP", "functions": ["Skin conditioning"], "benefits": ["Barrier support"], "is_key_ingredient": True}],
        "field_values": [field("product_type", "Moisturiser"), field("target_audience", profiles), field("benefits", [{"statement": "Supports lasting hydration and barrier comfort."}]),
            field("directions", "Apply to clean skin morning and evening."), field("targeted_concerns", {"values": ["Dryness", "Barrier discomfort"]}),
            field("skincare", {"skin_types": {"recommended_for": ["Dry", "Sensitive"]}, "texture": {"value": "Cream"}, "finish": {"value": "Comfortable satin"}})],
    },
    "haircare": {
        **base, "product_name": "Scalp Balance Shampoo", "product_category": "Haircare",
        "field_values": [field("product_type", "Shampoo"), field("target_audience", profiles), field("benefits", [{"statement": "Cleanses while supporting scalp comfort."}]),
            field("directions", "Massage into wet hair and scalp, then rinse thoroughly."), field("targeted_concerns", {"values": ["Scalp dryness", "Build-up"]}),
            field("haircare", {"hair_types": {"recommended_for": ["Normal", "Oily roots"]}, "texture_format": {"value": "Gel shampoo"}})],
    },
    "makeup": {
        **base, "product_name": "Second Skin Foundation", "product_category": "Makeup",
        "field_values": [field("product_type", "Foundation"), field("target_audience", profiles), field("benefits", [{"statement": "Evens the look of complexion with buildable coverage."}]),
            field("directions", "Blend a small amount from the centre of the face outward."),
            field("makeup", {"shade_colour": {"value": "Medium Neutral"}, "coverage": {"value": "Medium, buildable"},
                "finish": {"value": "Natural satin"}, "texture_format": {"value": "Fluid"}})],
    },
}

for category, payload in samples.items():
    path = OUT / f"beautypim-{category}-sample.pdf"
    path.write_bytes(build_product_pdf(payload))
    print(path)
