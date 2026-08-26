"""Deterministic, side-effect bounded merging of uploaded source rows.

This module intentionally has no dependency on AI enrichment, crawling, or
product research.  It is used both during normal feed processing and when an
operator replays already-stored SourceListing rows.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable

from sqlalchemy.orm import Session

from app.models import FieldValue, ImportJob, ImportJobItem, SourceListing


EMPTY_TEXT = {"", "none", "nan", "null", "not found", "not_found", "not provided"}


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


def _source_value(raw_data: Dict[str, Any], mapping: Dict[str, str], field: str, aliases: Iterable[str]) -> Any:
    mapped_column = (mapping or {}).get(field)
    if mapped_column:
        mapped_value = _clean((raw_data or {}).get(mapped_column))
        if mapped_value is not None:
            return mapped_value
    normalized = {_normalized_header(key): value for key, value in (raw_data or {}).items()}
    for alias in aliases:
        value = _clean(normalized.get(_normalized_header(alias)))
        if value is not None:
            return value
    return None


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


SOURCE_FIELD_SPECS = {
    "description": (
        "description",
        ("Product Description", "Description", "Long Description", "Marketing Description", "Marketing Copy"),
        lambda value: str(value).strip(),
    ),
    "benefits": (
        "benefits",
        ("Product Benefits", "Benefits", "Key Benefits"),
        _benefits,
    ),
    "product_usp": (
        "product_usp",
        ("Product USP", "USP", "Unique Selling Proposition"),
        lambda value: str(value).strip(),
    ),
}


@dataclass
class SourceMergeResult:
    listings_processed: int = 0
    products_updated: int = 0
    fields_written: int = 0
    fields_unchanged: int = 0
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


def merge_source_listing(
    db: Session,
    *,
    listing: SourceListing,
    mapping: Dict[str, str],
    canonical_product_id: uuid.UUID,
    result: SourceMergeResult | None = None,
) -> SourceMergeResult:
    """Apply explicit, non-empty uploaded values without invoking research."""
    result = result or SourceMergeResult()
    result.listings_processed += 1
    wrote_product = False
    raw_data = listing.raw_data or {}
    source_reference = f"feed:{listing.import_job_id}:listing:{listing.id}"

    for canonical_field, (mapping_field, aliases, normalizer) in SOURCE_FIELD_SPECS.items():
        raw_value = _source_value(raw_data, mapping, mapping_field, aliases)
        if raw_value is None:
            result.blank_values_skipped += 1
            continue
        value = normalizer(raw_value)
        if value in (None, "", [], {}):
            result.blank_values_skipped += 1
            continue

        current = db.query(FieldValue).filter(
            FieldValue.canonical_product_id == canonical_product_id,
            FieldValue.field_name == canonical_field,
            FieldValue.is_current == True,
        ).first()
        if current and current.source_type == "human_edit":
            result.human_values_protected += 1
            continue
        if current and _same_value(current.value, value):
            result.fields_unchanged += 1
            continue
        if current:
            current.is_current = False
            db.flush()

        db.add(FieldValue(
            id=uuid.uuid4(),
            canonical_product_id=canonical_product_id,
            product_variant_id=None,
            field_name=canonical_field,
            value=value,
            source_type="source_data",
            source_reference=source_reference,
            confidence_score=1.0,
            review_status="confirmed",
            is_current=True,
            evidence=[{
                "source_reference": source_reference,
                "source_field": (mapping or {}).get(mapping_field) or aliases[0],
                "supporting_text": str(raw_value)[:1000],
                "evidence_type": "explicit_source",
            }],
            reasoning_summary="Explicit non-empty value merged from the preserved uploaded source row.",
            semantic_status="explicit_source",
            semantic_status_type="source_data",
        ))
        result.fields_written += 1
        wrote_product = True

    if wrote_product:
        result.updated_product_ids.add(str(canonical_product_id))
        result.products_updated = len(result.updated_product_ids)
    return result


def reprocess_import_job_source_data(db: Session, job_id: uuid.UUID) -> SourceMergeResult:
    """Replay stored rows for an existing import job; performs no external I/O."""
    job_query = db.query(ImportJob).filter(ImportJob.id == job_id)
    if db.bind.dialect.name == "postgresql":
        # Serialize replay requests for one job so concurrent admin clicks
        # cannot race the unique current-field constraint.
        job_query = job_query.with_for_update()
    job = job_query.first()
    if not job:
        raise LookupError("Import job not found")
    result = SourceMergeResult()
    rows = db.query(ImportJobItem, SourceListing).join(
        SourceListing, SourceListing.id == ImportJobItem.source_listing_id,
    ).filter(
        ImportJobItem.import_job_id == job_id,
        SourceListing.is_deleted == False,
    ).order_by(ImportJobItem.source_row_number).all()
    for item, listing in rows:
        product_id = item.canonical_product_id or listing.canonical_product_id
        if not product_id:
            result.unlinked_listings_skipped += 1
            continue
        merge_source_listing(
            db, listing=listing, mapping=job.column_mapping or {},
            canonical_product_id=product_id, result=result,
        )
    db.commit()
    return result
