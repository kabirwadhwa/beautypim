from unittest.mock import Mock, patch

import pytest

from app.config import settings
from app.services.web_discovery import SearchProviderUnavailable, discover_product_sources


def test_discovery_requires_licensed_provider_key(monkeypatch):
    monkeypatch.setattr(settings, "BRAVE_SEARCH_API_KEY", None)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    with pytest.raises(SearchProviderUnavailable):
        discover_product_sources(brand="Example", product_name="Moon Serum")


@patch("app.services.web_discovery.validate_public_url")
@patch("app.services.web_discovery.requests.get")
def test_discovery_filters_to_approved_domains(get, validate, monkeypatch):
    monkeypatch.setattr(settings, "BRAVE_SEARCH_API_KEY", "test-key")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    monkeypatch.setattr(settings, "WEB_RESEARCH_ALLOWED_DOMAINS", None)
    response = Mock(status_code=200)
    response.json.return_value = {"web": {"results": [
        {"title": "Official product", "url": "https://brand.example/product/moon", "description": "Product page"},
        {"title": "Unapproved blog", "url": "https://blog.example/moon", "description": "Article"},
    ]}}
    get.return_value = response

    results = discover_product_sources(
        brand="Example", product_name="Moon Serum",
        approved_domains=["brand.example"],
    )

    assert [item["url"] for item in results] == ["https://brand.example/product/moon"]
    assert results[0]["candidate_only"] is True
    assert "site:brand.example" in get.call_args.kwargs["params"]["q"]
    validate.assert_called_once()


@patch("app.services.web_discovery.validate_public_url")
@patch("app.services.web_discovery.requests.post")
def test_openai_web_search_sources_and_images_are_candidates(post, validate, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "openai-test-key")
    monkeypatch.setattr(settings, "BRAVE_SEARCH_API_KEY", None)
    response = Mock(status_code=200)
    response.json.return_value = {"output": [{
        "type": "web_search_call",
        "action": {"sources": [{"url": "https://brand.example/product/moon", "title": "Official"}]},
        "results": [{
            "type": "image_result", "source_website_url": "https://brand.example/product/moon",
            "image_url": "https://cdn.brand.example/moon.jpg", "caption": "Moon Serum",
        }],
    }]}
    post.return_value = response

    results = discover_product_sources(
        brand="Example", product_name="Moon Serum", approved_domains=["brand.example"],
    )

    assert results[0]["provider"] == "OpenAI Responses web_search"
    assert results[0]["image_url"] == "https://cdn.brand.example/moon.jpg"
    request_json = post.call_args.kwargs["json"]
    assert request_json["tools"][0]["external_web_access"] is True
    assert request_json["tools"][0]["filters"]["allowed_domains"] == ["brand.example"]
