"""Canonical, evidence-first formulation resolution.

All trustworthy INCI entry paths converge here.  ``Formulation`` is the
product-facing truth; FieldValue and corpus/crawl records remain provenance.
The resolver never infers ingredients and never lets lower-precedence evidence
replace a human or customer formulation.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    CanonicalProduct, FieldValue, Formulation, FormulationIngredient,
    IngredientDefinition, ProductVariant, SourceListing, ValidationIssue,
)
from app.scraping.ingredients import normalize_ingredient, split_inci


logger = logging.getLogger(__name__)

PRECEDENCE = {
    "human_edit": 60,
    "customer_source": 50,
    "internal_corpus": 40,
    "verified_evidence": 35,
    "licensed_web_search": 30,
    "crawl_html": 30,
}


@dataclass(frozen=True)
class FormulationResolution:
    status: str
    formulation: Formulation | None = None
    reason: str | None = None


def formulation_hash(raw_inci: str) -> str:
    # Keep the established live-Formulation hash contract used by enrichment
    # and crawler persistence. Parsed normalized identities live separately.
    return hashlib.sha256(str(raw_inci).strip().encode("utf-8")).hexdigest()


def _kind(formulation: Formulation) -> str:
    reference = str(formulation.source_reference or "")
    if reference.startswith("human_edit:"):
        return "human_edit"
    if formulation.source_listing_id or reference.startswith(("feed:", "customer_import:")):
        return "customer_source"
    if reference.startswith("knowledge_corpus:"):
        return "internal_corpus"
    if reference.startswith("licensed_web_search:"):
        return "licensed_web_search"
    if reference.startswith(("crawl_html:", "http://", "https://")):
        return "crawl_html"
    return "verified_evidence"


def _active_formulations(
    db: Session, product_id: uuid.UUID, variant_id: uuid.UUID | None,
) -> list[Formulation]:
    query = db.query(Formulation).filter(
        Formulation.canonical_product_id == product_id,
        Formulation.is_deleted == False,
    )
    if variant_id:
        query = query.filter(
            (Formulation.product_variant_id == variant_id)
            | (Formulation.product_variant_id.is_(None))
        )
    return query.order_by(Formulation.created_at.desc()).all()


def _normalize_ingredients(
    db: Session, formulation: Formulation, *, evidence_source: str,
) -> int:
    existing = db.query(FormulationIngredient).filter(
        FormulationIngredient.formulation_id == formulation.id,
    ).order_by(FormulationIngredient.position).all()
    parsed = split_inci(formulation.raw_inci_text)
    if len(existing) == len(parsed) and all(
        row.raw_inci_name == raw and row.position == position
        for position, (row, raw) in enumerate(zip(existing, parsed), 1)
    ):
        return len(existing)
    db.query(FormulationIngredient).filter(
        FormulationIngredient.formulation_id == formulation.id,
    ).delete(synchronize_session=False)
    for position, raw_name in enumerate(parsed, 1):
        normalized = normalize_ingredient(raw_name)
        definition = db.query(IngredientDefinition).filter(
            IngredientDefinition.normalized_name == normalized,
        ).first()
        db.add(FormulationIngredient(
            id=uuid.uuid4(), formulation_id=formulation.id,
            ingredient_definition_id=definition.id if definition else None,
            raw_inci_name=raw_name[:255], position=position,
            is_key_ingredient=False, key_ingredient_status="unknown",
            evidence_source=evidence_source[:255], confidence_score=1,
            evidence={"method": "exact_raw_inci_order", "normalized_name": normalized},
        ))
    db.flush()
    return len(parsed)


def _conflict(
    db: Session, product_id: uuid.UUID, variant_id: uuid.UUID | None, reason: str,
) -> None:
    existing = db.query(ValidationIssue).filter(
        ValidationIssue.canonical_product_id == product_id,
        ValidationIssue.product_variant_id.is_(None),
        ValidationIssue.field_name == "inci",
        ValidationIssue.issue_type == "formulation_conflict",
        ValidationIssue.resolved == False,
    ).first()
    if not existing:
        db.add(ValidationIssue(
            id=uuid.uuid4(), canonical_product_id=product_id,
            product_variant_id=None, field_name="inci", severity="warning",
            issue_type="formulation_conflict", message=reason,
            created_by_type="system",
        ))
        db.flush()


def promote_formulation(
    db: Session, *, product: CanonicalProduct, variant: ProductVariant | None,
    raw_inci_text: str, source_kind: str, source_reference: str,
    source_listing: SourceListing | None = None, market: str | None = None,
    language: str | None = None,
) -> FormulationResolution:
    """Promote exact evidence into canonical Formulation without guessing."""
    raw = str(raw_inci_text or "").strip()
    if not raw or not split_inci(raw):
        return FormulationResolution("unresolved", reason="empty_formulation")
    if source_kind not in PRECEDENCE:
        return FormulationResolution("rejected", reason="unsupported_source_kind")
    variant_id = variant.id if variant else None
    incoming_hash = formulation_hash(raw)
    active = _active_formulations(db, product.id, variant_id)
    same = next((row for row in active if row.content_hash == incoming_hash), None)
    if same:
        if PRECEDENCE[source_kind] >= PRECEDENCE[_kind(same)]:
            same.raw_inci_text = raw
            same.source_listing_id = source_listing.id if source_listing else same.source_listing_id
            same.source_reference = source_reference[:255]
            same.market = market or same.market
            same.language = language or same.language
        count = _normalize_ingredients(db, same, evidence_source=source_kind)
        logger.info("formulation_resolution idempotent product=%s variant=%s source=%s ingredients=%s",
                    product.id, variant_id, source_kind, count)
        return FormulationResolution("unchanged", same)

    incoming_rank = PRECEDENCE[source_kind]
    stronger = [row for row in active if PRECEDENCE[_kind(row)] > incoming_rank]
    if stronger:
        return FormulationResolution("rejected", stronger[0], "higher_precedence_formulation_exists")
    equal_conflicts = [row for row in active if PRECEDENCE[_kind(row)] == incoming_rank]
    if equal_conflicts and source_kind not in {"human_edit", "customer_source"}:
        _conflict(db, product.id, variant_id,
                  "Conflicting exact formulation evidence requires review; no formulation was replaced.")
        return FormulationResolution("conflicting", reason="same_precedence_conflict")

    for row in active:
        if PRECEDENCE[_kind(row)] <= incoming_rank:
            row.is_deleted = True
    formulation = Formulation(
        id=uuid.uuid4(), canonical_product_id=product.id,
        product_variant_id=variant_id,
        source_listing_id=source_listing.id if source_listing else None,
        raw_inci_text=raw, market=market, language=language,
        source_reference=source_reference[:255], content_hash=incoming_hash,
    )
    db.add(formulation)
    db.flush()
    count = _normalize_ingredients(db, formulation, evidence_source=source_kind)
    logger.info("formulation_resolution applied product=%s variant=%s source=%s ingredients=%s",
                product.id, variant_id, source_kind, count)
    return FormulationResolution("applied", formulation)


def synchronize_current_source_formulation(
    db: Session, product: CanonicalProduct, variant: ProductVariant | None,
) -> FormulationResolution:
    """Promote the already chronology-resolved current ingredients FieldValue."""
    current = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product.id,
        FieldValue.field_name == "ingredients", FieldValue.is_current == True,
        FieldValue.source_type.in_(["human_edit", "source_data"]),
    ).order_by(FieldValue.created_at.desc()).first()
    if not current or current.value in (None, "", [], {}):
        return FormulationResolution("unresolved", reason="no_current_source_formulation")
    evidence = current.evidence if isinstance(current.evidence, list) else []
    source_listing = None
    listing_id = next((row.get("source_listing_id") for row in evidence if isinstance(row, dict) and row.get("source_listing_id")), None)
    if listing_id:
        try:
            source_listing = db.query(SourceListing).filter(SourceListing.id == uuid.UUID(str(listing_id))).first()
        except (ValueError, TypeError):
            source_listing = None
    kind = "human_edit" if current.source_type == "human_edit" else "customer_source"
    explicit_customer = bool(source_listing) or any(
        isinstance(row, dict) and row.get("evidence_type") == "explicit_customer_source"
        for row in evidence
    )
    if kind == "customer_source" and not explicit_customer:
        return FormulationResolution("unresolved", reason="source_data_lacks_customer_import_provenance")
    reference = (f"human_edit:{current.id}" if kind == "human_edit" else
                 f"customer_import:{source_listing.import_job_id}:listing:{source_listing.id}" if source_listing else
                 f"customer_import:field:{current.id}")
    return promote_formulation(
        db, product=product, variant=variant, raw_inci_text=str(current.value),
        source_kind=kind, source_reference=reference, source_listing=source_listing,
    )


def promote_exact_corpus_formulation(
    db: Session, product: CanonicalProduct, variant: ProductVariant | None,
    corpus_result: dict[str, Any],
) -> FormulationResolution:
    from app.knowledge_corpus.retrieval import resolve_exact_field_evidence
    corpus_conflicts = [
        conflict for match in (corpus_result.get("exact_matches") or [])
        for conflict in (match.get("conflicts") or [])
        if conflict.get("field_name") == "raw_inci"
    ]
    if corpus_conflicts:
        _conflict(db, product.id, variant.id if variant else None,
                  "Conflicting exact Knowledge Corpus formulations require review.")
        return FormulationResolution("conflicting", reason="knowledge_formulation_conflict")
    exact = resolve_exact_field_evidence(corpus_result)
    if "raw_inci" in (exact.get("conflicts") or []):
        _conflict(db, product.id, variant.id if variant else None,
                  "Conflicting exact Knowledge Corpus formulations require review.")
        return FormulationResolution("conflicting", reason="knowledge_formulation_conflict")
    row = exact.get("formulation") or {}
    raw = row.get("raw_inci_text")
    if not raw:
        return FormulationResolution("unresolved", reason="no_exact_knowledge_formulation")
    reference = "knowledge_corpus:{product}:{variant}:{hash}".format(
        product=row.get("knowledge_product_id") or "unknown",
        variant=row.get("knowledge_variant_id") or "unknown",
        hash=row.get("formulation_hash") or formulation_hash(str(raw)),
    )
    return promote_formulation(
        db, product=product, variant=variant, raw_inci_text=str(raw),
        source_kind="internal_corpus", source_reference=reference,
        market=row.get("market"), language=row.get("language"),
    )
