from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator
import re


CrawlMode = Literal[
    "single_url", "multiple_urls", "category", "brand_catalogue",
    "sitemap", "sitemap_index", "full_domain",
]
RecrawlStrategy = Literal[
    "crawl_once", "product_pages_only", "rediscover_catalogue",
    "refresh_stale", "prices_and_availability",
]


class CrawlConfiguration(BaseModel):
    domain: str
    starting_urls: list[str] = Field(default_factory=list)
    sitemap_url: Optional[str] = None
    crawl_mode: CrawlMode = "full_domain"
    allowed_url_patterns: list[str] = Field(default_factory=list)
    denied_url_patterns: list[str] = Field(default_factory=list)
    maximum_crawl_depth: int = Field(4, ge=0, le=20)
    maximum_pages: int = Field(1000, ge=1, le=100_000)
    maximum_product_pages: int = Field(500, ge=1, le=50_000)
    maximum_runtime_seconds: int = Field(3600, ge=30, le=604800)
    maximum_discovered_urls: int = Field(10_000, ge=1, le=1_000_000)
    country: Optional[str] = None
    locale: Optional[str] = None
    use_sitemap: bool = True
    use_category_discovery: bool = True
    use_browser_rendering: bool = False
    include_editorial: bool = False
    allow_subdomains: bool = False
    respect_robots_txt: bool = True
    rescrape_interval_hours: Optional[int] = Field(None, ge=1)
    recrawl_strategy: RecrawlStrategy = "crawl_once"
    request_delay_seconds: float = Field(1.0, ge=0.25, le=60)
    per_domain_concurrency: int = Field(1, ge=1, le=5)
    retry_limit: int = Field(3, ge=0, le=10)
    request_timeout_seconds: int = Field(20, ge=2, le=120)
    maximum_response_bytes: int = Field(8_000_000, ge=10_000, le=50_000_000)
    maximum_redirects: int = Field(5, ge=0, le=10)
    browser_page_limit: int = Field(100, ge=0, le=5000)
    user_agent: str = "BeautyPIM-KnowledgeCrawler/1.0 (+contact: admin@beautypim.local)"

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, value: str) -> str:
        domain = value.strip().lower().rstrip(".")
        if (
            not domain or "/" in domain or ":" in domain or "@" in domain
            or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain)
        ):
            raise ValueError("Enter a valid DNS domain without a scheme or path")
        return domain

    @field_validator("allowed_url_patterns", "denied_url_patterns")
    @classmethod
    def valid_patterns(cls, values: list[str]) -> list[str]:
        for value in values:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"Invalid URL pattern {value!r}: {exc}") from exc
        return values


class ExtractedField(BaseModel):
    value: Any = None
    raw_value: Any = None
    path: Optional[str] = None
    method: str


class ReviewSample(BaseModel):
    """A de-identified written review retained as first-class source evidence."""

    text: str
    rating: Optional[Decimal] = None
    title: Optional[str] = None
    date: Optional[str] = None
    source_url: Optional[str] = None
    source_domain: Optional[str] = None
    locale: Optional[str] = None
    verified_purchase: Optional[bool] = None


class ScrapedProduct(BaseModel):
    source_name: str
    source_domain: str
    source_url: str
    canonical_url: str
    scraped_at: datetime
    locale: Optional[str] = None
    country: Optional[str] = None
    retailer_product_id: Optional[str] = None
    brand: Optional[str] = None
    product_name: Optional[str] = None
    subtitle: Optional[str] = None
    description: Optional[str] = None
    gtin: Optional[str] = None
    ean: Optional[str] = None
    upc: Optional[str] = None
    sku: Optional[str] = None
    mpn: Optional[str] = None
    category_path: list[str] = Field(default_factory=list)
    product_type: Optional[str] = None
    variant_name: Optional[str] = None
    size: Optional[str] = None
    unit: Optional[str] = None
    shade: Optional[str] = None
    price: Optional[Decimal] = None
    promotional_price: Optional[Decimal] = None
    currency: Optional[str] = None
    availability: Optional[str] = None
    image_urls: list[str] = Field(default_factory=list)
    ingredient_text_raw: Optional[str] = None
    ingredients: list[str] = Field(default_factory=list)
    claims: list[str] = Field(default_factory=list)
    benefits: list[str] = Field(default_factory=list)
    usage_instructions: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
    skin_types: list[str] = Field(default_factory=list)
    hair_types: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    rating: Optional[Decimal] = None
    review_count: Optional[int] = None
    review_samples: list[ReviewSample] = Field(default_factory=list)
    review_summary: dict[str, Any] = Field(default_factory=dict)
    raw_payload_reference: Optional[str] = None
    parser_version: str
    fields: dict[str, ExtractedField] = Field(default_factory=dict, exclude=True)
