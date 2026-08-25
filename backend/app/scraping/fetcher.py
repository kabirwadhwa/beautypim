import asyncio
import json
import re
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from app.scraping.schemas import CrawlConfiguration
from app.scraping.url_safety import UnsafeUrl, validate_public_url


class FetchBlocked(RuntimeError):
    pass


class ResponseTooLarge(RuntimeError):
    pass


@dataclass
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    content: bytes


async def fetch_static(url: str, config: CrawlConfiguration, conditional_headers=None) -> FetchResult:
    validate_public_url(url, expected_domain=config.domain, allow_subdomains=config.allow_subdomains)
    headers = {"User-Agent": config.user_agent, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5"}
    headers.update(conditional_headers or {})
    current = url
    async with httpx.AsyncClient(
        timeout=config.request_timeout_seconds,
        follow_redirects=False,
        verify=True,
    ) as client:
        for _ in range(config.maximum_redirects + 1):
            validate_public_url(current, expected_domain=config.domain, allow_subdomains=config.allow_subdomains)
            async with client.stream("GET", current, headers=headers) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchBlocked("Redirect response did not include a location")
                    current = urljoin(current, location)
                    continue
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > config.maximum_response_bytes:
                        raise ResponseTooLarge("Response exceeded configured size limit")
                    chunks.append(chunk)
                if response.status_code in {401, 403, 407, 429}:
                    raise FetchBlocked(f"Remote site refused crawling with HTTP {response.status_code}")
                return FetchResult(url, str(response.url), response.status_code, dict(response.headers), b"".join(chunks))
    raise FetchBlocked("Maximum redirect count exceeded")


def fetch(url: str, config: CrawlConfiguration, conditional_headers=None) -> FetchResult:
    if config.use_browser_rendering:
        return asyncio.run(fetch_browser(url, config))
    return asyncio.run(fetch_static(url, config, conditional_headers))


async def fetch_browser(url: str, config: CrawlConfiguration) -> FetchResult:
    """Render a public page in an isolated Chromium context without stealth or bypasses."""
    validate_public_url(url, expected_domain=config.domain, allow_subdomains=config.allow_subdomains)
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=config.user_agent, java_script_enabled=True)
        page = await context.new_page()
        public_review_payloads: list[dict] = []

        async def capture_public_review_response(response):
            """Retain same-domain public JSON review payloads already used by the page."""
            try:
                response_url = response.url
                validate_public_url(
                    response_url, expected_domain=config.domain,
                    allow_subdomains=config.allow_subdomains,
                )
                content_type = (await response.all_headers()).get("content-type", "").lower()
                if "json" not in content_type and not re.search(r"review|rating|ugc", response_url, re.I):
                    return
                body = await response.body()
                if not body or len(body) > 1_000_000:
                    return
                decoded = body.decode("utf-8", "replace")
                if not re.search(r"review|rating|comment|stars", decoded, re.I):
                    return
                payload = json.loads(decoded)
                public_review_payloads.append({"source_url": response_url, "payload": payload})
            except Exception:
                # A failed optional widget response must never fail the product page.
                return

        page.on("response", capture_public_review_response)

        async def guard(route):
            request_url = route.request.url
            try:
                enforce_domain = route.request.resource_type in {
                    "document", "xhr", "fetch", "eventsource", "websocket",
                }
                validate_public_url(
                    request_url,
                    expected_domain=config.domain if enforce_domain else None,
                    allow_subdomains=config.allow_subdomains,
                )
            except UnsafeUrl:
                await route.abort()
                return
            if route.request.resource_type in {"media", "font"}:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", guard)
        response = await page.goto(url, wait_until="domcontentloaded", timeout=config.request_timeout_seconds * 1000)
        if not response:
            await browser.close()
            raise FetchBlocked("Browser navigation returned no response")
        validate_public_url(page.url, expected_domain=config.domain, allow_subdomains=config.allow_subdomains)
        if response.status in {401, 403, 407, 429}:
            await browser.close()
            raise FetchBlocked(f"Remote site refused browser crawling with HTTP {response.status}")
        # Open a public review tab/accordion when the product page hides review
        # copy behind a normal client-side control. This does not submit forms,
        # authenticate, or bypass retailer protections.
        review_controls = page.locator("button, [role=tab]").filter(
            has_text=re.compile(r"^(customer\s+)?reviews?|ratings?\s*&\s*reviews?|avis clients?$", re.I)
        )
        if await review_controls.count():
            control = review_controls.first
            if await control.is_visible() and await control.is_enabled():
                try:
                    await control.click(timeout=3000)
                    await page.wait_for_timeout(750)
                except Exception:
                    pass
        # Expand public catalogue/review listings conservatively.
        for _ in range(10):
            candidates = page.locator("button").filter(
                has_text=re.compile(
                    r"(load more|show more|more reviews|read more reviews|see all reviews|"
                    r"voir plus|afficher plus|plus d['’]avis|plus de produits)",
                    re.IGNORECASE,
                )
            )
            if await candidates.count() == 0:
                break
            button = candidates.first
            if not await button.is_visible() or not await button.is_enabled():
                break
            await button.click(timeout=3000)
            await page.wait_for_timeout(750)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1000)
        content_text = await page.content()
        if public_review_payloads:
            embedded = "".join(
                '<script type="application/json" data-beautypim-review-endpoint>'
                + json.dumps(item, ensure_ascii=False).replace("</", "<\\/") + "</script>"
                for item in public_review_payloads[:20]
            )
            content_text = content_text.replace("</body>", embedded + "</body>")
        content = content_text.encode()
        if len(content) > config.maximum_response_bytes:
            await browser.close()
            raise ResponseTooLarge("Rendered page exceeded configured size limit")
        result = FetchResult(url, page.url, response.status, await response.all_headers(), content)
        await browser.close()
        return result
