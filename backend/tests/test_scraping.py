import json
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
    assert len(product.review_samples) == 2
    assert product.review_samples[0].text.startswith("Beautiful shade")
    assert product.review_summary["review_samples"][0]["text"].startswith("Beautiful shade")
    assert "author" not in product.review_summary["review_samples"][0]
    assert "longevity" in product.review_summary["frequently_praised_topics"]
    assert "packaging" in product.review_summary["frequent_complaint_topics"]


def test_generic_parser_persists_all_eight_public_widget_review_texts():
    reviews = ",".join(
        '{"body":{"text":"Customer review number %d gives specific and useful product feedback."},'
        '"score":%d}' % (index, 4 + (index % 2))
        for index in range(1, 9)
    )
    html = f"""
    <html><head><meta property="og:title" content="Exact Product" />
    <script type="application/json">{{"reviewWidget":{{"reviews":[{reviews}]}}}}</script>
    </head><body><h1>Exact Product</h1></body></html>
    """
    product = GenericJsonLdAdapter().parse(html, "https://retailer.example/exact-product")

    assert product.review_summary["review_sample_count"] == 8
    assert len(product.review_summary["review_samples"]) == 8
    assert product.review_summary["review_sample_rejections"] == []


def test_generic_parser_extracts_rendered_review_dom_with_metadata():
    html = """
    <html><head><meta property="og:title" content="Pure Gold Radiance Cream" /></head><body>
      <article class="review-card" data-testid="review-card">
        <h3 data-testid="review-title">Visible radiance</h3>
        <div data-testid="review-content">My skin feels smoother and looks radiant after regular use.</div>
        <span aria-label="5 out of 5 stars"></span><time datetime="2026-06-12"></time>
      </article>
    </body></html>
    """
    product = GenericJsonLdAdapter().parse(html, "https://retailer.example/pure-gold/reviews")
    sample = product.review_summary["review_samples"][0]
    assert sample == {
        "text": "My skin feels smoother and looks radiant after regular use.",
        "title": "Visible radiance", "rating": 5.0, "date": "2026-06-12",
        "source_url": "https://retailer.example/pure-gold/reviews", "locale": None,
        "source_domain": "retailer.example",
        "verified_purchase": None,
    }


def test_generic_parser_extracts_jsonld_review_records():
    html = """
    <script type="application/ld+json">{
      "@type":"Product", "name":"Pure Gold Radiance Cream",
      "review":[
        {"@type":"Review","reviewBody":"A rich cream that leaves my complexion looking luminous.",
         "headline":"Luxury texture","datePublished":"2026-04-02",
         "reviewRating":{"ratingValue":"5"}},
        {"@type":"Review","reviewBody":"The texture is nourishing without feeling overly heavy.",
         "reviewRating":{"ratingValue":"4"}}
      ]
    }</script>
    """
    product = GenericJsonLdAdapter().parse(html, "https://brand.example/pure-gold")
    assert product.review_summary["review_sample_count"] == 2
    assert product.review_summary["review_samples"][0]["title"] == "Luxury texture"


def test_generic_parser_extracts_graph_and_standalone_review_nodes():
    html = """
    <html><head><title>Pure Gold Radiance Cream Reviews</title>
    <script type="application/ld+json">{"@context":"https://schema.org","@graph":[
      {"@type":"Product","name":"Pure Gold Radiance Cream","sku":"PGRC"},
      {"@type":"Review","itemReviewed":{"name":"Pure Gold Radiance Cream"},
       "reviewBody":"This cream feels luxurious and gives my skin a visible luminous finish.",
       "reviewRating":{"ratingValue":"5"}}
    ]}</script></head><body><h1>Pure Gold Radiance Cream Reviews</h1></body></html>
    """
    product = GenericJsonLdAdapter().parse(html, "https://retailer.example/reviews/pure-gold")

    assert len(product.review_samples) == 1
    assert product.review_samples[0].rating == 5
    assert product.review_summary["review_sample_count"] == 1


def test_dedicated_review_page_extracts_unwrapped_review_body_hook():
    html = """
    <html><head><title>Pure Gold Radiance Cream customer reviews</title></head><body>
      <h1>Customer reviews</h1>
      <div class="customer-review"><h3 class="ReviewTitle">Visible glow</h3>
        <p class="ReviewBody">After several weeks my skin looks smoother and has a healthy glow.</p>
        <span aria-label="4 out of 5 stars"></span>
      </div>
    </body></html>
    """
    product = GenericJsonLdAdapter().parse(html, "https://retailer.example/reviews/product/pg")

    assert [sample.text for sample in product.review_samples] == [
        "After several weeks my skin looks smoother and has a healthy glow."
    ]
    assert product.review_samples[0].title == "Visible glow"


def test_generic_parser_extracts_reviews_from_javascript_assigned_state():
    html = """
    <meta property="og:title" content="Pure Gold Radiance Cream" />
    <script>window.__INITIAL_STATE__ = {"reviews":{"results":[
      {"content":"The finish is silky and the jar lasts longer than expected.","rating":5},
      {"content":"Comfortable on dry skin and layers well at night.","rating":4}
    ]}};</script>
    """
    product = GenericJsonLdAdapter().parse(html, "https://retailer.example/pure-gold")
    assert product.review_summary["review_sample_count"] == 2


def test_generic_parser_extracts_reviews_from_react_flight_json_string():
    inner = json.dumps({"reviews": [{
        "reviewText": "The texture feels refined and leaves a soft luminous finish.",
        "rating": 5,
    }]})
    html = f"""
    <meta property="og:title" content="Pure Gold Radiance Cream" />
    <script>self.__next_f.push([1,{json.dumps(inner)}])</script>
    """
    product = GenericJsonLdAdapter().parse(html, "https://retailer.example/pure-gold")

    assert len(product.review_samples) == 1
    assert "react_hydration_json" in product.review_summary["review_extraction_strategies"]


def test_generic_review_page_without_product_markup_extracts_multiple_reviews():
    html = """
    <html><head><title>Pure Gold Radiance Cream Reviews</title></head><body><h1>Customer reviews</h1>
      <div data-automation-id="review-card"><p data-automation-id="review-text">Leaves a soft glow and feels comforting throughout the evening.</p></div>
      <div data-automation-id="review-card"><p data-automation-id="review-text">A little rich for daytime, but excellent as a night cream.</p></div>
    </body></html>
    """
    product = GenericJsonLdAdapter().parse(html, "https://walmart.example/reviews/product/123")
    assert product is not None
    assert product.review_summary["review_sample_count"] == 2


def test_generic_parser_rejects_near_duplicate_review_text():
    html = """
    <meta property="og:title" content="Pure Gold Radiance Cream" />
    <script type="application/json">{"reviews":[
      {"text":"This cream leaves my skin soft, smooth, luminous and comfortable all day."},
      {"text":"This cream leaves my skin soft smooth luminous and comfortable all day!"}
    ]}</script>
    """
    product = GenericJsonLdAdapter().parse(html, "https://retailer.example/pure-gold")
    assert product.review_summary["review_sample_count"] == 1
    assert {row["reason"]: row["count"] for row in product.review_summary["review_sample_rejections"]}["duplicate_review_text"] >= 1


def test_generic_parser_counts_and_rejects_empty_review_bodies():
    html = """
    <meta property="og:title" content="Pure Gold Radiance Cream" />
    <script type="application/json">{"reviews":[
      {"text":""}, {"title":"No body supplied","rating":5}
    ]}</script>
    """
    product = GenericJsonLdAdapter().parse(html, "https://retailer.example/pure-gold")
    assert product.review_summary["raw_review_candidate_count"] >= 2
    assert product.review_summary["review_sample_count"] == 0
    reasons = {row["reason"]: row["count"] for row in product.review_summary["review_sample_rejections"]}
    assert reasons["missing_review_text"] >= 2


def test_generic_parser_rejects_review_for_a_different_product():
    html = """
    <script type="application/ld+json">{
      "@type":"Product", "name":"Pure Gold Radiance Cream",
      "review":[{"@type":"Review","itemReviewed":{"name":"Different Night Serum"},
        "reviewBody":"This serum feels lightweight and absorbs very quickly into my skin."}]
    }</script>
    """
    product = GenericJsonLdAdapter().parse(html, "https://retailer.example/pure-gold")
    assert product.review_summary["review_sample_count"] == 0
    reasons = {row["reason"]: row["count"] for row in product.review_summary["review_sample_rejections"]}
    assert reasons["wrong_product_review"] >= 1


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


def test_generic_parser_extracts_objective_business_copy_from_hydration_state():
    html = """
    <script type="application/ld+json">
      {"@type":"Product","name":"Barrier Cream","brand":{"name":"Maison Test"}}
    </script>
    <script id="__NEXT_DATA__" type="application/json">
      {"props":{"pageProps":{"product":{
        "longDescription":"A restorative face cream designed to support a comfortable, supple skin feel.",
        "keyBenefits":["Supports the moisture barrier","Leaves skin feeling soft"],
        "howToUse":"Apply a small amount to clean face and neck morning or evening."
      }}}}
    </script>
    """
    product = GenericJsonLdAdapter().parse(html, "https://example.com/barrier-cream")
    assert product.description.startswith("A restorative face cream")
    assert product.benefits == ["Supports the moisture barrier", "Leaves skin feeling soft"]
    assert product.usage_instructions.startswith("Apply a small amount")
    assert product.fields["description"].method == "embedded_application_json"


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
