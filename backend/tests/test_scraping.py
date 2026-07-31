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
    assert product.canonical_url == "https://shop.example.com/product/moon-serum"
    assert product.fields["product_name"].method == "json_ld"


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
