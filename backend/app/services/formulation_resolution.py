"""Canonical, evidence-first formulation resolution.

All trustworthy INCI entry paths converge here.  ``Formulation`` is the
product-facing truth; FieldValue and corpus/crawl records remain provenance.
The resolver never infers ingredients and never lets lower-precedence evidence
replace a human or customer formulation.
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    CanonicalProduct, FieldValue, Formulation, FormulationIngredient,
    IngredientDefinition, ProductVariant, SourceListing, ValidationIssue,
)
from app.scraping.ingredients import split_inci


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


def normalize_ingredient_identity(value: str) -> str:
    """Normalize spelling/format only; never infer chemical equivalence."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("α", "alpha").replace("β", "beta").replace("γ", "gamma")
    text = "".join(
        character for character in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


@dataclass(frozen=True)
class IngredientIdentityResolution:
    definition: IngredientDefinition | None
    status: str
    method: str
    normalized_name: str
    candidates: tuple[str, ...] = ()


def resolve_ingredient_definition(
    db: Session, raw_name: str, definitions: list[IngredientDefinition] | None = None,
) -> IngredientIdentityResolution:
    """Resolve exact trusted glossary identities; ambiguity remains unresolved."""
    normalized = normalize_ingredient_identity(raw_name)
    if not normalized:
        return IngredientIdentityResolution(None, "unresolved", "empty", normalized)
    rows = definitions if definitions is not None else db.query(IngredientDefinition).all()
    exact = [row for row in rows if normalize_ingredient_identity(row.normalized_name or row.name) == normalized]
    if len(exact) == 1:
        return IngredientIdentityResolution(exact[0], "resolved", "canonical_exact", normalized)
    if len(exact) > 1:
        return IngredientIdentityResolution(None, "ambiguous", "canonical_exact", normalized,
                                            tuple(str(row.id) for row in exact))
    alias_matches: list[tuple[IngredientDefinition, str]] = []
    for row in rows:
        aliases = row.aliases if isinstance(row.aliases, list) else []
        if any(normalize_ingredient_identity(alias) == normalized for alias in aliases if alias):
            alias_matches.append((row, "trusted_alias"))
        elif row.common_name and normalize_ingredient_identity(row.common_name) == normalized:
            alias_matches.append((row, "trusted_common_name"))
        elif raw_name and str(raw_name).strip() in {str(row.cas_number or "").strip(), str(row.ec_number or "").strip()}:
            alias_matches.append((row, "trusted_identifier"))
    unique = {str(row.id): (row, method) for row, method in alias_matches}
    if len(unique) == 1:
        row, method = next(iter(unique.values()))
        return IngredientIdentityResolution(row, "resolved", method, normalized)
    if len(unique) > 1:
        return IngredientIdentityResolution(None, "ambiguous", "trusted_alias", normalized,
                                            tuple(sorted(unique)))
    return IngredientIdentityResolution(None, "unresolved", "no_trusted_match", normalized)


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
    definitions = db.query(IngredientDefinition).all()
    for position, raw_name in enumerate(parsed, 1):
        resolution = resolve_ingredient_definition(db, raw_name, definitions)
        definition = resolution.definition
        db.add(FormulationIngredient(
            id=uuid.uuid4(), formulation_id=formulation.id,
            ingredient_definition_id=definition.id if definition else None,
            raw_inci_name=raw_name[:255], position=position,
            is_key_ingredient=False, key_ingredient_status="unknown",
            evidence_source=evidence_source[:255], confidence_score=1,
            evidence={
                "formulation_method": "exact_raw_inci_order",
                "identity_resolution_status": resolution.status,
                "identity_resolution_method": resolution.method,
                "normalized_name": resolution.normalized_name,
                "candidate_definition_ids": list(resolution.candidates),
            },
        ))
    db.flush()
    return len(parsed)


def resolve_selected_formulation(
    db: Session, product_id: uuid.UUID, variant_id: uuid.UUID | None = None,
) -> Formulation | None:
    """Return canonical formulation deterministically without sibling leakage."""
    active = db.query(Formulation).filter(
        Formulation.canonical_product_id == product_id,
        Formulation.is_deleted == False,
    ).all()
    exact = [row for row in active if variant_id and row.product_variant_id == variant_id]
    if exact:
        return sorted(exact, key=lambda row: (PRECEDENCE[_kind(row)], row.created_at), reverse=True)[0]
    product_level = [row for row in active if row.product_variant_id is None]
    if variant_id:
        # A product-level formulation is safely applicable only when there is a
        # single active variant.  With siblings, applicability is ambiguous and
        # unresolved is safer than leaking one formula across concentrations,
        # shades or markets.
        active_variant_count = db.query(ProductVariant).filter(
            ProductVariant.canonical_product_id == product_id,
            ProductVariant.is_deleted == False,
        ).count()
        if active_variant_count > 1:
            return None
    if product_level:
        return sorted(product_level, key=lambda row: (PRECEDENCE[_kind(row)], row.created_at), reverse=True)[0]
    return None


def formulation_ingredient_rows(db: Session, formulation: Formulation | None) -> list[FormulationIngredient]:
    if not formulation:
        return []
    return db.query(FormulationIngredient).filter(
        FormulationIngredient.formulation_id == formulation.id,
    ).order_by(FormulationIngredient.position.asc()).all()


def is_defensible_key_ingredient(row: FormulationIngredient) -> bool:
    """Reject legacy/AI key flags unless exact-product highlight evidence exists."""
    if not row.is_key_ingredient or str(row.evidence_source or "") == "ai_inference":
        return False
    evidence = row.evidence if isinstance(row.evidence, list) else [row.evidence]
    exact_scopes = {"exact_product", "exact_variant", "exact_gtin", "exact_resolved_identity"}
    return any(
        isinstance(item, dict)
        and str(item.get("match_type") or item.get("match_scope") or item.get("identity_scope") or "").lower()
        in exact_scopes
        for item in evidence
    )


def apply_key_ingredient_highlights(
    db: Session, formulation: Formulation, highlights: list[str], *, evidence: list[dict[str, Any]],
    source_kind: str, replace_existing: bool = False,
) -> int:
    """Apply explicit exact-product highlights only; ingredient presence is insufficient."""
    accepted_evidence = [row for row in evidence if isinstance(row, dict) and str(
        row.get("match_type") or row.get("match_scope") or row.get("identity_scope") or ""
    ).lower() in {"exact_product", "exact_variant", "exact_gtin", "exact_resolved_identity"}]
    if not accepted_evidence or source_kind == "ai_inference":
        return 0
    wanted = {normalize_ingredient_identity(value) for value in highlights if value}
    rows_with_candidates: list[tuple[FormulationIngredient, set[str]]] = []
    key_rank = {
        "human_edit": 60, "source_data": 50, "customer_source": 50,
        "internal_corpus": 40, "verified_evidence": 35,
        "licensed_web_search": 30, "crawl_html": 30, "ai_inference": 0,
    }
    changed = 0
    for row in formulation_ingredient_rows(db, formulation):
        candidates = {normalize_ingredient_identity(row.raw_inci_name)}
        if row.ingredient_definition_id:
            definition = db.query(IngredientDefinition).filter(
                IngredientDefinition.id == row.ingredient_definition_id,
            ).first()
            if definition:
                candidates.add(normalize_ingredient_identity(definition.name))
                candidates.update(normalize_ingredient_identity(item) for item in (definition.aliases or []) if item)
                if definition.common_name:
                    candidates.add(normalize_ingredient_identity(definition.common_name))
        rows_with_candidates.append((row, candidates))
    if replace_existing:
        incoming_rank = key_rank.get(source_kind, 0)
        for row, candidates in rows_with_candidates:
            existing_rank = key_rank.get(str(row.evidence_source or ""), 0)
            if row.is_key_ingredient and not (candidates & wanted) and existing_rank <= incoming_rank:
                row.is_key_ingredient = False
                row.key_ingredient_status = "not_highlighted_by_current_source"
                row.evidence_source = source_kind
                row.evidence = accepted_evidence
    for row, candidates in rows_with_candidates:
        if candidates & wanted:
            row.is_key_ingredient = True
            row.key_ingredient_status = "source_supported"
            row.evidence_source = source_kind
            row.evidence = accepted_evidence
            row.confidence_score = 1
            changed += 1
    db.flush()
    return changed


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


def synchronize_current_key_ingredients(
    db: Session, product: CanonicalProduct, variant: ProductVariant | None,
) -> int:
    """Apply only current human/customer explicit product highlights."""
    current = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product.id,
        FieldValue.field_name == "key_ingredients_source",
        FieldValue.is_current == True,
        FieldValue.source_type.in_(["human_edit", "source_data"]),
    ).order_by(FieldValue.created_at.desc()).first()
    formulation = resolve_selected_formulation(db, product.id, variant.id if variant else None)
    if not current or not formulation:
        return 0
    values = current.value if isinstance(current.value, list) else [current.value]
    evidence = current.evidence if isinstance(current.evidence, list) else []
    exact_evidence = [{
        **row, "match_type": row.get("match_type") or "exact_product",
        "identity_scope": row.get("identity_scope") or "exact_resolved_identity",
    } for row in evidence if isinstance(row, dict) and (
        row.get("evidence_type") == "explicit_customer_source" or current.source_type == "human_edit"
    )]
    if current.source_type == "human_edit" and not exact_evidence:
        exact_evidence = [{
            "evidence_type": "human_edit",
            "match_type": "exact_product",
            "identity_scope": "exact_resolved_identity",
            "field_value_id": str(current.id),
        }]
    return apply_key_ingredient_highlights(
        db, formulation, [str(value) for value in values if value],
        evidence=exact_evidence, source_kind=current.source_type, replace_existing=True,
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
    result = promote_formulation(
        db, product=product, variant=variant, raw_inci_text=str(raw),
        source_kind="internal_corpus", source_reference=reference,
        market=row.get("market"), language=row.get("language"),
    )
    highlights = exact.get("values", {}).get("key_ingredients")
    if result.formulation and highlights and "key_ingredients" not in (exact.get("conflicts") or []):
        values = highlights if isinstance(highlights, list) else [highlights]
        evidence = [{
            **item, "identity_scope": "exact_resolved_identity",
            "match_type": item.get("match_type") or "exact_product",
        } for item in (exact.get("evidence", {}).get("key_ingredients") or [])]
        apply_key_ingredient_highlights(
            db, result.formulation, [str(value) for value in values],
            evidence=evidence, source_kind="internal_corpus", replace_existing=True,
        )
    return result
