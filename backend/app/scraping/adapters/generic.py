import json
import re
from difflib import SequenceMatcher
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


_INGREDIENT_KEYS = {
    "ingredient", "ingredients", "ingredientlist", "ingredient_list",
    "ingredienttext", "ingredient_text", "inci", "incilist", "inci_list",
    "composition", "fullingredients", "full_ingredients",
}


def _ingredient_candidate(value):
    """Return a credible INCI string from embedded public product state.

    Retailers often render the ingredient accordion client-side while keeping
    the exact value in Next/Nuxt/application JSON.  JSON-LD therefore has no
    ingredients even though the public page visibly does.  This deliberately
    accepts only list-like values with at least four parsed ingredients so a
    marketing paragraph cannot become a formulation.
    """
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            value = ", ".join(item.strip() for item in value if item.strip())
        else:
            return None
    if not isinstance(value, str):
        return None
    normalized = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    normalized = re.sub(r"^\s*(ingredients?|inci|composition)\s*:?\s*", "", normalized, flags=re.I)
    normalized = normalized.replace("●", ",").strip()
    return normalized if len(split_inci(normalized)) >= 4 else None


def _embedded_ingredient_text(soup: BeautifulSoup):
    candidates = []

    def walk(value, path="embedded_json"):
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}"
                if re.sub(r"[^a-z_]", "", str(key).lower()) in _INGREDIENT_KEYS:
                    candidate = _ingredient_candidate(child)
                    if candidate:
                        candidates.append((len(split_inci(candidate)), child_path, candidate))
                if isinstance(child, (dict, list)):
                    walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, (dict, list)):
                    walk(child, f"{path}[{index}]")

    for script in soup.select('script[type="application/ld+json"], script[type="application/json"], script#__NEXT_DATA__'):
        try:
            walk(json.loads(script.string or script.get_text() or "{}"))
        except (json.JSONDecodeError, TypeError, RecursionError):
            continue
    return max(candidates, default=None)


_COMMERCIAL_KEYS = {
    "description": {"description", "longdescription", "long_description", "productdescription", "product_description"},
    "benefits": {"benefits", "benefit", "features", "keybenefits", "key_benefits", "sellingpoints", "selling_points"},
    "usage_instructions": {"directions", "howtouse", "how_to_use", "usage", "usageinstructions", "usage_instructions", "application"},
    "warnings": {"warnings", "warning", "cautions", "caution", "precautions"},
}


def _embedded_commercial_fields(soup: BeautifulSoup) -> dict[str, tuple[str, str]]:
    """Extract public product copy from inert application/hydration state."""
    candidates: dict[str, list[tuple[int, str, str]]] = {key: [] for key in _COMMERCIAL_KEYS}

    def clean(value):
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            value = " | ".join(item for item in value if item.strip())
        if not isinstance(value, str):
            return ""
        return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)

    def walk(value, path="embedded_json"):
        if isinstance(value, dict):
            for key, child in value.items():
                normalized_key = re.sub(r"[^a-z_]", "", str(key).lower())
                child_path = f"{path}.{key}"
                for field_name, keys in _COMMERCIAL_KEYS.items():
                    if normalized_key in keys:
                        text = clean(child)
                        if 15 <= len(text) <= 8000:
                            candidates[field_name].append((len(text), child_path, text))
                if isinstance(child, (dict, list)):
                    walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, (dict, list)):
                    walk(child, f"{path}[{index}]")

    for script in soup.select('script[type="application/ld+json"], script[type="application/json"], script#__NEXT_DATA__'):
        try:
            walk(json.loads(script.string or script.get_text() or "{}"))
        except (json.JSONDecodeError, TypeError, RecursionError):
            continue
    return {
        field_name: (path, value)
        for field_name, values in candidates.items()
        for _, path, value in [max(values, default=(0, "", ""))]
        if value
    }


_REVIEW_BODY_KEYS = (
    "reviewBody", "review_body", "reviewText", "review_text", "contentText",
    "content_text", "body", "text", "comment", "comments", "content", "description",
    "message",
)


def _review_text_value(value) -> str:
    if isinstance(value, str):
        return _clean_review_text(value)
    if isinstance(value, dict):
        for key in ("text", "body", "content", "value", "rendered", "html"):
            text = _review_text_value(value.get(key))
            if text:
                return text
    if isinstance(value, list):
        return _clean_review_text(" ".join(str(item) for item in value if isinstance(item, str)))
    return ""


def _normalize_review_record(review: dict) -> dict:
    """Normalize common public review-widget shapes without retaining author PII."""
    body = ""
    for key in _REVIEW_BODY_KEYS:
        body = _review_text_value(review.get(key))
        if body:
            break
    rating = review.get("reviewRating") or review.get("rating") or review.get("score") or review.get("stars")
    if isinstance(rating, dict):
        rating = rating.get("ratingValue") or rating.get("value") or rating.get("score") or rating.get("rating")
    item_reviewed = review.get("itemReviewed") or review.get("product") or review.get("productInfo") or {}
    item_reviewed_name = _text(item_reviewed) if isinstance(item_reviewed, (dict, list, str)) else None
    return {
        "reviewBody": body,
        "reviewRating": {"ratingValue": rating} if rating not in (None, "") else {},
        "headline": _review_text_value(review.get("headline") or review.get("title") or review.get("subject")),
        "datePublished": review.get("datePublished") or review.get("createdAt") or review.get("created_at") or review.get("date"),
        "verifiedPurchase": review.get("verifiedPurchase") if isinstance(review.get("verifiedPurchase"), bool) else None,
        "itemReviewedName": item_reviewed_name or review.get("productName") or review.get("product_name"),
    }


def _embedded_review_evidence(soup: BeautifulSoup) -> tuple[dict, list]:
    """Find public review aggregates/samples in embedded application state."""
    aggregates: list[dict] = []
    review_sets: list[list] = []

    def walk(value, parent_key=""):
        if isinstance(value, dict):
            lower = {str(key).lower(): child for key, child in value.items()}
            aggregate = lower.get("aggregaterating") or lower.get("aggregate_rating")
            if isinstance(aggregate, dict):
                aggregates.append(aggregate)
            reviews = lower.get("reviews") or lower.get("review")
            if isinstance(reviews, list) and any(isinstance(row, dict) for row in reviews):
                review_sets.append([_normalize_review_record(row) for row in reviews if isinstance(row, dict)])
            elif isinstance(reviews, dict):
                review_sets.append([_normalize_review_record(reviews)])
            body_keys = {key.lower() for key in _REVIEW_BODY_KEYS}
            has_review_body = bool(body_keys & set(lower))
            has_review_signal = bool({
                "reviewrating", "review_rating", "rating", "score", "stars",
                "verifiedpurchase", "verified_purchase", "datepublished",
            } & set(lower))
            # Public review endpoints frequently call their arrays `results`,
            # `items` or `data`. Detect record semantics instead of relying on
            # one widget vendor's container name.
            if has_review_body and ("review" in parent_key or has_review_signal):
                review_sets.append([_normalize_review_record(value)])
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    walk(child, str(key).lower())
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, (dict, list)):
                    walk(child, parent_key)

    decoder = json.JSONDecoder()

    def script_payloads(script):
        raw = (script.string or script.get_text() or "").strip()
        if not raw or len(raw) > 3_000_000:
            return
        try:
            yield json.loads(raw)
            return
        except (json.JSONDecodeError, TypeError):
            pass
        # Public application state is frequently assigned to window globals
        # rather than emitted as application/json. Decode JSON values only;
        # never execute page script.
        if not re.search(r"review|rating|__NEXT_DATA__|__INITIAL_STATE__|__PRELOADED_STATE__", raw, re.I):
            return
        starts = [match.start() for match in re.finditer(r"[\[{]", raw)]
        for start in starts[:50]:
            try:
                value, _ = decoder.raw_decode(raw[start:])
            except (json.JSONDecodeError, TypeError, RecursionError):
                continue
            if isinstance(value, (dict, list)):
                yield value
                return

    for script in soup.select("script"):
        for payload in script_payloads(script) or ():
            try:
                walk(payload)
            except RecursionError:
                continue
    def review_total(row):
        value = str(row.get("reviewCount") or row.get("ratingCount") or 0).replace(",", "")
        match = re.search(r"\d+(?:\.\d+)?", value)
        return float(match.group()) if match else 0
    aggregate = max(aggregates, key=review_total, default={})
    # A page can expose complementary reviews through JSON-LD, application
    # state, and a rendered widget.  Keep all candidates here and let the
    # deterministic acceptance layer deduplicate them below.
    reviews = [review for review_set in review_sets for review in review_set][:500]
    # Some public retailer widgets render semantic review cards without
    # serializing them into JSON-LD. Capture only public copy and rating; never
    # retain author/profile/customer identifiers.
    dom_reviews = []
    for card in soup.select(
        '[itemprop="review"], [data-testid*="review-card"], [data-testid*="review-item"], '
        '[data-automation-id*="review"], [data-testid="review"], '
        'article[class*="review"], li[class*="review"], div[class*="review-card"], '
        'div[class*="ReviewCard"], section[class*="review-item"]'
    )[:100]:
        body_node = card.select_one(
            '[itemprop="reviewBody"], [data-testid*="review-content"], [data-testid*="review-text"], '
            '[data-automation-id*="review-text"], [data-automation-id*="review-body"], '
            '.review-content, .review-body, .review-text, [class*="ReviewText"], '
            '[class*="reviewBody"], [class*="review-content"], [class*="review-text"]'
        )
        body = " ".join((body_node.get_text(" ", strip=True) if body_node else "").split())
        rating_node = card.select_one('[itemprop="ratingValue"], [data-rating], [aria-label*="out of 5"]')
        title_node = card.select_one(
            '[itemprop="headline"], [data-testid*="review-title"], '
            '[data-automation-id*="review-title"], .review-title, [class*="ReviewTitle"]'
        )
        date_node = card.select_one(
            '[itemprop="datePublished"], time[datetime], [data-testid*="review-date"], '
            '[data-automation-id*="review-date"], .review-date, [class*="ReviewDate"]'
        )
        raw_rating = None
        if rating_node:
            raw_rating = rating_node.get("content") or rating_node.get("data-rating") or rating_node.get("aria-label") or rating_node.get_text(" ", strip=True)
        match = re.search(r"([0-5](?:\.\d+)?)", str(raw_rating or ""))
        dom_reviews.append({
            "reviewBody": body,
            "reviewRating": {"ratingValue": match.group(1)} if match else {},
            "headline": title_node.get_text(" ", strip=True) if title_node else None,
            "datePublished": (
                date_node.get("datetime") or date_node.get("content") or date_node.get_text(" ", strip=True)
                if date_node else None
            ),
        })
    reviews.extend(dom_reviews)
    unique_reviews = []
    seen_records = set()
    for review in reviews:
        fingerprint = json.dumps(review, sort_keys=True, default=str)
        if fingerprint in seen_records:
            continue
        seen_records.add(fingerprint)
        unique_reviews.append(review)
    return aggregate, unique_reviews


def _clean_review_text(value) -> str:
    """Return useful public review copy without retaining reviewer identity/PII."""
    text = BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())[:4000]


def _review_summary(node: dict, aggregate: dict, source_url: str, locale: str | None = None) -> dict:
    """Retain a bounded, de-identified exact-page sample plus aggregate signals."""
    reviews = node.get("review") or []
    if isinstance(reviews, dict):
        reviews = [reviews]
    reviews = [_normalize_review_record(review) for review in reviews if isinstance(review, dict)]
    topics = {
        "performance": ("effective", "results", "works well", "made a difference", "didn't work", "not effective"),
        "hydration": ("hydrating", "hydration", "moisturizing", "moisturising", "dryness", "dry skin"),
        "sensitivity": ("sensitive", "irritation", "irritating", "redness", "gentle", "reaction"),
        "application": ("application", "applies", "blend", "blends", "easy to use", "difficult to use"),
        "wear": ("wear time", "stays on", "longwear", "long-wear", "creases", "smudges", "fades"),
        "shade": ("shade", "colour", "color", "undertone", "match"),
        "finish": ("finish", "glowy", "matte", "dewy", "radiant", "cakey"),
        "hair feel": ("soft hair", "shine", "frizz", "tangles", "scalp", "weighed down"),
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
    samples = []
    seen_samples = set()
    rejection_counts: dict[str, int] = {}
    expected_name = _clean_review_text(node.get("name")).casefold()
    expected_tokens = {token for token in re.findall(r"[a-z0-9]+", expected_name) if len(token) > 2}

    def near_duplicate(text: str) -> bool:
        normalized = re.sub(r"\W+", " ", text.casefold()).strip()
        tokens = set(normalized.split())
        for sample in samples:
            existing = re.sub(r"\W+", " ", sample["text"].casefold()).strip()
            existing_tokens = set(existing.split())
            # Distinct numbered/quantified reviews can otherwise look almost
            # identical to a fuzzy matcher (for example templated survey text).
            if {token for token in tokens if token.isdigit()} != {
                token for token in existing_tokens if token.isdigit()
            }:
                continue
            union = tokens | existing_tokens
            if normalized == existing:
                return True
            if union and len(tokens & existing_tokens) / len(union) >= 0.94:
                return True
            if SequenceMatcher(None, normalized, existing).ratio() >= 0.96:
                return True
        return False

    for review in reviews[:250]:
        if not isinstance(review, dict):
            continue
        body_text = _clean_review_text(
            review.get("reviewBody") or review.get("description") or review.get("text") or review.get("content")
        )
        body = body_text.lower()
        rating_obj = review.get("reviewRating") or {}
        rating = _decimal(rating_obj.get("ratingValue") if isinstance(rating_obj, dict) else rating_obj)
        if rating is not None:
            bucket = str(max(1, min(5, int(round(float(rating))))))
            distribution[bucket] = distribution.get(bucket, 0) + 1
        sample_key = re.sub(r"\W+", "", body)[:1000]
        rejection_reason = None
        if not body_text or not sample_key:
            rejection_reason = "missing_review_text"
        elif len(body_text) < 20:
            rejection_reason = "review_text_too_short"
        reviewed_name = _clean_review_text(review.get("itemReviewedName")).casefold()
        reviewed_tokens = {token for token in re.findall(r"[a-z0-9]+", reviewed_name) if len(token) > 2}
        if reviewed_name and expected_tokens and not (expected_tokens & reviewed_tokens):
            rejection_reason = "wrong_product_review"
        elif sample_key in seen_samples or near_duplicate(body_text):
            rejection_reason = "duplicate_review_text"
        elif len(samples) >= 25:
            rejection_reason = "review_sample_limit_reached"
        if rejection_reason:
            rejection_counts[rejection_reason] = rejection_counts.get(rejection_reason, 0) + 1
        else:
            seen_samples.add(sample_key)
            samples.append({
                "text": body_text,
                "title": _clean_review_text(review.get("headline") or review.get("title"))[:300] or None,
                "rating": float(rating) if rating is not None else None,
                "date": str(review.get("datePublished") or review.get("date") or "")[:40] or None,
                "source_url": source_url,
                "locale": locale,
                "verified_purchase": review.get("verifiedPurchase") if isinstance(review.get("verifiedPurchase"), bool) else None,
                # Deliberately no author, email, profile URL or other reviewer PII.
            })
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
    aggregate_rating = _decimal(aggregate.get("ratingValue"))
    best_rating = _decimal(aggregate.get("bestRating"))
    normalized_rating = (
        float(aggregate_rating) * 5 / float(best_rating)
        if aggregate_rating is not None and best_rating is not None and float(best_rating) > 0
        else float(aggregate_rating) if aggregate_rating is not None else None
    )
    return {
        "average_rating": round(normalized_rating, 3) if normalized_rating is not None else None,
        "review_count": int(aggregate["reviewCount"]) if str(aggregate.get("reviewCount", "")).isdigit() else len(reviews) or None,
        "review_sample_count": len(samples),
        "raw_review_candidate_count": len(reviews),
        "accepted_review_candidate_count": len(samples),
        "rejected_review_candidate_count": sum(rejection_counts.values()),
        "review_samples": samples,
        "review_sample_rejections": [
            {"reason": reason, "count": count} for reason, count in sorted(rejection_counts.items())
        ],
        "rating_distribution": distribution,
        "frequently_praised_topics": praised,
        "frequent_complaint_topics": complaints,
        "longevity_mentions": {"positive": positive["longevity"], "negative": negative["longevity"]},
        "sillage_mentions": {"positive": positive["sillage"], "negative": negative["sillage"]},
        "packaging_mentions": {"positive": positive["packaging"], "negative": negative["packaging"]},
        "source_urls": [source_url],
        "summary_method": "deterministic topic aggregation; bounded de-identified review text retained when publicly visible",
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
            is_review_page = bool(
                re.search(r"/reviews?(?:/|$)", urlsplit(url).path, re.I)
                or soup.select_one(
                    '[itemprop="review"], [data-testid*="review"], '
                    '[data-automation-id*="review"], article[class*="review"], '
                    'li[class*="review"], div[class*="review-card"]'
                )
            )
            fallback_title = soup.select_one("h1, title") if is_review_page else None
            resolved_title = (
                title.get("content") if title and title.get("content")
                else fallback_title.get_text(" ", strip=True) if fallback_title else None
            )
            if not resolved_title:
                return None
            node = {
                "@type": "Product",
                "name": resolved_title,
                "description": description.get("content") if description else None,
                "image": image.get("content") if image else None,
                "brand": brand.get("content") if brand else None,
            }
            extraction_method = "open_graph" if title and title.get("content") else "review_page_dom"
        offers = node.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        aggregate = node.get("aggregateRating") or {}
        embedded_aggregate, embedded_reviews = _embedded_review_evidence(soup)
        embedded_commercial = _embedded_commercial_fields(soup)
        if not aggregate and embedded_aggregate:
            aggregate = embedded_aggregate
        if not node.get("review") and embedded_reviews:
            node = {**node, "review": embedded_reviews[:250]}
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
        ingredient_from_embedded = False
        embedded_ingredients = _embedded_ingredient_text(soup)
        if not ingredient_text and embedded_ingredients:
            _, _, ingredient_text = embedded_ingredients
            ingredient_from_embedded = True
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
            review_summary=_review_summary(node, aggregate, url, locale),
            parser_version=PARSER_VERSION,
        )
        for field_name in ("description", "usage_instructions"):
            if not getattr(product, field_name) and field_name in embedded_commercial:
                path, value = embedded_commercial[field_name]
                setattr(product, field_name, value)
                product.fields[field_name] = ExtractedField(
                    value=value, raw_value=value, path=path, method="embedded_application_json",
                )
        if not product.benefits and "benefits" in embedded_commercial:
            path, value = embedded_commercial["benefits"]
            product.benefits = [item.strip() for item in re.split(r"[|;•]", value) if item.strip()]
            product.fields["benefits"] = ExtractedField(
                value=product.benefits, raw_value=value, path=path, method="embedded_application_json",
            )
        if not product.warnings and "warnings" in embedded_commercial:
            path, value = embedded_commercial["warnings"]
            product.warnings = [value]
            product.fields["warnings"] = ExtractedField(
                value=product.warnings, raw_value=value, path=path, method="embedded_application_json",
            )
        for key in product.model_fields:
            if key in {"fields", "parser_version"}:
                continue
            if key in product.fields:
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
            "benefits": (
                "[itemprop=featureList]", "#benefits", ".benefits",
                "[data-testid*=benefit]", "[class*=key-benefit]", "[class*=product-benefit]",
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
                normalized = [value] if field_name in {"warnings", "benefits"} else value
                setattr(product, field_name, normalized)
                product.fields[field_name] = ExtractedField(
                    value=normalized, raw_value=value, path=selector,
                    method="semantic_html",
                )
                break
        if product.ingredient_text_raw:
            if ingredient_from_embedded and embedded_ingredients:
                _, path, value = embedded_ingredients
                product.fields["ingredient_text_raw"] = ExtractedField(
                    value=value, raw_value=value, path=path,
                    method="embedded_application_json",
                )
            product.ingredients = split_inci(product.ingredient_text_raw)
        return product
