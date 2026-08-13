"""Generate deterministic visual-QA dossiers for all supported category modules."""
from pathlib import Path

from app.services.product_pdf import _current_fields, _select_density, build_product_pdf

OUT = Path(__file__).resolve().parents[2] / "output" / "pdf"
OUT.mkdir(parents=True, exist_ok=True)


def field(name, value):
    return {"field_name": name, "value": value, "is_current": True}


base = {
    "brand_name": "Beauty PIM Lab", "gtin": "3614271716026", "image_url": None,
    "variants": [{"size": "100", "unit": "ml", "gtin": "3614271716026"}],
    "formulations": [], "key_ingredients": [],
    "market_observations": [{"source_name": "Retail Data", "rating": 4.6, "review_count": 218,
                             "review_summary": {"summary": "Customers praise its performance and easy everyday use."}}],
}
profiles = {"value": [
    "Shoppers seeking a clearly defined product benefit for their everyday routine.",
    "Consumers drawn to the documented texture, finish or sensory character of this formula.",
    "Busy users wanting a polished product that fits naturally into relevant daily occasions.",
]}

samples = {
    "fragrance": {
        **base, "brand_name": "YSL", "product_name": "Y", "product_category": "Perfume",
        "market_observations": [{"source_name": "Ulta", "rating": 4.8, "review_count": 2924,
                                 "review_summary": {"summary": "Praised for its versatile fresh woody profile."}}],
        "formulations": [{"raw_inci_text": "Alcohol, Parfum (Fragrance), Aqua (Water), Limonene, Linalool, Coumarin, Citral, Geraniol."}],
        "field_values": [field("product_type", "Eau de Toilette"), field("target_audience", {"value": [
            "Professionals seeking a versatile fresh woody fragrance for regular daytime wear.",
            "Fragrance buyers who prefer aromatic freshness balanced by a structured woody dry-down.",
            "Consumers wanting a polished signature scent that transitions from office to evening occasions.",
        ]}),
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
        **base, "brand_name": "Armani Beauty", "product_name": "Lip Maestro", "product_category": "Makeup",
        "gtin": "3605522075283", "variants": [{"variant_name": "405 Sultan", "gtin": "3605522075283"}],
        "field_values": [field("product_type", "Liquid Lipstick"), field("target_audience", profiles),
            field("benefits", [{"statement": "Delivers rich colour with a polished velvet finish."}]),
            field("directions", "Apply to the lips with the applicator and build to the desired intensity."),
            field("makeup", {"shade_colour": {"value": "405 Sultan"}, "coverage": {"value": "Full"},
                "finish": {"value": "Velvet"}, "texture_format": {"value": "Liquid lipstick"}})],
    },
}

for category, payload in samples.items():
    path = OUT / f"beautypim-{category}-sample.pdf"
    path.write_bytes(build_product_pdf(payload))
    density = _select_density(payload, _current_fields(payload)).name
    print(f"{path} [{density} density]")
