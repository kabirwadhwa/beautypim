from pathlib import Path
from unittest.mock import patch

import pytest

from app.scraping.adapters.generic import GenericJsonLdAdapter
from app.scraping.adapters.retail_site import RetailSiteAdapter
from app.scraping.classification import classify_page
from app.scraping.discovery import discover_links, parse_sitemap
from app.scraping.ingredients import normalize_ingredient, split_inci
from app.scraping.url_safety import UnsafeUrl, normalize_url, validate_public_url

FIXTURES = Path(__file__).parent / "fixtures" / "crawl"


def test_sitemap_and_index_parsing():
    kind, urls = parse_sitemap((FIXTURES / "sitemap.xml").read_bytes())
    assert kind == "sitemap"
    assert urls == [
        "https://shop.example.com/product/moon-serum",
        "https://shop.example.com/category/skincare",
    ]
    kind, urls = parse_sitemap((FIXTURES / "sitemap-index.xml").read_bytes())
    assert kind == "sitemap_index"
    assert len(urls) == 2


def test_url_normalization_deduplicates_tracking_and_facets():
    value = normalize_url(
        "HTTPS://Shop.Example.com/product/moon-serum/?utm_source=x&sort=price&shade=blue#reviews"
    )
    assert value == "https://shop.example.com/product/moon-serum?shade=blue"


@patch("app.scraping.url_safety.socket.getaddrinfo")
def test_ssrf_and_same_domain_protection(resolve):
    resolve.return_value = [(None, None, None, None, ("93.184.216.34", 443))]
    assert validate_public_url(
        "https://shop.example.com/p/a", expected_domain="shop.example.com"
    ) == "shop.example.com"
    with pytest.raises(UnsafeUrl):
        validate_public_url("https://evil.example/p/a", expected_domain="shop.example.com")
    resolve.return_value = [(None, None, None, None, ("127.0.0.1", 443))]
    with pytest.raises(UnsafeUrl):
        validate_public_url("https://shop.example.com/p/a", expected_domain="shop.example.com")


def test_product_classification_and_pagination_discovery():
    html = (FIXTURES / "generic-product.html").read_text()
    result = classify_page("https://shop.example.com/product/moon-serum", html)
    assert result.page_type == "product"
    assert "schema.org Product" in result.reasons
    links = discover_links(
        '<a href="/product/a?utm_source=x">A</a><a href="/product/a">A2</a>',
        "https://shop.example.com/category",
    )
    assert links == ["https://shop.example.com/product/a"]


def test_generic_jsonld_product_parsing():
    product = GenericJsonLdAdapter().parse(
        (FIXTURES / "generic-product.html").read_text(),
        "https://shop.example.com/product/moon-serum",
        country="FR", locale="en-FR",
    )
    assert product.product_name == "Moon Glass Barrier Serum"
    assert product.brand == "Lunar Atelier"
    assert product.gtin == "3760000012345"
    assert str(product.price) == "42.90"
    assert product.currency == "EUR"
    assert product.review_count == 128
    assert product.review_summary["average_rating"] == 4.7
    assert product.review_summary["review_count"] == 128
    assert product.review_summary["review_sample_count"] == 0
    assert "review text retained" in product.review_summary["summary_method"]
    assert product.canonical_url == "https://shop.example.com/product/moon-serum"
    assert product.fields["product_name"].method == "json_ld"


def test_generic_official_page_open_graph_fallback():
    html = """
    <html><head>
      <meta property="og:title" content="Burberry Goddess Eau de Parfum" />
      <meta property="og:description" content="A vanilla-led fragrance." />
      <meta property="og:image" content="https://cdn.brand.example/goddess.jpg" />
      <link rel="canonical" href="https://brand.example/goddess" />
    </head><body><h1>Burberry Goddess</h1></body></html>
    """
    product = GenericJsonLdAdapter().parse(html, "https://brand.example/c/goddess")

    assert product.product_name == "Burberry Goddess Eau de Parfum"
    assert product.description == "A vanilla-led fragrance."
    assert product.image_urls == ["https://cdn.brand.example/goddess.jpg"]
    assert product.fields["image_urls"].method == "open_graph"


def test_generic_parser_extracts_reviews_from_embedded_application_state():
    html = """
    <html><head>
      <meta property="og:title" content="Evidence Lip Colour" />
      <script id="__NEXT_DATA__" type="application/json">{
        "props": {"product": {"aggregateRating": {"ratingValue": "4.6", "reviewCount": "81"},
        "reviews": [
          {"reviewBody": "Beautiful shade and long lasting wear", "reviewRating": {"ratingValue": 5}},
          {"reviewBody": "Packaging feels expensive", "reviewRating": {"ratingValue": 2}}
        ]}}
      }</script>
    </head><body><h1>Evidence Lip Colour</h1></body></html>
    """
    product = GenericJsonLdAdapter().parse(html, "https://brand.example/lip")
    assert str(product.rating) == "4.6"
    assert product.review_count == 81
    assert product.review_summary["review_sample_count"] == 2
    assert len(product.review_summary["review_samples"]) == 2
    assert product.review_summary["review_samples"][0]["text"].startswith("Beautiful shade")
    assert "author" not in product.review_summary["review_samples"][0]
    assert "longevity" in product.review_summary["frequently_praised_topics"]
    assert "packaging" in product.review_summary["frequent_complaint_topics"]


def test_generic_parser_prefers_rich_pdp_copy_and_extracts_bulleted_inci():
    html = """
    <html><head>
      <script type="application/ld+json">{
        "@type":"Product", "name":"Y Eau de Toilette", "brand":{"name":"YSL"},
        "description":"A fresh fragrance.", "image":"https://example.com/y.jpg"
      }</script>
    </head><body>
      <div class="c-accordion__content">
        <p class="pdp_description_content">This mineral woody fragrance contrasts fresh lavender and aromatic clary sage
        with a crisp geranium heart. Freshness is matched by sensual woods, ambergris and addictive incense,
        with source-stated lasting intensity suitable for a polished everyday fragrance profile.</p>
      </div>
      <div id="description">
        <p class="pdp_description_content">ALCOHOL ● AQUA / WATER ● PARFUM / FRAGRANCE ● LIMONENE ●
        COUMARIN ● LINALOOL ● GERANIOL ● CITRAL ●</p>
      </div>
    </body></html>
    """
    product = GenericJsonLdAdapter().parse(html, "https://example.com/y-edt")
    assert product is not None
    assert product.description.startswith("This mineral woody fragrance")
    assert "ALCOHOL" in product.ingredient_text_raw
    assert len(product.ingredients) >= 7


def test_generic_parser_extracts_inci_from_embedded_application_json():
    html = """
    <html><head>
      <script type="application/ld+json">
        {"@type":"Product","name":"Exact Eau de Toilette","brand":{"name":"Maison Test"}}
      </script>
      <script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"product":{"fullIngredients":"Alcohol, Parfum, Aqua, Limonene, Linalool"}}}}
      </script>
    </head><body><h1>Exact Eau de Toilette</h1></body></html>
    """
    product = GenericJsonLdAdapter().parse(html, "https://example.com/exact-edt")
    assert product.ingredient_text_raw == "Alcohol, Parfum, Aqua, Limonene, Linalool"
    assert product.ingredients == ["Alcohol", "Parfum", "Aqua", "Limonene", "Linalool"]
    assert product.fields["ingredient_text_raw"].method == "embedded_application_json"
    assert product.fields["ingredient_text_raw"].path.endswith("fullIngredients")


def test_retail_site_adapter_and_inci_order():
    product = RetailSiteAdapter().parse(
        (FIXTURES / "retail-product.html").read_text(),
        "https://retail-data.invalid/products/velours-cream",
    )
    assert product.product_name == "Crème Velours Nuit"
    assert product.country == "FR"
    assert product.ingredient_text_raw.startswith("Aqua, Glycerin")
    assert product.usage_instructions.startswith("Appliquer")
    ingredients = split_inci(product.ingredient_text_raw)
    assert ingredients[2] == "Butyrospermum Parkii (Shea) Butter"
    assert normalize_ingredient(ingredients[2]) == "butyrospermum parkii shea butter"
