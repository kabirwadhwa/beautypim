"""Licensed web-search discovery for product research candidates."""
from __future__ import annotations

from urllib.parse import urlparse

import requests

from app.config import settings
from app.scraping.url_safety import UnsafeUrl, validate_public_url


class SearchProviderUnavailable(RuntimeError):
    pass


def _allowed_domains(requested: list[str]) -> list[str]:
    configured = [
        value.strip().lower() for value in (settings.WEB_RESEARCH_ALLOWED_DOMAINS or "").split(",")
        if value.strip()
    ]
    clean_requested = [value.strip().lower() for value in requested if value.strip()]
    if configured and clean_requested:
        return [domain for domain in clean_requested if domain in configured]
    return clean_requested or configured


def discover_product_sources(
    *, brand: str, product_name: str, product_format: str = "",
    gtin: str = "", approved_domains: list[str] | None = None,
) -> list[dict]:
    """Return search candidates; never treats snippets as product evidence."""
    if not settings.OPENAI_API_KEY and not settings.BRAVE_SEARCH_API_KEY:
        raise SearchProviderUnavailable(
            "Live source discovery requires the existing OPENAI_API_KEY or BRAVE_SEARCH_API_KEY in Railway."
        )
    domains = _allowed_domains(approved_domains or [])
    identity = " ".join(value for value in (brand, product_name, product_format, gtin) if value)
    if settings.OPENAI_API_KEY:
        return _discover_with_openai(identity, domains)
    return _discover_with_brave(identity, domains)


def _discover_with_openai(identity: str, domains: list[str]) -> list[dict]:
    tool: dict = {
        "type": "web_search", "external_web_access": True,
        "search_context_size": "low", "search_content_types": ["text", "image"],
        "image_settings": {"max_results": 5, "caption": True},
    }
    if domains:
        tool["filters"] = {"allowed_domains": domains[:100]}
    request = {
        "model": settings.OPENAI_WEB_SEARCH_MODEL,
        "tools": [tool], "tool_choice": "required",
        "include": ["web_search_call.action.sources", "web_search_call.results"],
        "input": (
            f"Find the official brand product page and reputable retailer product pages for: {identity}. "
            "Return only exact or plausible product-version pages; do not use search pages, blogs or editorial articles."
        ),
    }
    response = None
    last_error = None
    for _attempt in range(2):
        try:
            response = requests.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=request, timeout=75,
            )
            break
        except requests.Timeout as exc:
            last_error = exc
    if response is None:
        raise SearchProviderUnavailable(
            f"OpenAI live web search timed out after two attempts: {last_error}"
        )
    if response.status_code != 200:
        raise SearchProviderUnavailable(
            f"OpenAI live web search returned HTTP {response.status_code}. Check model access and API quota."
        )
    payload = response.json()
    candidates: dict[str, dict] = {}
    for output in payload.get("output", []):
        if output.get("type") == "web_search_call":
            action = output.get("action") or {}
            for source in action.get("sources") or []:
                url = str(source.get("url") or "").strip()
                if url:
                    candidates.setdefault(url, {
                        "title": source.get("title"), "url": url,
                        "snippet": None, "image_url": None,
                    })
            for result in output.get("results") or []:
                if result.get("type") == "image_result":
                    page_url = str(result.get("source_website_url") or "").strip()
                    if page_url:
                        candidate = candidates.setdefault(page_url, {
                            "title": result.get("caption"), "url": page_url, "snippet": None,
                        })
                        candidate["image_url"] = result.get("image_url")
        if output.get("type") == "message":
            for content in output.get("content") or []:
                for annotation in content.get("annotations") or []:
                    if annotation.get("type") != "url_citation":
                        continue
                    url = str(annotation.get("url") or "").strip()
                    if url:
                        candidates.setdefault(url, {
                            "title": annotation.get("title"), "url": url,
                            "snippet": None, "image_url": None,
                        })
    return _validate_candidates(candidates.values(), domains, "OpenAI Responses web_search")


def _validate_candidates(values, domains: list[str], provider: str) -> list[dict]:
    output = []
    for item in values:
        url = str(item.get("url") or "").strip()
        host = (urlparse(url).hostname or "").lower()
        if not url or (domains and host not in domains and not any(host.endswith(f".{domain}") for domain in domains)):
            continue
        try:
            validate_public_url(url, expected_domain=host, allow_subdomains=False)
        except UnsafeUrl:
            continue
        output.append({
            **item, "domain": host, "provider": provider, "candidate_only": True,
        })
    return output[:10]


def _discover_with_brave(identity: str, domains: list[str]) -> list[dict]:
    query = f'"{identity}" product'
    if domains:
        query += " (" + " OR ".join(f"site:{domain}" for domain in domains[:8]) + ")"
    response = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": 10, "safesearch": "moderate", "search_lang": "en"},
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": settings.BRAVE_SEARCH_API_KEY,
        },
        timeout=20,
    )
    if response.status_code != 200:
        raise SearchProviderUnavailable(
            f"Search provider returned HTTP {response.status_code}. Check the configured API key and quota."
        )
    candidates = []
    for item in (response.json().get("web") or {}).get("results", []):
        url = str(item.get("url") or "").strip()
        candidates.append({
            "title": item.get("title"), "url": url,
            "snippet": item.get("description"), "image_url": None,
        })
    return _validate_candidates(candidates, domains, "Brave Search API")
