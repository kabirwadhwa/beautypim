"""Source-backed catalogue context for product enrichment.

Exact observations remain evidence.  The anonymised retail corpus is also
used as a retrieval knowledge base: comparable products teach the model the
industry's taxonomy and merchandising patterns without becoming assertions
about the target product.
"""
from __future__ import annotations

from typing import Any, Optional
import uuid

from sqlalchemy.orm import Session

from app.models import (
    CanonicalProduct,
    Category,
    FieldValue,
    Formulation,
    ScrapedProductObservation,
)
from app.services.deduplication import normalize_text
from app.knowledge_corpus.retrieval import retrieve_corpus_evidence


MAX_OBSERVATIONS = 3
MAX_RETAIL_EXAMPLES = 5
MAX_FIELD_VALUES = 80
MAX_TEXT_LENGTH = 12_000
RETAIL_KNOWLEDGE_FIELDS = (
    "brand", "product_name", "subtitle", "description", "category_path",
    "product_type", "variant_name", "size", "unit", "shade",
    "ingredient_text_raw", "ingredients", "claims", "benefits",
    "usage_instructions", "warnings", "skin_types", "hair_types", "concerns",
)

STOP_WORDS = {
    "a", "an", "and", "for", "from", "in", "of", "on", "the", "to", "with",
    "beauty", "product", "new", "care", "ml", "g", "oz",
}

CONCEPT_ALIASES = {
    "cleanser": {"cleanser", "cleansing", "face wash", "nettoyant", "demaquillant", "démaquillant"},
    "moisturizer": {"moisturizer", "moisturiser", "facial cream", "face cream", "day cream", "night cream", "hydratant visage"},
    "serum": {"serum", "sérum"},
    "lip_oil": {"lip oil", "huile a levres", "huile à lèvres"},
    "body_oil": {"body oil", "huile corps", "huile pour le corps"},
    "body_moisturizer": {"body cream", "body lotion", "body balm", "hydratant corps", "lait corps"},
    "sunscreen": {"sunscreen", "sun cream", "spf", "solaire", "protection solaire"},
    "fragrance": {"fragrance", "perfume", "parfum", "eau de parfum", "eau de toilette"},
    "shampoo": {"shampoo", "shampoing"},
    "conditioner": {"conditioner", "apres shampoing", "après-shampoing"},
    "mask": {"mask", "masque"},
    "balm": {"balm", "baume"},
    "toner": {"toner", "tonique", "lotion tonique"},
    "foundation": {"foundation", "fond de teint"},
    "mascara": {"mascara"},
}

DOMAIN_ALIASES = {
    "skin": {"skincare", "skin care", "face", "facial", "visage", "soin visage", "anti age"},
    "hair": {"hair", "scalp", "capillaire", "cheveux", "cuir chevelu"},
    "body": {"body", "corps", "hand", "hands", "main", "mains"},
    "lips": {"lip", "lips", "levre", "levres", "lèvre", "lèvres"},
    "makeup": {"makeup", "make up", "maquillage", "teint", "mascara"},
}


def _tokens(value: Any) -> set[str]:
    """Return useful normalized retrieval terms, including joined compounds."""
    normalized = normalize_text(str(value or ""))
    parts = {
        part for part in normalized.split()
        if len(part) > 2 and part not in STOP_WORDS
    }
    if normalized:
        parts.add(normalized)
    return parts


def _concepts(value: Any) -> set[str]:
    normalized = normalize_text(str(value or ""))
    return {
        concept
        for concept, aliases in CONCEPT_ALIASES.items()
        if any(normalize_text(alias) in normalized for alias in aliases)
    }


def _domains(value: Any) -> set[str]:
    normalized = normalize_text(str(value or ""))
    return {
        domain
        for domain, aliases in DOMAIN_ALIASES.items()
        if any(normalize_text(alias) in normalized for alias in aliases)
    }


def _payload_tokens(payload: dict[str, Any]) -> dict[str, set[str]]:
    categories = payload.get("category_path") or []
    return {
        "name": _tokens(payload.get("product_name")),
        "brand": _tokens(payload.get("brand")),
        "taxonomy": _tokens(" ".join(str(item) for item in categories))
        | _tokens(payload.get("product_type")),
        "content": _tokens(payload.get("description"))
        | _tokens(" ".join(str(item) for item in (payload.get("benefits") or [])))
        | _tokens(" ".join(str(item) for item in (payload.get("claims") or []))),
    }


def _payload_concepts(payload: dict[str, Any]) -> set[str]:
    taxonomy = " ".join(str(item) for item in (payload.get("category_path") or []))
    taxonomy = f"{taxonomy} {payload.get('product_type') or ''}"
    # Retail taxonomy is the strongest signal. Only fall back to the title and
    # description when the source taxonomy has no recognized beauty concept.
    concepts = _concepts(taxonomy)
    if concepts:
        return concepts
    return _concepts(payload.get("product_name"))


def _payload_domains(payload: dict[str, Any]) -> set[str]:
    taxonomy = " ".join(str(item) for item in (payload.get("category_path") or []))
    return _domains(
        f"{taxonomy} {payload.get('product_type') or ''} {payload.get('product_name') or ''}"
    )


def _retail_similarity(
    payload: dict[str, Any], *, name: str, brand: str, category: str,
    product_family: str, description: str,
) -> tuple[float, list[str]]:
    """Rank comparable retail products with deterministic lexical signals."""
    row = _payload_tokens(payload)
    name_terms = _tokens(name)
    brand_terms = _tokens(brand)
    taxonomy_terms = _tokens(category) | _tokens(product_family)
    description_terms = _tokens(description)
    # Product-type concepts come from identity/taxonomy fields, never marketing
    # prose: e.g. "fragrance-free serum" must not become a fragrance product.
    target_concepts = _concepts(f"{name} {category} {product_family}")
    target_domains = _domains(f"{name} {category} {product_family}")
    row_domains = _payload_domains(payload)
    domain_overlap = target_domains & row_domains
    concept_overlap = target_concepts & _payload_concepts(payload)
    score = 0.0
    reasons: list[str] = []

    name_overlap = name_terms & (row["name"] | row["taxonomy"])
    taxonomy_overlap = taxonomy_terms & row["taxonomy"]
    description_overlap = description_terms & (row["name"] | row["taxonomy"] | row["content"])
    brand_overlap = brand_terms & row["brand"]
    if concept_overlap:
        score += min(24.0, len(concept_overlap) * 12.0)
        reasons.append("beauty product concepts: " + ", ".join(sorted(concept_overlap)))
    if domain_overlap:
        score += min(12.0, len(domain_overlap) * 8.0)
        reasons.append("beauty domain: " + ", ".join(sorted(domain_overlap)))
    elif target_domains and row_domains:
        score -= 10.0
    if name_overlap:
        score += min(12.0, len(name_overlap) * 4.0)
        reasons.append("name/product-type terms: " + ", ".join(sorted(name_overlap)[:5]))
    if taxonomy_overlap:
        score += min(10.0, len(taxonomy_overlap) * 5.0)
        reasons.append("taxonomy terms: " + ", ".join(sorted(taxonomy_overlap)[:5]))
    if brand_overlap:
        score += 3.0
        reasons.append("brand-family signal")
    if description_overlap:
        score += min(6.0, len(description_overlap) * 1.5)
        reasons.append("description terms: " + ", ".join(sorted(description_overlap)[:5]))
    return score, reasons


def _trim(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_TEXT_LENGTH:
        return value[:MAX_TEXT_LENGTH] + "…"
    if isinstance(value, list):
        return [_trim(item) for item in value[:100]]
    if isinstance(value, dict):
        return {str(key): _trim(item) for key, item in list(value.items())[:100]}
    return value


def _retail_knowledge_payload(payload: dict[str, Any], *, comparable: bool = False) -> dict[str, Any]:
    """Project a retail observation into a compact enrichment knowledge packet."""
    selected = {
        field: payload[field]
        for field in RETAIL_KNOWLEDGE_FIELDS
        if payload.get(field) not in (None, "", [], {})
    }
    if comparable:
        for field in ("brand", "variant_name", "size", "unit", "shade", "ingredient_text_raw", "ingredients", "claims"):
            selected.pop(field, None)
    # INCI and descriptions are useful but can dominate the model context.
    for field, limit in (("description", 1_200), ("ingredient_text_raw", 1_500)):
        value = selected.get(field)
        if isinstance(value, str) and len(value) > limit:
            selected[field] = value[:limit] + "…"
    preferred_keys = (
        "ingredient_name", "normalized_inci_name", "name", "label",
        "statement", "text", "value", "benefit", "concern",
    )
    for field, value in list(selected.items()):
        if isinstance(value, list):
            compact_items = []
            for item in value[:12]:
                if not isinstance(item, dict):
                    compact_items.append(item)
                    continue
                compact = {
                    key: item[key]
                    for key in preferred_keys
                    if item.get(key) not in (None, "", [], {})
                }
                if compact:
                    compact_items.append(compact)
            selected[field] = compact_items
    return _trim(selected)


def build_catalogue_knowledge_context(
    db: Session,
    canonical_product_id: Optional[uuid.UUID],
    *,
    product_name: str = "",
    brand: str = "",
    gtin: str = "",
    category: str = "",
    product_family: str = "",
    description: str = "",
) -> dict[str, Any]:
    """Return exact evidence plus ranked, attributable retail knowledge."""
    observations = (
        db.query(ScrapedProductObservation)
        .filter(
            ScrapedProductObservation.canonical_product_id == canonical_product_id
        )
        .order_by(ScrapedProductObservation.scraped_at.desc())
        .limit(MAX_OBSERVATIONS)
        .all()
    )
    field_values = (
        db.query(FieldValue)
        .filter(
            FieldValue.canonical_product_id == canonical_product_id,
            FieldValue.is_current == True,
            FieldValue.source_type.in_(["source_data", "human_edit"]),
        )
        .order_by(FieldValue.updated_at.desc())
        .limit(MAX_FIELD_VALUES)
        .all()
    )
    from app.models import ProductVariant
    from app.services.formulation_resolution import resolve_selected_formulation
    context_variant = db.query(ProductVariant).filter(
        ProductVariant.canonical_product_id == canonical_product_id,
        ProductVariant.gtin == gtin,
        ProductVariant.is_deleted == False,
    ).first() if gtin else None
    selected_formulation = resolve_selected_formulation(
        db, canonical_product_id, context_variant.id if context_variant else None,
    ) if canonical_product_id else None
    formulations = [selected_formulation] if selected_formulation else []

    retrieval_fields = (
        db.query(FieldValue)
        .filter(
            FieldValue.canonical_product_id == canonical_product_id,
            FieldValue.is_current == True,
            FieldValue.field_name.in_(["subcategory", "product_type"]),
        )
        .all()
    )
    current_field_map = {item.field_name: item.value for item in retrieval_fields}
    canonical_category = (
        db.query(Category.path)
        .join(CanonicalProduct, CanonicalProduct.category_id == Category.id)
        .filter(CanonicalProduct.id == canonical_product_id)
        .scalar()
    )
    retrieval_category = category or str(canonical_category or "")
    retrieval_family = (
        product_family
        or str(current_field_map.get("subcategory") or "")
        or str(current_field_map.get("product_type") or "")
    )

    # Legacy 4k corpus compatibility. Keep the candidate pool bounded while the
    # dedicated indexed corpus below becomes the primary retrieval layer.
    legacy_query = (
        db.query(ScrapedProductObservation)
        .filter(ScrapedProductObservation.source_domain == "retail-data.invalid")
        .order_by(ScrapedProductObservation.scraped_at.desc())
    )
    normalized_name = normalize_text(product_name)
    normalized_brand = normalize_text(brand)
    normalized_gtin = "".join(character for character in str(gtin or "") if character.isdigit())
    exact_retail = []
    comparable_retail = []
    # PostgreSQL JSON indexes are not assumed for legacy rows, so this bounded
    # fallback cannot grow into an unbounded in-memory scan.
    retail_rows = legacy_query.limit(500).all()
    for row in retail_rows:
        payload = row.normalized_payload or {}
        row_gtin = "".join(character for character in str(payload.get("gtin") or payload.get("ean") or payload.get("upc") or "") if character.isdigit())
        same_gtin = bool(normalized_gtin and row_gtin == normalized_gtin)
        if same_gtin:
            exact_retail.append(row)
            continue
        similarity, reasons = _retail_similarity(
            payload,
            name=product_name,
            brand=brand,
            category=retrieval_category,
            product_family=retrieval_family,
            description=description,
        )
        if similarity >= 4.0:
            comparable_retail.append((similarity, reasons, row))
    exact_retail = exact_retail[:MAX_OBSERVATIONS]
    comparable_retail.sort(key=lambda item: (item[0], item[2].scraped_at), reverse=True)
    deduplicated_retail = []
    seen_retail_identities = set()
    for candidate in comparable_retail:
        payload = candidate[2].normalized_payload or {}
        identity = (
            normalize_text(str(payload.get("brand") or "")),
            normalize_text(str(payload.get("product_name") or "")),
            normalize_text(str(payload.get("product_type") or "")),
        )
        if identity in seen_retail_identities:
            continue
        seen_retail_identities.add(identity)
        deduplicated_retail.append(candidate)
        if len(deduplicated_retail) == MAX_RETAIL_EXAMPLES:
            break
    comparable_retail = deduplicated_retail

    corpus = retrieve_corpus_evidence(
        db, gtin=gtin, brand=brand, product_name=product_name,
        category=retrieval_category,
    )

    if not observations and not field_values and not formulations and not exact_retail and not comparable_retail and corpus.get("match_level") == "unmatched":
        return {}

    return {
        "usage_policy": (
            "Exact-product matches are product evidence. Comparable retail examples are industry "
            "knowledge for reasonable inference: use their taxonomy, product positioning, "
            "benefit patterns, concern patterns, texture, usage and audience vocabulary. "
            "Never present a comparable product's ingredients, claims, certifications, "
            "price or compliance status as a verified fact about the target product."
        ),
        "internal_corpus": {
            "policy": (
                "Exact-product corpus evidence may directly support the target field. Family evidence may only support "
                "family-safe fields. Comparable examples may inform taxonomy and commercial language only; never copy "
                "ingredients, claims, identifiers, prices, availability, testing, free-from or regulatory facts. "
                "A match_level of conflict is not direct evidence: preserve the alternatives and do not select one "
                "without corroboration or human review."
            ),
            **corpus,
        },
        "observations": [
            {
                "source_name": item.source_name,
                "source_domain": item.source_domain,
                "source_url": item.source_url,
                "observed_at": item.scraped_at.isoformat() if item.scraped_at else None,
                "match_status": item.match_status,
                "data": _trim(item.normalized_payload),
            }
            for item in observations
        ],
        "retail_reference_matches": [
            {
                "match_basis": "exact barcode",
                "source_name": "Retail Data",
                "source_url": item.source_url,
                "observed_at": item.scraped_at.isoformat() if item.scraped_at else None,
                "data": _retail_knowledge_payload(item.normalized_payload or {}),
            }
            for item in exact_retail
        ],
        "retail_knowledge_examples": [
            {
                "knowledge_role": "Comparable industry example; supports inference, not direct claims.",
                "similarity_score": score,
                "similarity_basis": reasons,
                "data": _retail_knowledge_payload(item.normalized_payload or {}, comparable=True),
            }
            for score, reasons, item in comparable_retail
        ],
        "accepted_or_current_fields": [
            {
                "field_name": item.field_name,
                "value": _trim(item.value),
                "review_status": item.review_status,
                "source_type": item.source_type,
                "source_reference": item.source_reference,
            }
            for item in field_values
        ],
        "formulations": [
            {
                "raw_inci_text": _trim(item.raw_inci_text),
                "market": item.market,
                "language": item.language,
                "source_reference": item.source_reference,
                "observed_at": item.created_at.isoformat() if item.created_at else None,
            }
            for item in formulations
        ],
    }
