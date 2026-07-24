import asyncio
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
        # Expand public catalogue listings conservatively. This does not submit
        # forms, authenticate, or interact with basket/account controls.
        for _ in range(10):
            candidates = page.locator("button").filter(
                has_text=re.compile(
                    r"(load more|show more|voir plus|afficher plus|plus de produits)",
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
        await page.wait_for_timeout(500)
        content = (await page.content()).encode()
        if len(content) > config.maximum_response_bytes:
            await browser.close()
            raise ResponseTooLarge("Rendered page exceeded configured size limit")
        result = FetchResult(url, page.url, response.status, await response.all_headers(), content)
        await browser.close()
        return result
