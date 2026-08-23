import json
from unittest.mock import Mock, patch

import pytest

from app.config import settings
from app.scraping.url_safety import UnsafeUrl
from app.services.web_discovery import (
    SearchProviderUnavailable, discover_product_sources,
    _parse_openai_market_observations, poll_product_source_discovery,
    start_product_source_discovery,
)


def test_discovery_requires_licensed_provider_key(monkeypatch):
    monkeypatch.setattr(settings, "BRAVE_SEARCH_API_KEY", None)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    with pytest.raises(SearchProviderUnavailable):
        discover_product_sources(brand="Example", product_name="Moon Serum")


def test_configured_domain_allowlist_restricts_requested_domains(monkeypatch):
    monkeypatch.setattr(settings, "WEB_RESEARCH_ALLOWED_DOMAINS", "brand.example, retailer.example")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "key")
    with patch("app.services.web_discovery._discover_with_openai", return_value=[]) as discover:
        discover_product_sources(
            brand="Example", product_name="Moon Serum",
            approved_domains=["brand.example", "unapproved.example"],
        )
    assert discover.call_args.args[1] == ["brand.example"]


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

    assert results[0]["provider"].startswith("OpenAI Responses web_search")
    assert results[0]["image_url"] == "https://cdn.brand.example/moon.jpg"
    request_json = post.call_args.kwargs["json"]
    assert request_json["tools"][0]["external_web_access"] is True
    assert request_json["tools"][0]["filters"]["allowed_domains"] == ["brand.example"]


@patch("app.services.web_discovery.validate_public_url")
@patch("app.services.web_discovery.requests.post")
def test_openai_citations_are_discovered_and_unsafe_urls_are_rejected(post, validate, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "openai-test-key")
    monkeypatch.setattr(settings, "BRAVE_SEARCH_API_KEY", None)
    monkeypatch.setattr(settings, "WEB_RESEARCH_ALLOWED_DOMAINS", None)
    post.return_value = Mock(status_code=200)
    post.return_value.json.return_value = {"output": [{
        "type": "message", "content": [{"annotations": [
            {"type": "url_citation", "url": "https://brand.example/product/moon", "title": "Moon"},
            {"type": "other"},
            {"type": "url_citation", "url": "http://127.0.0.1/private", "title": "Unsafe"},
        ]}],
    }]}
    validate.side_effect = [None, UnsafeUrl("private address")]

    results = discover_product_sources(brand="Example", product_name="Moon Serum")

    assert [item["url"] for item in results] == ["https://brand.example/product/moon"]
    assert results[0]["title"] == "Moon"


@patch("app.services.web_discovery.validate_public_url")
def test_openai_market_observations_retain_cited_image_and_review_aggregate(validate):
    payload = {"output": [{
        "type": "web_search_call", "action": {"sources": [{
            "url": "https://retailer.example/product/moon", "title": "Moon",
        }]},
    }, {
        "type": "message", "content": [{
            "type": "output_text", "text": json.dumps({"market_observations": [{
                "source_url": "https://retailer.example/product/moon",
                "source_name": "Retailer", "matched_gtin": "1234567890123",
                "matched_brand": "Example", "matched_product_name": "Moon Serum",
                "matched_variant": None, "image_url": "https://cdn.example/moon.jpg",
                "average_rating": 4.7, "review_count": 812,
                "evidence_excerpt": "Rated 4.7 from 812 reviews",
            }]})
        }],
    }]}

    values = _parse_openai_market_observations(payload, [])

    assert values[0]["image_url"] == "https://cdn.example/moon.jpg"
    assert values[0]["average_rating"] == 4.7
    assert values[0]["review_count"] == 812
    validate.assert_called_once()


@patch("app.services.web_discovery.validate_public_url")
@patch("app.services.web_discovery.requests.post")
def test_openai_discovery_does_not_duplicate_a_timed_out_request(post, validate, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "openai-test-key")
    monkeypatch.setattr(settings, "BRAVE_SEARCH_API_KEY", None)
    import requests
    post.side_effect = requests.Timeout("slow provider")

    with pytest.raises(SearchProviderUnavailable, match="not retried"):
        discover_product_sources(brand="Example", product_name="Moon Serum")

    assert post.call_count == 1


@patch("app.services.web_discovery.time.sleep")
@patch("app.services.web_discovery.validate_public_url")
@patch("app.services.web_discovery.requests.get")
@patch("app.services.web_discovery.requests.post")
def test_openai_background_search_polls_one_response(post, get, validate, sleep, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "openai-test-key")
    monkeypatch.setattr(settings, "BRAVE_SEARCH_API_KEY", None)
    post.return_value = Mock(status_code=200)
    post.return_value.json.return_value = {"id": "resp_123", "status": "queued", "output": []}
    get.return_value = Mock(status_code=200)
    get.return_value.json.return_value = {
        "id": "resp_123", "status": "completed", "output": [{
            "type": "web_search_call",
            "action": {"sources": [{"url": "https://brand.example/product/moon", "title": "Official"}]},
        }],
    }

    results = discover_product_sources(brand="Example", product_name="Moon Serum")

    assert len(results) == 1
    assert post.call_count == 1
    get.assert_called_once_with(
        "https://api.openai.com/v1/responses/resp_123",
        headers={"Authorization": "Bearer openai-test-key"}, timeout=(10, 20),
    )
    assert post.call_args.kwargs["json"]["background"] is True


@patch("app.services.web_discovery.requests.get")
@patch("app.services.web_discovery.requests.post")
def test_durable_discovery_persists_and_polls_the_same_response(post, get, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "openai-test-key")
    monkeypatch.setattr(settings, "BRAVE_SEARCH_API_KEY", None)
    post.return_value = Mock(status_code=200)
    post.return_value.json.return_value = {"id": "resp_durable", "status": "queued", "output": []}
    get.return_value = Mock(status_code=200)
    get.return_value.json.return_value = {
        "id": "resp_durable", "status": "completed", "output": [],
    }

    state = start_product_source_discovery(brand="YSL", product_name="Y", product_format="Eau de Toilette")
    assert state["response_id"] == "resp_durable"
    assert state["status"] == "queued"

    completed = poll_product_source_discovery(state)
    assert completed["status"] == "completed"
    assert post.call_count == 1
    get.assert_called_once()


@pytest.mark.parametrize("provider", ["openai", "brave"])
def test_discovery_reports_provider_http_errors(provider, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "key" if provider == "openai" else None)
    monkeypatch.setattr(settings, "BRAVE_SEARCH_API_KEY", "key" if provider == "brave" else None)
    monkeypatch.setattr(settings, "WEB_RESEARCH_ALLOWED_DOMAINS", None)
    request_target = "app.services.web_discovery.requests.post" if provider == "openai" else "app.services.web_discovery.requests.get"
    with patch(request_target, return_value=Mock(status_code=429)):
        with pytest.raises(SearchProviderUnavailable, match="HTTP 429"):
            discover_product_sources(brand="Example", product_name="Moon Serum")
