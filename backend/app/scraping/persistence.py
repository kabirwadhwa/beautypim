from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.auth import log_audit_event
from app.models import (
    Brand, CanonicalProduct, Category, CrawlConflict, CrawlJob, FieldValue,
    Formulation, FormulationIngredient, IngredientDefinition, ProductVariant,
    RawPageObservation, ScrapedFieldObservation, ScrapedProductObservation,
    SourceListing, SourcePrice, ValidationIssue,
)
from app.scraping.adapters.base import ProductAdapter
from app.scraping.ingredients import normalize_ingredient, split_inci
from app.scraping.schemas import ScrapedProduct
from app.services.deduplication import evaluate_match, normalize_text
from app.services.product_identity import research_identity_compatible
from app.services.review_evidence import enforce_review_summary_invariants


def stable_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def json_value(value):
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def _brand(db: Session, value: str | None) -> Brand:
    name = (value or "Unknown Brand").strip()
    normalized = normalize_text(name) or "unknown brand"
    record = db.query(Brand).filter(Brand.normalized_name == normalized).first()
    if not record:
        record = Brand(id=uuid.uuid4(), name=name, normalized_name=normalized)
        db.add(record)
        db.flush()
    return record


def _category(db: Session, parts: list[str]) -> Category | None:
    parent = None
    path_parts = []
    for level, name in enumerate(part.strip() for part in parts if part.strip()):
        path_parts.append(name)
        path = " > ".join(path_parts)
        category = db.query(Category).filter(Category.path == path).first()
        if not category:
            category = Category(id=uuid.uuid4(), name=name, parent_id=parent.id if parent else None, level=level, path=path)
            db.add(category)
            db.flush()
        parent = category
    return parent


def _match(db: Session, product: ScrapedProduct):
    if product.retailer_product_id:
        source_match = db.query(ScrapedProductObservation).filter(
            ScrapedProductObservation.source_domain == product.source_domain,
            ScrapedProductObservation.retailer_product_id == product.retailer_product_id,
            ScrapedProductObservation.canonical_product_id.isnot(None),
        ).order_by(ScrapedProductObservation.scraped_at.desc()).first()
        if source_match:
            return (
                "matched", 1.0, source_match.canonical_product_id,
                source_match.product_variant_id,
            )
    status, score, product_id, variant_id = evaluate_match(
        db, product.product_name or "", product.brand or "",
        product.gtin or product.ean or product.upc, product.size,
    )
    if status in {"exact_match", "deterministic_match"}:
        return "matched", score, product_id, variant_id
    if status in {"candidate", "ambiguous"}:
        return "possible_match", score, product_id, variant_id
    return "unmatched", score, None, None


def persist_product(
    db: Session, job: CrawlJob, raw_page: RawPageObservation,
    product: ScrapedProduct, adapter: ProductAdapter, *, create_unmatched_draft: bool = True,
) -> ScrapedProductObservation:
    product.review_summary = enforce_review_summary_invariants(product.review_summary)
    payload = product.model_dump(mode="json", exclude={"fields"})
    safe_fields_only = bool((job.configuration or {}).get("research_safe_fields_only"))
    identity_only = bool((job.configuration or {}).get("research_identity_only"))
    structured_hash = stable_hash(payload)
    identity = {
        "domain": product.source_domain, "retailer_id": product.retailer_product_id,
        "gtin": product.gtin or product.ean or product.upc,
        "brand": normalize_text(product.brand or ""),
        "name": normalize_text(product.product_name or ""),
        "size": normalize_text(product.size or ""),
    }
    identity_hash = stable_hash(identity)
    existing = db.query(ScrapedProductObservation).filter(
        ScrapedProductObservation.crawl_job_id == job.id,
        ScrapedProductObservation.canonical_url == product.canonical_url,
        ScrapedProductObservation.structured_hash == structured_hash,
    ).first()
    if existing:
        return existing

    match_status, score, canonical_id, variant_id = _match(db, product)
    # Research launched from a product detail page belongs to that product.
    # This avoids creating a duplicate merely because a retailer uses a title
    # or size variation that does not satisfy the generic matcher.
    research_product_id = (job.configuration or {}).get("research_product_id")
    if research_product_id:
        target = db.query(CanonicalProduct).filter(
            CanonicalProduct.id == research_product_id,
            CanonicalProduct.is_deleted == False,
        ).first()
        if target:
            observed_gtin = re.sub(r"\D", "", str(product.gtin or product.ean or product.upc or ""))
            target_gtins = {
                re.sub(r"\D", "", str(value or "")) for (value,) in db.query(ProductVariant.gtin).filter(
                    ProductVariant.canonical_product_id == target.id,
                    ProductVariant.is_deleted == False,
                    ProductVariant.gtin.isnot(None),
                ).all()
            }
            target_gtins.discard("")
            if target_gtins and observed_gtin and observed_gtin not in target_gtins:
                raise ValueError(
                    "Discovered page has a different GTIN and was not attached to the requested product "
                    f"({observed_gtin} not in {sorted(target_gtins)})."
                )
            expected_format = (job.configuration or {}).get("research_expected_format") or ""
            observed_format = " ".join(filter(None, (
                product.product_name, product.product_type, product.variant_name,
                " ".join(product.category_path or []), product.subtitle,
            )))
            if not research_identity_compatible(
                expected_format, observed_format,
                (job.configuration or {}).get("research_product_name") or target.product_name,
            ):
                raise ValueError(
                    "Discovered page is a conflicting product edition and was not attached "
                    f"to the requested product ({expected_format!r} vs {observed_format!r})."
                )
            canonical_id = target.id
            configured_variant_id = (job.configuration or {}).get("research_variant_id")
            target_variant = db.query(ProductVariant).filter(
                ProductVariant.id == configured_variant_id,
                ProductVariant.canonical_product_id == target.id,
                ProductVariant.is_deleted == False,
            ).first() if configured_variant_id else None
            if not target_variant:
                from app.services.product_identity import preferred_product_variant
                target_variant = preferred_product_variant(db, target.id)
            variant_id = target_variant.id if target_variant else None
            match_status, score = "matched", 1.0
    suggested_product_id = canonical_id if match_status == "possible_match" else None
    if match_status in {"unmatched", "possible_match"} and create_unmatched_draft:
        brand = _brand(db, product.brand)
        category = _category(db, product.category_path)
        canonical = CanonicalProduct(
            id=uuid.uuid4(), brand_id=brand.id,
            product_name=product.product_name or "Unnamed scraped product",
            normalized_name=normalize_text(product.product_name or "Unnamed scraped product"),
            category_id=category.id if category else None,
            image_url=product.image_urls[0] if product.image_urls else None,
            review_status="needs_review",
        )
        db.add(canonical)
        db.flush()
        variant = ProductVariant(
            id=uuid.uuid4(), canonical_product_id=canonical.id,
            variant_name=product.variant_name or product.shade or product.size,
            gtin=product.gtin or product.ean or product.upc,
            size=product.size, unit=product.unit,
        )
        db.add(variant)
        db.flush()
        canonical_id, variant_id = canonical.id, variant.id
        log_audit_event(
            db, "CanonicalProduct", canonical.id, canonical.product_name, "create",
            after={"origin": "knowledge_crawl", "source_url": product.source_url},
            changed={"created_as_draft": True}, actor_type="system",
        )

    listing = SourceListing(
        id=uuid.uuid4(), import_job_id=None, crawl_job_id=job.id,
        canonical_product_id=canonical_id, product_variant_id=variant_id,
        raw_data=payload, source_hash=structured_hash, source_url=product.source_url,
        retailer=product.source_name,
    )
    db.add(listing)
    db.flush()
    prior = None
    if canonical_id and not identity_only:
        prior = db.query(ScrapedProductObservation).filter(
            ScrapedProductObservation.canonical_product_id == canonical_id,
            ScrapedProductObservation.source_domain == product.source_domain,
        ).order_by(ScrapedProductObservation.scraped_at.desc()).first()
    changed_fields = {}
    if prior:
        prior_payload = prior.normalized_payload or {}
        changed_fields = {
            key: {"before": prior_payload.get(key), "after": value}
            for key, value in payload.items()
            if prior_payload.get(key) != value
        }
    observation = ScrapedProductObservation(
        id=uuid.uuid4(), crawl_job_id=job.id, raw_page_id=raw_page.id,
        source_listing_id=listing.id, canonical_product_id=canonical_id,
        possible_match_product_id=suggested_product_id,
        product_variant_id=variant_id, source_name=product.source_name,
        source_domain=product.source_domain, source_url=product.source_url,
        canonical_url=product.canonical_url, locale=product.locale,
        country=product.country, retailer_product_id=product.retailer_product_id,
        normalized_payload=payload, identity_hash=identity_hash,
        structured_hash=structured_hash, match_status=match_status,
        changed_fields=changed_fields or None,
        adapter_name=adapter.name, adapter_version=adapter.version,
        parser_version=product.parser_version,
    )
    db.add(observation)
    db.flush()
    if suggested_product_id:
        db.add(ValidationIssue(
            id=uuid.uuid4(), canonical_product_id=canonical_id,
            severity="warning", issue_type="possible_crawl_product_match",
            message=f"Possible existing product match requires review: {suggested_product_id}",
            created_by_type="system",
        ))

    for field_name, field in product.fields.items():
        db.add(ScrapedFieldObservation(
            id=uuid.uuid4(), scraped_product_id=observation.id,
            field_name=field_name, raw_value=json_value(field.raw_value),
            normalized_value=json_value(field.value), extraction_path=field.path,
            extraction_method=field.method, source_domain=product.source_domain,
            source_url=product.source_url, adapter_version=adapter.version,
            parser_version=product.parser_version,
        ))

    if canonical_id:
        variant = db.query(ProductVariant).filter(ProductVariant.id == variant_id).first() if variant_id else None
        if not variant:
            variant = ProductVariant(id=uuid.uuid4(), canonical_product_id=canonical_id)
            db.add(variant)
            db.flush()
            variant_id = variant.id
            listing.product_variant_id = variant.id
            observation.product_variant_id = variant.id
        # Research observations may resolve identity details that were absent
        # from the import. Fill blanks only; never silently replace an accepted
        # variant identity with a retailer's conflicting representation.
        observed_gtin = product.gtin or product.ean or product.upc
        if observed_gtin and not variant.gtin and not safe_fields_only:
            duplicate = db.query(ProductVariant).filter(
                ProductVariant.gtin == observed_gtin, ProductVariant.id != variant.id,
            ).first()
            if not duplicate:
                variant.gtin = observed_gtin
        if not safe_fields_only and not variant.variant_name and (product.variant_name or product.shade or product.size):
            variant.variant_name = product.variant_name or product.shade or product.size
        if not safe_fields_only and not variant.size and product.size:
            variant.size = product.size
        if not safe_fields_only and not variant.unit and product.unit:
            variant.unit = product.unit
        _persist_values_and_conflicts(db, job, observation, product, match_status)
        if not safe_fields_only:
            _persist_formulation(db, listing, canonical_id, variant_id, product)
        canonical = db.query(CanonicalProduct).filter(CanonicalProduct.id == canonical_id).first()
        if canonical and not canonical.image_url and product.image_urls:
            from app.services.image_urls import normalize_public_image_url
            canonical.image_url = normalize_public_image_url(product.image_urls[0])
    if product.price is not None and variant_id and not safe_fields_only and not identity_only:
        db.add(SourcePrice(
            id=uuid.uuid4(), source_listing_id=listing.id,
            product_variant_id=variant_id, amount=product.price,
            original_amount=product.price, promotional_amount=product.promotional_price,
            currency=product.currency or "EUR", retailer=product.source_name,
            country=product.country,
        ))
    return observation


def _persist_values_and_conflicts(db, job, observation, product, match_status):
    product_id = observation.canonical_product_id
    fields = ("description", "product_type", "subtitle", "claims", "benefits",
              "usage_instructions", "warnings", "skin_types", "hair_types",
              "concerns", "availability", "image_urls", "shade", "rating", "review_count",
              "review_summary")
    if (job.configuration or {}).get("research_safe_fields_only"):
        fields = ("image_urls", "rating", "review_count", "review_summary")
    if (job.configuration or {}).get("research_identity_only"):
        fields = ()
    # Ratings, review counts and review texts are independent time/source
    # observations.  A different exact retailer aggregate is not a product
    # identity conflict and must not downgrade the whole observation (which
    # would also discard its accepted review texts from canonical synthesis).
    market_observation_fields = {
        "availability", "image_urls", "rating", "review_count", "review_summary",
    }
    for field in fields:
        value = getattr(product, field)
        if value in (None, "", []):
            continue
        if field == "rating":
            value = float(value)
        if field in market_observation_fields:
            # The normalized payload on ScrapedProductObservation is the
            # append-only evidence store used by the canonical selector.
            continue
        current = db.query(FieldValue).filter(
            FieldValue.canonical_product_id == product_id,
            FieldValue.field_name == field, FieldValue.is_current.is_(True),
        ).first()
        if current and current.value != value:
            conflict = CrawlConflict(
                id=uuid.uuid4(), crawl_job_id=job.id,
                scraped_product_id=observation.id, canonical_product_id=product_id,
                field_name=field, current_value=current.value,
                observed_value=value, status="pending",
            )
            db.add(conflict)
            observation.match_status = "conflict"
            db.add(ValidationIssue(
                id=uuid.uuid4(), canonical_product_id=product_id, field_name=field,
                severity="warning", issue_type="scraped_value_conflict",
                message=f"New crawl observation conflicts with the accepted {field}.",
                created_by_type="system",
            ))
        elif not current:
            db.add(FieldValue(
                id=uuid.uuid4(), canonical_product_id=product_id,
                field_name=field, value=value, source_type="source_data",
                source_reference=product.source_url, confidence_score=1,
                review_status="inferred",
                is_current=True, evidence=[{
                    "source_reference": product.source_url,
                    "source_field": field,
                    "supporting_text": str(value)[:1000],
                    "evidence_type": "scraped_product_observation",
                    "scraped_product_observation_id": str(observation.id),
                    "source_domain": product.source_domain,
                }],
            ))


def _persist_formulation(db, listing, product_id, variant_id, product):
    raw = (product.ingredient_text_raw or "").strip()
    if not raw:
        return
    content_hash = hashlib.sha256(raw.encode()).hexdigest()
    existing = db.query(Formulation).filter(
        Formulation.canonical_product_id == product_id,
        Formulation.content_hash == content_hash,
    ).first()
    if existing:
        return
    formulation = Formulation(
        id=uuid.uuid4(), canonical_product_id=product_id,
        product_variant_id=variant_id, source_listing_id=listing.id,
        raw_inci_text=raw, market=product.country,
        language=product.locale, source_reference=product.source_url,
        content_hash=content_hash,
    )
    db.add(formulation)
    db.flush()
    for position, raw_name in enumerate(split_inci(raw), 1):
        normalized = normalize_ingredient(raw_name)
        definition = db.query(IngredientDefinition).filter(
            IngredientDefinition.normalized_name == normalized,
        ).first()
        db.add(FormulationIngredient(
            id=uuid.uuid4(), formulation_id=formulation.id,
            ingredient_definition_id=definition.id if definition else None,
            raw_inci_name=raw_name[:255], position=position,
            evidence_source=product.source_url, confidence_score=1,
            evidence={"method": "exact_raw_inci_order"},
        ))
