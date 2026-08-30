"""Deterministic ingestion of uploaded customer source rows.

This module is the single precedence boundary for customer feeds.  It has no
dependency on AI enrichment, crawling, browser rendering, or product research.
Known columns are adapted to BeautyPIM's structural/canonical fields; every
other non-empty column becomes a durable ``source_attr.*`` FieldValue.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable

from sqlalchemy.orm import Session

from app.models import Brand, CanonicalProduct, FieldValue, ImportJob, ImportJobItem, ProductVariant, SourceListing


EMPTY_TEXT = {"", "none", "nan", "null", "not found", "not_found", "not provided"}
SOURCE_ATTRIBUTE_PREFIX = "source_attr."


def _clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return None if value.lower() in EMPTY_TEXT else value
    if isinstance(value, (list, dict)):
        return value or None
    return value


def _normalized_header(value: Any) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def _slug_header(value: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-") or "attribute"
    return slug[:68].rstrip("-") or "attribute"


def dynamic_source_field_key(header: str) -> str:
    """Return a bounded, collision-safe key while retaining the label in evidence."""
    digest = hashlib.sha1(str(header).encode("utf-8")).hexdigest()[:12]
    return f"{SOURCE_ATTRIBUTE_PREFIX}{_slug_header(header)}.{digest}"[:100]


def ignored_source_headers(mapping: Dict[str, str] | None) -> set[str]:
    return {
        str(value) for key, value in (mapping or {}).items()
        if str(key).startswith("__ignore__.") and value
    }


def _source_value(raw_data: Dict[str, Any], mapping: Dict[str, str], field: str, aliases: Iterable[str]) -> tuple[Any, str | None]:
    ignored = ignored_source_headers(mapping)
    mapped_column = (mapping or {}).get(field)
    if mapped_column and mapped_column not in ignored:
        mapped_value = _clean((raw_data or {}).get(mapped_column))
        if mapped_value is not None:
            return mapped_value, mapped_column
    normalized = {_normalized_header(key): (key, value) for key, value in (raw_data or {}).items()}
    for alias in aliases:
        match = normalized.get(_normalized_header(alias))
        if match:
            header, raw_value = match
            if str(header) in ignored:
                continue
            value = _clean(raw_value)
            if value is not None:
                return value, str(header)
    return None, mapped_column


def _benefits(value: Any) -> list[str] | None:
    value = _clean(value)
    if value is None:
        return None
    if isinstance(value, list):
        parts = value
    elif isinstance(value, dict):
        parts = list(value.values())
    else:
        text = str(value).strip()
        if text.startswith("["):
            try:
                decoded = json.loads(text)
                parts = decoded if isinstance(decoded, list) else [text]
            except (TypeError, ValueError):
                parts = re.split(r"[\n;|]+", text)
        else:
            parts = re.split(r"[\n;|]+", text)
    cleaned: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            part = part.get("statement") or part.get("value") or part.get("name")
        item = str(part or "").strip().lstrip("-•").strip()
        if item and item.lower() not in EMPTY_TEXT and item not in cleaned:
            cleaned.append(item)
    return cleaned or None


def _text(value: Any) -> str | None:
    value = _clean(value)
    return str(value).strip() if value is not None else None


# Known business concepts are adapters, not an allow-list: headers that do not
# match these definitions still flow through the generic dynamic path below.
KNOWN_FIELD_SPECS: dict[str, tuple[str, tuple[str, ...], Callable[[Any], Any]]] = {
    "description": ("description", ("Product Description", "Description", "Long Description", "Marketing Description"), _text),
    "benefits": ("benefits", ("Product Benefits", "Benefits", "Key Benefits"), _benefits),
    "product_usp": ("product_usp", ("Product USP", "USP", "Unique Selling Proposition"), _text),
    "article_description": ("article_description", ("Article description", "Article Description", "Item Description"), _text),
    "bgb_subgroup": ("bgb_subgroup", ("BGB Subgroup",), _text),
    "bgb_typegroup": ("bgb_typegroup", ("BGB Typegroup",), _text),
    "customer_review_summary": ("customer_review_summary", ("Product Review Summary", "Customer Review Summary"), _text),
    "claims": ("claims", ("Claims", "Product Claims", "Marketing Claims"), _benefits),
    "directions": ("directions", ("Directions", "Usage Instructions", "How To Use"), _text),
    "ingredients": ("ingredients", (
        "Ingredients", "Ingredient", "INCI", "INCI List", "Raw INCI",
        "Ingredient List", "Ingredients List", "Composition", "Formula", "Formulation",
    ), _text),
    "customer_category": ("category", ("Category", "Customer Category"), _text),
    "customer_subcategory": ("subcategory", ("Customer Subcategory",), _text),
    "product_url": ("product_url", ("Product URL", "Product Page URL"), _text),
    "image_url": ("image_url", ("Image URL", "Product Image URL"), _text),
    "price": ("price", ("Price", "List Price"), _text),
    "market": ("market", ("Market", "Country"), _text),
    "language": ("language", ("Language", "Locale"), _text),
}

STRUCTURAL_FIELDS: dict[str, tuple[str, ...]] = {
    "ean": ("EAN", "EAN code", "GTIN", "UPC", "Barcode"),
    "brand": ("Brand",),
    "product_name": ("Product Name", "Product Title"),
    "size": ("Size",),
    "unit": ("Unit",),
    "variant": ("Variant", "Variant Name"),
    "sku": ("SKU", "SKU Number", "Supplier SKU"),
}


def _recognized_headers(raw_data: Dict[str, Any], mapping: Dict[str, str]) -> set[str]:
    normalized_to_header = {_normalized_header(key): str(key) for key in (raw_data or {})}
    headers: set[str] = set()
    for field, aliases in STRUCTURAL_FIELDS.items():
        mapped = (mapping or {}).get(field)
        if mapped:
            headers.add(mapped)
        for alias in aliases:
            if _normalized_header(alias) in normalized_to_header:
                headers.add(normalized_to_header[_normalized_header(alias)])
    for _, (mapping_field, aliases, _) in KNOWN_FIELD_SPECS.items():
        mapped = (mapping or {}).get(mapping_field)
        if mapped:
            headers.add(mapped)
        for alias in aliases:
            if _normalized_header(alias) in normalized_to_header:
                headers.add(normalized_to_header[_normalized_header(alias)])
    return headers


@dataclass
class SourceMergeResult:
    listings_processed: int = 0
    products_updated: int = 0
    fields_written: int = 0
    fields_unchanged: int = 0
    structural_values_written: int = 0
    dynamic_attributes_written: int = 0
    blank_values_skipped: int = 0
    human_values_protected: int = 0
    unlinked_listings_skipped: int = 0
    updated_product_ids: set[str] = field(default_factory=set)

    def as_dict(self) -> Dict[str, Any]:
        result = vars(self).copy()
        result["updated_product_ids"] = sorted(self.updated_product_ids)
        return result


def _same_value(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, default=str) == json.dumps(right, sort_keys=True, default=str)


def _current_field(
    db: Session, product_id: uuid.UUID, field_name: str,
    variant_id: uuid.UUID | None = None, *, lock: bool = False,
) -> FieldValue | None:
    query = db.query(FieldValue).filter(FieldValue.field_name == field_name, FieldValue.is_current == True)
    query = query.filter(
        FieldValue.product_variant_id == variant_id
    ) if variant_id else query.filter(FieldValue.canonical_product_id == product_id)
    if lock and db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return query.first()


def _source_chronology(db: Session, listing: SourceListing) -> tuple[datetime, int]:
    """Return immutable import chronology, never replay/execution chronology."""
    job = db.query(ImportJob).filter(ImportJob.id == listing.import_job_id).first()
    item = db.query(ImportJobItem).filter(
        ImportJobItem.import_job_id == listing.import_job_id,
        ImportJobItem.source_listing_id == listing.id,
    ).first()
    return (
        (job.created_at if job and job.created_at else listing.created_at) or datetime.min,
        int(item.source_row_number or 0) if item else 0,
    )


def _field_source_chronology(db: Session, current: FieldValue) -> tuple[datetime, int] | None:
    evidence = current.evidence if isinstance(current.evidence, list) else []
    row = next((entry for entry in evidence if isinstance(entry, dict) and entry.get("import_job_id")), None)
    if not row:
        return None
    try:
        job_id = uuid.UUID(str(row["import_job_id"]))
    except (TypeError, ValueError, AttributeError):
        return None
    job = db.query(ImportJob).filter(ImportJob.id == job_id).first()
    if not job:
        return None
    return (job.created_at or datetime.min, int(row.get("source_row_number") or 0))


def _incoming_can_replace(db: Session, current: FieldValue | None, listing: SourceListing) -> bool:
    if not current or current.source_type != "source_data":
        return True
    current_order = _field_source_chronology(db, current)
    # Legacy source values without customer-import provenance are weaker than
    # an explicit customer row. Customer values with provenance are ordered by
    # import creation time and then row order within that import.
    return current_order is None or _source_chronology(db, listing) >= current_order


def _write_source_field(
    db: Session, *, product_id: uuid.UUID, field_name: str, value: Any,
    listing: SourceListing, source_header: str, raw_value: Any,
    result: SourceMergeResult, variant_id: uuid.UUID | None = None,
) -> bool:
    value = _clean(value)
    if value in (None, "", [], {}):
        result.blank_values_skipped += 1
        return False
    current = _current_field(db, product_id, field_name, variant_id, lock=True)
    if current and current.source_type == "human_edit":
        result.human_values_protected += 1
        return False
    if current and not _incoming_can_replace(db, current, listing):
        result.fields_unchanged += 1
        return False
    if current and _same_value(current.value, value):
        result.fields_unchanged += 1
        return False
    if current:
        current.is_current = False
        db.flush()
    source_reference = f"feed:{listing.import_job_id}:listing:{listing.id}"
    db.add(FieldValue(
        id=uuid.uuid4(), canonical_product_id=None if variant_id else product_id,
        product_variant_id=variant_id, field_name=field_name, value=value,
        source_type="source_data", source_reference=source_reference,
        confidence_score=1.0, review_status="confirmed", is_current=True,
        evidence=[{
            "source_reference": source_reference, "source_listing_id": str(listing.id),
            "import_job_id": str(listing.import_job_id), "source_field": source_header,
            "source_header": source_header, "supporting_text": str(raw_value)[:1000],
            "source_row_number": _source_chronology(db, listing)[1],
            "evidence_type": "explicit_customer_source",
        }],
        reasoning_summary="Explicit non-empty value merged from the preserved uploaded customer row.",
        semantic_status="explicit_source", semantic_status_type="source_data",
    ))
    result.fields_written += 1
    return True


def _normalize_identity(value: Any) -> str:
    return "".join(character for character in str(value or "").lower() if character.isalnum())


def _split_size(value: Any, explicit_unit: Any = None) -> tuple[str | None, str | None]:
    text = _text(value)
    unit = _text(explicit_unit)
    if not text:
        return None, unit
    match = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*([a-zA-Zµ]+)?\s*", text)
    if not match:
        return text, unit
    return match.group(1).replace(",", "."), unit or match.group(2)


def _merge_structural_fields(
    db: Session, *, product: CanonicalProduct, variant: ProductVariant | None,
    listing: SourceListing, mapping: Dict[str, str], result: SourceMergeResult,
) -> bool:
    raw = listing.raw_data or {}
    wrote = False
    brand_value, brand_header = _source_value(raw, mapping, "brand", STRUCTURAL_FIELDS["brand"])
    product_name, product_name_header = _source_value(raw, mapping, "product_name", STRUCTURAL_FIELDS["product_name"])
    ean, ean_header = _source_value(raw, mapping, "ean", STRUCTURAL_FIELDS["ean"])
    size, size_header = _source_value(raw, mapping, "size", STRUCTURAL_FIELDS["size"])
    unit, unit_header = _source_value(raw, mapping, "unit", STRUCTURAL_FIELDS["unit"])
    variant_name, variant_header = _source_value(raw, mapping, "variant", STRUCTURAL_FIELDS["variant"])
    sku, sku_header = _source_value(raw, mapping, "sku", STRUCTURAL_FIELDS["sku"])

    if brand_value is not None:
        human = _current_field(db, product.id, "brand", lock=True)
        if human and human.source_type == "human_edit":
            result.human_values_protected += 1
        elif human and not _incoming_can_replace(db, human, listing):
            result.fields_unchanged += 1
        else:
            normalized = _normalize_identity(brand_value)
            brand = db.query(Brand).filter(Brand.normalized_name == normalized).first()
            if not brand:
                brand = Brand(id=uuid.uuid4(), name=str(brand_value).strip(), normalized_name=normalized)
                db.add(brand); db.flush()
            if product.brand_id != brand.id:
                product.brand_id = brand.id; wrote = True; result.structural_values_written += 1
            wrote = _write_source_field(db, product_id=product.id, field_name="brand", value=str(brand_value).strip(),
                                        listing=listing, source_header=brand_header or "Brand", raw_value=brand_value, result=result) or wrote

    if product_name is not None:
        human = _current_field(db, product.id, "product_name", lock=True)
        if human and human.source_type == "human_edit":
            result.human_values_protected += 1
        elif human and not _incoming_can_replace(db, human, listing):
            result.fields_unchanged += 1
        else:
            name = str(product_name).strip()
            if product.product_name != name:
                product.product_name = name; product.normalized_name = _normalize_identity(name)
                wrote = True; result.structural_values_written += 1
            wrote = _write_source_field(db, product_id=product.id, field_name="product_name", value=name,
                                        listing=listing, source_header=product_name_header or "Product Name", raw_value=product_name, result=result) or wrote

    if variant:
        current_gtin = _current_field(db, product.id, "gtin", variant.id)
        if ean is not None and not (current_gtin and current_gtin.source_type == "human_edit"):
            digits = "".join(character for character in str(ean) if character.isdigit())
            if digits and (not variant.gtin or variant.gtin == digits):
                if variant.gtin != digits:
                    variant.gtin = digits; wrote = True; result.structural_values_written += 1
        parsed_size, parsed_unit = _split_size(size, unit)
        for field_name, value, header in (
            ("size", parsed_size, size_header or "Size"), ("unit", parsed_unit, unit_header or size_header or "Unit"),
            ("variant", _text(variant_name), variant_header or "Variant"),
        ):
            if value is None:
                continue
            current = _current_field(db, product.id, field_name, variant.id, lock=True)
            if current and current.source_type == "human_edit":
                result.human_values_protected += 1; continue
            if current and not _incoming_can_replace(db, current, listing):
                result.fields_unchanged += 1; continue
            attr = "variant_name" if field_name == "variant" else field_name
            if getattr(variant, attr) != value:
                setattr(variant, attr, value); wrote = True; result.structural_values_written += 1
            _write_source_field(db, product_id=product.id, variant_id=variant.id, field_name=field_name, value=value,
                                listing=listing, source_header=header, raw_value=size if field_name in {"size", "unit"} else variant_name, result=result)

    if sku is not None:
        current = _current_field(db, product.id, "sku")
        existing = current.value if current and isinstance(current.value, list) else ([current.value] if current and current.value else [])
        value = str(sku).strip()
        combined = existing if value in existing else [*existing, value]
        wrote = _write_source_field(db, product_id=product.id, field_name="sku", value=combined,
                                    listing=listing, source_header=sku_header or "SKU", raw_value=sku, result=result) or wrote
    return wrote


def merge_source_listing(
    db: Session, *, listing: SourceListing, mapping: Dict[str, str],
    canonical_product_id: uuid.UUID, product_variant_id: uuid.UUID | None = None,
    result: SourceMergeResult | None = None,
) -> SourceMergeResult:
    """Apply one preserved customer row using human > source > evidence > AI."""
    result = result or SourceMergeResult()
    result.listings_processed += 1
    product_query = db.query(CanonicalProduct).filter(CanonicalProduct.id == canonical_product_id)
    if db.bind.dialect.name == "postgresql":
        product_query = product_query.with_for_update()
    product = product_query.first()
    if not product:
        result.unlinked_listings_skipped += 1
        return result
    variant = None
    if product_variant_id:
        variant_query = db.query(ProductVariant).filter(ProductVariant.id == product_variant_id)
        if db.bind.dialect.name == "postgresql":
            variant_query = variant_query.with_for_update()
        variant = variant_query.first()
    if not variant:
        ean, _ = _source_value(listing.raw_data or {}, mapping, "ean", STRUCTURAL_FIELDS["ean"])
        digits = "".join(character for character in str(ean or "") if character.isdigit())
        if digits:
            variant = db.query(ProductVariant).filter(ProductVariant.gtin == digits).first()

    wrote_product = _merge_structural_fields(
        db, product=product, variant=variant, listing=listing, mapping=mapping, result=result,
    )
    raw_data = listing.raw_data or {}
    for canonical_field, (mapping_field, aliases, normalizer) in KNOWN_FIELD_SPECS.items():
        raw_value, source_header = _source_value(raw_data, mapping, mapping_field, aliases)
        if raw_value is None:
            continue
        value = normalizer(raw_value)
        wrote_product = _write_source_field(
            db, product_id=product.id, field_name=canonical_field, value=value,
            listing=listing, source_header=source_header or aliases[0], raw_value=raw_value, result=result,
        ) or wrote_product

    # ``ingredients`` FieldValue carries immutable customer chronology and
    # provenance.  The product-facing truth is the live Formulation, so always
    # synchronize from the winning current value (not blindly from this row).
    # This makes historical reprocessing safe and repairs legacy imports that
    # retained INCI only as a FieldValue.
    from app.services.formulation_resolution import synchronize_current_source_formulation
    db.flush()
    formulation_result = synchronize_current_source_formulation(db, product, variant)
    if formulation_result.status == "applied":
        wrote_product = True

    recognized = _recognized_headers(raw_data, mapping)
    ignored = ignored_source_headers(mapping)
    for header, raw_value in raw_data.items():
        value = _clean(raw_value)
        if str(header) in recognized or str(header) in ignored or value is None:
            if value is None:
                result.blank_values_skipped += 1
            continue
        if _write_source_field(
            db, product_id=product.id, field_name=dynamic_source_field_key(str(header)), value=value,
            listing=listing, source_header=str(header), raw_value=raw_value, result=result,
        ):
            result.dynamic_attributes_written += 1
            wrote_product = True

    listing.canonical_product_id = product.id
    if variant:
        listing.product_variant_id = variant.id
    if wrote_product:
        product.updated_at = datetime.utcnow()
        result.updated_product_ids.add(str(product.id))
        result.products_updated = len(result.updated_product_ids)
    return result


def reprocess_import_job_source_data(db: Session, job_id: uuid.UUID) -> SourceMergeResult:
    """Replay stored rows in source order; performs no external I/O."""
    job_query = db.query(ImportJob).filter(ImportJob.id == job_id)
    if db.bind.dialect.name == "postgresql":
        job_query = job_query.with_for_update()
    job = job_query.first()
    if not job:
        raise LookupError("Import job not found")
    result = SourceMergeResult()
    rows = db.query(ImportJobItem, SourceListing).join(
        SourceListing, SourceListing.id == ImportJobItem.source_listing_id,
    ).filter(
        ImportJobItem.import_job_id == job_id, SourceListing.is_deleted == False,
    ).order_by(ImportJobItem.source_row_number.asc()).all()
    for item, listing in rows:
        product_id = item.canonical_product_id or listing.canonical_product_id
        if not product_id:
            result.unlinked_listings_skipped += 1
            continue
        merge_source_listing(
            db, listing=listing, mapping=job.column_mapping or {}, canonical_product_id=product_id,
            product_variant_id=item.product_variant_id or listing.product_variant_id, result=result,
        )
    db.commit()
    return result
