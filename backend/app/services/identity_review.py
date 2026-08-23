"""Human-in-the-loop workflow built on the Product Understanding contract.

This module is intentionally decision/presentation oriented.  Foundational
truth remains in protected human ``FieldValue`` rows and the authoritative
``product_understanding`` contract; no second identity model is introduced.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import CanonicalProduct, FieldValue, ImportJob, SourceListing, ValidationIssue


IDENTITY_REVIEW_FIELD = "identity_review_state"
IDENTITY_ISSUE_TYPE = "foundational_identity_unresolved"
FOUNDATIONAL_FIELDS = {
    "brand", "product_name", "product_family", "variant", "gtin", "category",
    "subcategory", "product_type", "application_area", "category_module", "size",
}


def _current_value(db: Session, product_id: uuid.UUID, field_name: str) -> FieldValue | None:
    return db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product_id,
        FieldValue.field_name == field_name,
        FieldValue.is_current == True,
    ).order_by(FieldValue.created_at.desc()).first()


def current_understanding(db: Session, product_id: uuid.UUID) -> dict[str, Any]:
    row = _current_value(db, product_id, "product_understanding")
    return dict(row.value or {}) if row and isinstance(row.value, dict) else {}


def _human_fields(db: Session, product_id: uuid.UUID) -> set[str]:
    return {
        row.field_name for row in db.query(FieldValue.field_name).filter(
            FieldValue.canonical_product_id == product_id,
            FieldValue.source_type == "human_edit", FieldValue.review_status == "confirmed",
            FieldValue.is_current == True, FieldValue.field_name.in_(FOUNDATIONAL_FIELDS),
        ).all()
    }


def _latest_source(db: Session, product_id: uuid.UUID) -> tuple[dict[str, Any], dict[str, str]]:
    listing = db.query(SourceListing).filter(
        SourceListing.canonical_product_id == product_id, SourceListing.is_deleted == False,
    ).order_by(SourceListing.created_at.desc()).first()
    job = db.query(ImportJob).filter(ImportJob.id == listing.import_job_id).first() if listing else None
    return dict(listing.raw_data or {}) if listing else {}, dict(job.column_mapping or {}) if job else {}


def _compact_source(raw: dict[str, Any]) -> dict[str, Any]:
    preferred = (
        "ean", "gtin", "barcode", "article description", "product name", "brand",
        "supplier", "manufacturer", "category", "bgb subgroup", "bgb typegroup",
        "product type", "sku type", "size", "market", "country",
    )
    lookup = {re.sub(r"\W+", "", str(k).casefold()): (k, v) for k, v in raw.items()}
    selected: dict[str, Any] = {}
    for name in preferred:
        item = lookup.get(re.sub(r"\W+", "", name.casefold()))
        if item and item[1] not in (None, ""):
            selected[str(item[0])] = item[1]
    return selected


def _reasons(contract: dict[str, Any], summary: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    identity = contract.get("identity") or {}
    taxonomy = contract.get("taxonomy") or {}
    missing = list(summary.get("missing_identity_fields") or [])
    labels = {
        "brand": "Consumer brand could not be established.",
        "product_name": "Consumer-facing product name could not be established.",
        "product_family": "Product family could not be established.",
        "format": "Product type or format is ambiguous.",
        "variant": "Product family was identified, but the exact variant is unresolved.",
        "gtin": "No reliable exact product identifier is available.",
        "category": "Product category could not be established.",
        "product_type": "Product type is ambiguous.",
    }
    reasons.extend(labels.get(name, f"{name.replace('_', ' ').title()} remains unresolved.") for name in missing)
    if contract.get("category_module") == "unknown":
        reasons.append("Category evidence is insufficient to select a safe enrichment module.")
    for conflict in contract.get("conflicts") or []:
        field = str(conflict.get("field_name") or "identity").replace("_", " ")
        if conflict.get("type") == "human_vs_automatic":
            reasons.append(
                f"Human-confirmed {field} '{conflict.get('human_value')}' conflicts with new evidence "
                f"suggesting '{conflict.get('automatic_value')}'. The human value was retained."
            )
        elif conflict.get("severity") == "high":
            reasons.append(f"Strong evidence sources disagree about {field}.")
    if summary.get("identity_status") == "ambiguous" and not reasons:
        reasons.append("Multiple plausible product versions remain and exact variant identity is unsafe.")
    if not reasons and summary.get("identity_resolution_required"):
        reasons.append("Foundational identity is incomplete, so product-specific enrichment is paused.")
    return list(dict.fromkeys(reasons))


def does_this_product_require_identity_review(
    db: Session, product: CanonicalProduct, summary: dict[str, Any],
) -> dict[str, Any]:
    """Return the one canonical identity-review decision used by API/workers/UI."""
    contract = current_understanding(db, product.id)
    fingerprint = contract.get("foundational_fingerprint")
    state_row = _current_value(db, product.id, IDENTITY_REVIEW_FIELD)
    state = dict(state_row.value or {}) if state_row and isinstance(state_row.value, dict) else {}
    skipped_current = state.get("status") == "SKIPPED" and state.get("fingerprint") == fingerprint
    conflicts = [item for item in contract.get("conflicts") or [] if item.get("severity") == "high"]
    unresolved = bool(
        summary.get("identity_resolution_required")
        or summary.get("identity_status") in {"ambiguous", "incomplete", "unresolved", "conflicting"}
        or contract.get("identity_status") in {"unresolved", "partial", "conflicting"}
        or contract.get("category_module") == "unknown"
    )
    human_fields = _human_fields(db, product.id)
    if skipped_current:
        review_status = "SKIPPED"
    elif unresolved:
        review_status = "CONFLICT" if conflicts else "NEEDS_REVIEW"
    elif conflicts:
        review_status = "CONFLICT"
    elif human_fields:
        review_status = "REVIEWED"
    else:
        review_status = "RESOLVED"
    raw, mapping = _latest_source(db, product.id)
    resolved = {
        "brand": ((contract.get("identity") or {}).get("consumer_brand") or {}).get("value"),
        "product_family": ((contract.get("identity") or {}).get("product_family") or {}).get("value"),
        "variant": ((contract.get("identity") or {}).get("variant") or {}).get("value"),
        "gtin": ((contract.get("identity") or {}).get("gtin") or {}).get("value"),
        "size": ((contract.get("identity") or {}).get("size") or {}).get("value"),
        "category": ((contract.get("taxonomy") or {}).get("category") or {}).get("value"),
        "subcategory": ((contract.get("taxonomy") or {}).get("subcategory") or {}).get("value"),
        "product_type": ((contract.get("taxonomy") or {}).get("product_type") or {}).get("value"),
        "application_area": ((contract.get("taxonomy") or {}).get("application_area") or {}).get("value"),
        "category_module": contract.get("category_module"),
    }
    return {
        "requires_review": unresolved,
        "review_status": review_status,
        "reasons": _reasons(contract, summary),
        "understanding_fingerprint": fingerprint,
        "source_values": dict(contract.get("source_interpretation") or {}),
        "source_details": _compact_source(raw),
        "raw_source_available": bool(raw),
        "resolved_values": resolved,
        "human_confirmed_fields": sorted(human_fields),
        "match_type": contract.get("match_type") or summary.get("corpus_match_level"),
        "confidence": contract.get("confidence"),
        "conflicts": contract.get("conflicts") or [],
        "reconciliation_reason": contract.get("reconciliation_reason"),
        "mapping": mapping,
    }


def persist_review_state(
    db: Session, product: CanonicalProduct, *, status: str, fingerprint: str | None,
    actor_id: uuid.UUID, reason: str, resume_context: dict[str, Any] | None = None,
) -> FieldValue:
    previous = _current_value(db, product.id, IDENTITY_REVIEW_FIELD)
    if previous:
        previous.is_current = False
    row = FieldValue(
        id=uuid.uuid4(), canonical_product_id=product.id, field_name=IDENTITY_REVIEW_FIELD,
        value={"status": status, "fingerprint": fingerprint, "resume_context": resume_context or {},
               "updated_at": datetime.utcnow().isoformat()},
        source_type="human_edit", source_reference=f"user:{actor_id}", confidence_score=1,
        review_status="confirmed", reviewer_id=actor_id, is_current=True,
        override_reason=reason, semantic_status=status.lower(), semantic_status_type="identity_review",
    )
    db.add(row)
    return row


def synchronize_blocking_issue(db: Session, product: CanonicalProduct, decision: dict[str, Any]) -> None:
    if "requires_review" not in decision:
        return
    issues = db.query(ValidationIssue).filter(
        ValidationIssue.canonical_product_id == product.id,
        ValidationIssue.issue_type == IDENTITY_ISSUE_TYPE,
        ValidationIssue.resolved == False,
    ).all()
    if decision.get("requires_review"):
        message = "Identity confirmation required: " + " ".join(decision.get("reasons") or [])
        if issues:
            issues[0].message = message
        else:
            db.add(ValidationIssue(
                id=uuid.uuid4(), canonical_product_id=product.id, field_name="product_understanding",
                severity="blocking", issue_type=IDENTITY_ISSUE_TYPE, message=message,
                created_by_type="system",
            ))
    else:
        for issue in issues:
            issue.resolved = True
            issue.resolved_at = datetime.utcnow()
            issue.resolution_note = "Foundational identity resolved through Product Understanding review."
