"""Licensed web-search discovery for product research candidates."""
from __future__ import annotations

import json
import time
import uuid
from urllib.parse import urlparse

import requests

from app.config import settings
from app.scraping.url_safety import UnsafeUrl, validate_public_url


class SearchProviderUnavailable(RuntimeError):
    pass


def _provider_status(payload: dict) -> str:
    explicit = str(payload.get("status") or "").strip()
    if explicit:
        return explicit
    return "in_progress" if payload.get("id") else "completed"


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


def start_product_source_discovery(
    *, brand: str, product_name: str, product_format: str = "",
    gtin: str = "", approved_domains: list[str] | None = None,
    research_objectives: list[str] | None = None,
) -> dict:
    """Start one durable provider request and return serializable polling state."""
    if not settings.OPENAI_API_KEY and not settings.BRAVE_SEARCH_API_KEY:
        raise SearchProviderUnavailable(
            "Live source discovery requires the existing OPENAI_API_KEY or BRAVE_SEARCH_API_KEY in Railway."
        )
    domains = _allowed_domains(approved_domains or [])
    identity = " ".join(value for value in (brand, product_name, product_format, gtin) if value)
    if not settings.OPENAI_API_KEY:
        return {
            "provider": "brave", "status": "completed", "response_id": None,
            "domains": domains, "candidates": _discover_with_brave(identity, domains),
        }
    model = settings.OPENAI_WEB_SEARCH_FALLBACK_MODEL or settings.OPENAI_WEB_SEARCH_MODEL
    payload = _start_openai_discovery(identity, domains, model, research_objectives)
    status = _provider_status(payload)
    return {
        "provider": "openai", "status": status,
        "response_id": payload.get("id"), "domains": domains, "model": model,
        "candidates": _parse_openai_candidates(payload, domains, model)
        if status not in {"queued", "in_progress"} else [],
        "market_observations": _parse_openai_market_observations(payload, domains)
        if status not in {"queued", "in_progress"} else [],
    }


def poll_product_source_discovery(state: dict) -> dict:
    """Poll a previously started request without ever creating another paid search."""
    if state.get("provider") != "openai" or state.get("status") not in {"queued", "in_progress"}:
        return state
    response_id = str(state.get("response_id") or "").strip()
    if not response_id:
        raise SearchProviderUnavailable("The live-search response ID is missing.")
    headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}
    try:
        poll = requests.get(
            f"https://api.openai.com/v1/responses/{response_id}",
            headers=headers, timeout=(10, 20),
        )
    except requests.Timeout:
        return state
    if poll.status_code != 200:
        raise SearchProviderUnavailable(
            f"OpenAI live-search status returned HTTP {poll.status_code}."
        )
    payload = poll.json()
    status = _provider_status(payload)
    updated = {**state, "status": status}
    if status in {"failed", "cancelled", "incomplete"}:
        detail = (payload.get("error") or {}).get("message") or status
        raise SearchProviderUnavailable(f"OpenAI live search ended with status {status}: {detail}")
    if status not in {"queued", "in_progress"}:
        updated["candidates"] = _parse_openai_candidates(
            payload, state.get("domains") or [], state.get("model") or settings.OPENAI_WEB_SEARCH_MODEL,
        )
        updated["market_observations"] = _parse_openai_market_observations(
            payload, state.get("domains") or [],
        )
    return updated


def _discover_with_openai(identity: str, domains: list[str]) -> list[dict]:
    # Use one economical request only. A timed-out synchronous request may
    # continue running provider-side, so launching a fallback can double bill
    # the same user action. Background mode returns an ID quickly and lets us
    # poll that one response to completion instead.
    model = settings.OPENAI_WEB_SEARCH_FALLBACK_MODEL or settings.OPENAI_WEB_SEARCH_MODEL
    return _discover_with_openai_model(identity, domains, model)


def _discover_with_openai_model(identity: str, domains: list[str], model: str) -> list[dict]:
    payload = _start_openai_discovery(identity, domains, model)
    state = {
        "provider": "openai", "status": _provider_status(payload),
        "response_id": payload.get("id"), "domains": domains, "model": model,
        "candidates": _parse_openai_candidates(payload, domains, model)
        if _provider_status(payload) not in {"queued", "in_progress"} else [],
    }
    started = time.monotonic()
    while state["status"] in {"queued", "in_progress"}:
        if time.monotonic() - started >= 120:
            raise SearchProviderUnavailable(
                "Live search is still processing after two minutes. No second request was launched."
            )
        time.sleep(2)
        state = poll_product_source_discovery(state)
    return state.get("candidates") or []


def _start_openai_discovery(identity: str, domains: list[str], model: str,
                            research_objectives: list[str] | None = None) -> dict:
    tool: dict = {
        "type": "web_search", "external_web_access": True,
        "search_context_size": "low", "search_content_types": ["text", "image"],
        "image_settings": {"max_results": 5, "caption": True},
    }
    if domains:
        tool["filters"] = {"allowed_domains": domains[:100]}
    request = {
        "model": model,
        "background": True,
        "tools": [tool], "tool_choice": "required",
        "include": ["web_search_call.action.sources", "web_search_call.results"],
        "text": {"format": {
            "type": "json_schema", "name": "product_market_evidence", "strict": True,
            "schema": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "market_observations": {
                        "type": "array", "maxItems": 8,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "source_url": {"type": "string"},
                                "source_name": {"type": ["string", "null"]},
                                "matched_gtin": {"type": ["string", "null"]},
                                "matched_brand": {"type": ["string", "null"]},
                                "matched_product_name": {"type": ["string", "null"]},
                                "matched_variant": {"type": ["string", "null"]},
                                "image_url": {"type": ["string", "null"]},
                                "average_rating": {"type": ["number", "null"], "minimum": 0, "maximum": 5},
                                "review_count": {"type": ["integer", "null"], "minimum": 0},
                                "evidence_excerpt": {"type": ["string", "null"]},
                            },
                            "required": ["source_url", "source_name", "matched_gtin", "matched_brand", "matched_product_name", "matched_variant", "image_url", "average_rating", "review_count", "evidence_excerpt"],
                        },
                    },
                },
                "required": ["market_observations"],
            },
        }},
        "input": (
            f"Find the official brand product page and reputable retailer product pages for: {identity}. "
            f"The unresolved high-value fields are: {', '.join(research_objectives or []) or 'official product evidence'}. "
            "Return only exact or plausible product-version pages; do not use search pages, blogs or editorial articles. "
            "Include results from multiple distinct domains when available: the official page for identity and imagery, "
            "plus public retailer product pages that visibly expose aggregate rating or review-count evidence. "
            "Return a market_observations JSON array containing only exact-product evidence you directly found. "
            "For each source include its page URL, the GTIN, brand, exact displayed product name and variant that the "
            "page itself supports, and, when visibly supported, a direct public product image URL, "
            "average rating, review count, and a short evidence excerpt. Use null for anything not established. "
            "Never transfer ratings, reviews or images from a sibling concentration, shade, size or different product. "
            "When ingredients or INCI is unresolved, prioritize an exact concentration/variant page that visibly exposes "
            "the complete ingredient list; never substitute a sibling EDT, EDP, Parfum, Elixir, shade or size formulation. "
            "Prioritize exact pages that also expose size, GTIN and a full ingredients/INCI section."
        ),
    }
    request_id = str(uuid.uuid4())
    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
        "X-Client-Request-Id": request_id,
    }
    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers=headers, json=request, timeout=(10, 20),
        )
    except requests.Timeout as exc:
        raise SearchProviderUnavailable(
            "Live search could not be started in time. It was not retried, to avoid duplicate API usage."
        ) from exc
    if response.status_code != 200:
        raise SearchProviderUnavailable(
            f"OpenAI live web search returned HTTP {response.status_code}. Check model access and API quota."
        )
    return response.json()


def _parse_openai_market_observations(payload: dict, domains: list[str]) -> list[dict]:
    """Extract cited exact-product market evidence from the model response.

    Retailer pages frequently refuse automated HTML fetching.  The licensed
    search response can still expose a cited image/rating aggregate; retain it
    as source-backed market evidence rather than pretending the crawl worked.
    """
    text_values = []
    cited_urls = set()
    for output in payload.get("output", []):
        if output.get("type") == "web_search_call":
            action = output.get("action") or {}
            cited_urls.update(
                str(source.get("url") or "").strip()
                for source in action.get("sources") or [] if source.get("url")
            )
            cited_urls.update(
                str(result.get("source_website_url") or "").strip()
                for result in output.get("results") or [] if result.get("source_website_url")
            )
        if output.get("type") != "message":
            continue
        for content in output.get("content") or []:
            if content.get("type") == "output_text" and content.get("text"):
                text_values.append(content["text"])
            cited_urls.update(
                str(annotation.get("url") or "").strip()
                for annotation in content.get("annotations") or []
                if annotation.get("type") == "url_citation" and annotation.get("url")
            )
    if not text_values:
        return []
    try:
        parsed = json.loads("\n".join(text_values))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    observations = parsed.get("market_observations") if isinstance(parsed, dict) else None
    if not isinstance(observations, list):
        return []
    output = []
    for value in observations[:8]:
        if not isinstance(value, dict):
            continue
        source_url = str(value.get("source_url") or "").strip()
        host = (urlparse(source_url).hostname or "").lower()
        if (not source_url or source_url not in cited_urls
                or (domains and host not in domains and not any(host.endswith(f".{domain}") for domain in domains))):
            continue
        try:
            validate_public_url(source_url, expected_domain=host, allow_subdomains=False)
        except UnsafeUrl:
            continue
        output.append({
            "source_url": source_url, "source_domain": host,
            "source_name": value.get("source_name"),
            "matched_gtin": value.get("matched_gtin"),
            "matched_brand": value.get("matched_brand"),
            "matched_product_name": value.get("matched_product_name"),
            "matched_variant": value.get("matched_variant"),
            "image_url": value.get("image_url"),
            "average_rating": value.get("average_rating"),
            "review_count": value.get("review_count"),
            "evidence_excerpt": value.get("evidence_excerpt"),
            "evidence_method": "OpenAI Responses web_search",
        })
    return output


def _parse_openai_candidates(payload: dict, domains: list[str], model: str) -> list[dict]:
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
    return _validate_candidates(candidates.values(), domains, f"OpenAI Responses web_search ({model})")


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
