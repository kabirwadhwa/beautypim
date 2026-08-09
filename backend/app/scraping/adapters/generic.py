import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.scraping import PARSER_VERSION
from app.scraping.adapters.base import ProductAdapter
from app.scraping.schemas import ExtractedField, ScrapedProduct
from app.scraping.url_safety import normalize_url
from app.scraping.ingredients import split_inci


def _nodes(payload):
    values = payload if isinstance(payload, list) else [payload]
    for value in values:
        if not isinstance(value, dict):
            continue
        yield value
        for child in value.get("@graph", []):
            if isinstance(child, dict):
                yield child


def _product_node(soup: BeautifulSoup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _nodes(payload):
            types = node.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if any(str(value).lower() == "product" for value in types):
                return node
    return None


def _text(value):
    if isinstance(value, dict):
        return value.get("name") or value.get("@id")
    if isinstance(value, list):
        return _text(value[0]) if value else None
    return str(value).strip() if value is not None else None


def _decimal(value):
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        match = re.search(r"\d+(?:[.,]\d+)?", str(value))
        return Decimal(match.group().replace(",", ".")) if match else None


def _review_summary(node: dict, aggregate: dict, source_url: str) -> dict:
    """Aggregate review signals without copying customer review text."""
    reviews = node.get("review") or []
    if isinstance(reviews, dict):
        reviews = [reviews]
    topics = {
        "longevity": ("long lasting", "long-lasting", "longevity", "lasts", "fade"),
        "sillage": ("sillage", "projection", "projects", "trail"),
        "packaging": ("packaging", "bottle", "pump", "cap", "box"),
        "texture": ("texture", "sticky", "greasy", "lightweight", "absorbs"),
        "scent": ("scent", "fragrance", "smell", "notes", "aroma"),
        "value": ("price", "expensive", "value", "worth"),
    }
    positive = {key: 0 for key in topics}
    negative = {key: 0 for key in topics}
    distribution: dict[str, int] = {}
    for review in reviews[:250]:
        if not isinstance(review, dict):
            continue
        body = str(review.get("reviewBody") or review.get("description") or "").lower()
        rating_obj = review.get("reviewRating") or {}
        rating = _decimal(rating_obj.get("ratingValue") if isinstance(rating_obj, dict) else rating_obj)
        if rating is not None:
            bucket = str(max(1, min(5, int(round(float(rating))))))
            distribution[bucket] = distribution.get(bucket, 0) + 1
        for topic, keywords in topics.items():
            if not any(keyword in body for keyword in keywords):
                continue
            if rating is not None and rating <= 2:
                negative[topic] += 1
            elif rating is None or rating >= 4:
                positive[topic] += 1
    praised = [key for key, count in sorted(positive.items(), key=lambda item: item[1], reverse=True) if count][:5]
    complaints = [key for key, count in sorted(negative.items(), key=lambda item: item[1], reverse=True) if count][:5]
    if not reviews and not aggregate:
        return {}
    return {
        "average_rating": float(_decimal(aggregate.get("ratingValue"))) if _decimal(aggregate.get("ratingValue")) is not None else None,
        "review_count": int(aggregate["reviewCount"]) if str(aggregate.get("reviewCount", "")).isdigit() else len(reviews) or None,
        "review_sample_count": len(reviews),
        "rating_distribution": distribution,
        "frequently_praised_topics": praised,
        "frequent_complaint_topics": complaints,
        "longevity_mentions": {"positive": positive["longevity"], "negative": negative["longevity"]},
        "sillage_mentions": {"positive": positive["sillage"], "negative": negative["sillage"]},
        "packaging_mentions": {"positive": positive["packaging"], "negative": negative["packaging"]},
        "source_urls": [source_url],
        "summary_method": "deterministic topic aggregation over visible review samples; no customer review text retained",
    }


class GenericJsonLdAdapter(ProductAdapter):
    name = "generic_jsonld"
    version = "1.0.0"

    def parse(self, html: str, url: str, *, country=None, locale=None):
        soup = BeautifulSoup(html, "html.parser")
        node = _product_node(soup)
        extraction_method = "json_ld"
        if not node:
            # Some official brand pages expose product identity only through
            # Open Graph metadata while rendering the catalogue client-side.
            # The runner uses this fallback only for an explicitly selected
            # product-research URL, not for blind domain crawling.
            title = soup.select_one('meta[property="og:title"][content]')
            description = soup.select_one('meta[property="og:description"][content], meta[name="description"][content]')
            image = soup.select_one('meta[property="og:image"][content], meta[name="twitter:image"][content]')
            brand = soup.select_one('meta[property="product:brand"][content]')
            if not title or not title.get("content"):
                return None
            node = {
                "@type": "Product",
                "name": title.get("content"),
                "description": description.get("content") if description else None,
                "image": image.get("content") if image else None,
                "brand": brand.get("content") if brand else None,
            }
            extraction_method = "open_graph"
        offers = node.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        aggregate = node.get("aggregateRating") or {}
        images = node.get("image") or []
        if isinstance(images, (str, dict)):
            images = [images]
        image_urls = [_text(image) for image in images]
        image_urls = [value for value in image_urls if value]
        canonical = soup.select_one('link[rel="canonical"][href]')
        canonical_url = normalize_url(canonical["href"] if canonical else url, url)
        brand = _text(node.get("brand"))
        name = _text(node.get("name"))
        sku = _text(node.get("sku"))
        properties = {}
        for prop in node.get("additionalProperty", []) or []:
            if isinstance(prop, dict) and prop.get("name"):
                properties[str(prop["name"]).strip().lower()] = prop.get("value")
        ingredient_text = _text(node.get("ingredients")) or _text(properties.get("ingredients"))
        claim_value = properties.get("claims") or properties.get("claim")
        benefit_value = properties.get("benefits") or properties.get("benefit")
        claims = [item.strip() for item in re.split(r"[;|]", _text(claim_value) or "") if item.strip()]
        benefits = [item.strip() for item in re.split(r"[;|]", _text(benefit_value) or "") if item.strip()]
        product = ScrapedProduct(
            source_name=urlsplit(url).hostname or "",
            source_domain=urlsplit(url).hostname or "",
            source_url=url,
            canonical_url=canonical_url,
            scraped_at=datetime.now(timezone.utc),
            locale=locale,
            country=country,
            retailer_product_id=sku or _text(node.get("productID")),
            brand=brand,
            product_name=name,
            description=_text(node.get("description")),
            gtin=_text(node.get("gtin")) or _text(node.get("gtin13")) or _text(node.get("gtin12")),
            sku=sku,
            mpn=_text(node.get("mpn")),
            category_path=[part.strip() for part in (_text(node.get("category")) or "").split(">") if part.strip()],
            product_type=_text(properties.get("product type")),
            variant_name=_text(node.get("model")) or _text(properties.get("variant")),
            size=_text(node.get("size")) or _text(properties.get("size")),
            shade=_text(node.get("color")) or _text(properties.get("shade")),
            price=_decimal(offers.get("price") or offers.get("lowPrice")),
            currency=_text(offers.get("priceCurrency")),
            availability=(_text(offers.get("availability")) or "").rsplit("/", 1)[-1] or None,
            image_urls=image_urls,
            ingredient_text_raw=ingredient_text,
            claims=claims,
            benefits=benefits,
            usage_instructions=_text(properties.get("how to use")) or _text(properties.get("directions")),
            skin_types=[
                item.strip() for item in re.split(r"[,;|]", _text(properties.get("skin type")) or "")
                if item.strip()
            ],
            hair_types=[
                item.strip() for item in re.split(r"[,;|]", _text(properties.get("hair type")) or "")
                if item.strip()
            ],
            rating=_decimal(aggregate.get("ratingValue")),
            review_count=int(aggregate["reviewCount"]) if str(aggregate.get("reviewCount", "")).isdigit() else None,
            review_summary=_review_summary(node, aggregate, url),
            parser_version=PARSER_VERSION,
        )
        for key in product.model_fields:
            if key in {"fields", "parser_version"}:
                continue
            value = getattr(product, key)
            if value not in (None, "", []):
                product.fields[key] = ExtractedField(
                    value=value, raw_value=node.get(key, value),
                    path=(f"jsonld.Product.{key}" if extraction_method == "json_ld" else f"meta.og:{key}"),
                    method=extraction_method,
                )

        # Prefer substantial source-stated PDP copy over a short JSON-LD/OG
        # teaser. Reject navigation and INCI-looking paragraphs.
        description_candidates = []
        for selector in (
            "[itemprop=description]", "[data-testid*=description]",
            "[class*=description] p", ".pdp_description_content",
            ".c-accordion__content p",
        ):
            for element in soup.select(selector):
                value = element.get_text(" ", strip=True)
                if len(value) < 120 or len(value) > 6000:
                    continue
                ingredient_count = len(split_inci(value.replace("●", ",")))
                if ingredient_count >= 8 or value.count(",") >= 12:
                    continue
                description_candidates.append((len(value), selector, value))
        if description_candidates:
            _, selector, richer_description = max(description_candidates)
            if len(richer_description) > len(product.description or ""):
                product.description = richer_description
                product.fields["description"] = ExtractedField(
                    value=richer_description, raw_value=richer_description,
                    path=selector, method="semantic_html",
                )
        semantic_selectors = {
            "ingredient_text_raw": (
                "[itemprop=ingredients]", "[data-testid*=ingredient]",
                "#ingredients", ".ingredients", "[class*=ingredient]",
                ".pdp_description_content",
            ),
            "usage_instructions": (
                "[itemprop=usageInfo]", "#directions", ".directions",
                "[class*=how-to-use]", "[class*=advice]",
            ),
            "warnings": ("[itemprop=warning]", ".warnings", "[class*=warning]"),
        }
        for field_name, selectors in semantic_selectors.items():
            if getattr(product, field_name):
                continue
            if field_name == "ingredient_text_raw":
                candidates = []
                for selector in selectors:
                    for element in soup.select(selector):
                        value = element.get_text(" ", strip=True)
                        normalized = value.replace("●", ",")
                        count = len(split_inci(normalized))
                        if count >= 4:
                            candidates.append((count, selector, normalized))
                if candidates:
                    _, selector, value = max(candidates)
                    product.ingredient_text_raw = value
                    product.fields[field_name] = ExtractedField(
                        value=value, raw_value=value, path=selector,
                        method="semantic_html",
                    )
                continue
            for selector in selectors:
                element = soup.select_one(selector)
                if not element:
                    continue
                value = element.get_text(" ", strip=True)
                if not value:
                    continue
                normalized = [value] if field_name == "warnings" else value
                setattr(product, field_name, normalized)
                product.fields[field_name] = ExtractedField(
                    value=normalized, raw_value=value, path=selector,
                    method="semantic_html",
                )
                break
        if product.ingredient_text_raw:
            product.ingredients = split_inci(product.ingredient_text_raw)
        return product
