"""Deterministic inventory and repair of historical ingredient state.

The module is intentionally self-contained and external-call free.  It never
rebuilds formulations: reconciliation only changes definition references,
legacy highlight flags, and inactive legacy intelligence fields.  Dry-run is
the default and contains the exact proposed mutation for every affected row.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any
from urllib.parse import urlparse
import uuid

from sqlalchemy.orm import Session

from app.models import (
    CanonicalProduct, FieldValue, Formulation, FormulationIngredient,
    IngredientDefinition, ProductVariant, SourceListing,
)
from app.knowledge_corpus.normalization import normalized_gtin
from app.services.formulation_resolution import (
    _kind, is_defensible_key_ingredient, normalize_ingredient_identity,
    resolve_selected_formulation, synchronize_current_source_formulation,
)


CLASS_SAFE_MERGE = "SAFE_CANONICAL_MERGE"
CLASS_METADATA_CLEANUP = "TRUSTED_IDENTITY_NEEDS_METADATA_CLEANUP"
CLASS_NO_MATCH = "NO_TRUSTED_MATCH"
CLASS_AMBIGUOUS = "AMBIGUOUS"
CLASS_FALSE_POSITIVE = "ALREADY_TRUSTED_FALSE_POSITIVE"
CLASSIFICATIONS = (
    CLASS_SAFE_MERGE, CLASS_METADATA_CLEANUP, CLASS_NO_MATCH,
    CLASS_AMBIGUOUS, CLASS_FALSE_POSITIVE,
)

INVENTORY_KEYS = (
    "eligible", "already_correct", "conflict", "outside_canonical_provenance",
    "ambiguous_product_level_formulation", "unsafe_legacy_key_ingredient",
    "suspected_ai_definition", "legacy_ingredients_intelligence",
)
ACTION_KEYS = (
    "eligible", "promoted", "already_correct", "skipped", "unresolved",
    "ambiguous", "conflict", "quarantined", "failed", "references_repointed",
    "references_unresolved", "metadata_cleaned", "legacy_fields_archived",
    "formulations_assigned",
)


def _bucket() -> dict[str, list[str]]:
    return defaultdict(list)


def _trusted_definition(definition: IngredientDefinition) -> bool:
    """Return true only for glossary identities with durable authority."""
    source = str(definition.source_name or "").casefold()
    record = str(definition.source_record_id or "").strip()
    return bool(record and source and (
        "cosing" in source
        or "european commission" in source
        or "trusted glossary" in source
        or "authoritative glossary" in source
    ))


def _authoritative_identity_without_full_provenance(definition: IngredientDefinition) -> bool:
    """Recognize an official CosIng identity whose historical record id was lost.

    This category permits identity retention only; unsupported descriptive
    metadata is removed.  An arbitrary URL/source label is never sufficient.
    """
    source = str(definition.source_name or "").casefold()
    host = urlparse(str(definition.source_url or "")).hostname or ""
    return ("cosing" in source or "european commission" in source) and (
        host == "ec.europa.eu" or host.endswith(".ec.europa.eu")
    )


def _definition_tokens(definition: IngredientDefinition) -> dict[str, set[str]]:
    names = {
        normalize_ingredient_identity(value)
        for value in (definition.name, definition.normalized_name)
        if value and normalize_ingredient_identity(value)
    }
    aliases = {
        normalize_ingredient_identity(value)
        for value in (definition.aliases or [])
        if value and normalize_ingredient_identity(value)
    }
    common = {
        normalize_ingredient_identity(definition.common_name)
    } if definition.common_name and normalize_ingredient_identity(definition.common_name) else set()
    return {
        "canonical_name": names,
        "trusted_alias": aliases,
        "trusted_common_name": common,
        "cas_number": {str(definition.cas_number).strip().casefold()} if definition.cas_number else set(),
        "ec_number": {str(definition.ec_number).strip().casefold()} if definition.ec_number else set(),
    }


def _trusted_indexes(definitions: list[IngredientDefinition]) -> dict[str, dict[str, set[uuid.UUID]]]:
    indexes: dict[str, dict[str, set[uuid.UUID]]] = {
        key: defaultdict(set) for key in (
            "canonical_name", "trusted_alias", "trusted_common_name", "cas_number", "ec_number"
        )
    }
    for definition in definitions:
        if not _trusted_definition(definition):
            continue
        for kind, values in _definition_tokens(definition).items():
            for value in values:
                indexes[kind][value].add(definition.id)
    return indexes


def _candidate_matches(
    definition: IngredientDefinition,
    indexes: dict[str, dict[str, set[uuid.UUID]]],
) -> dict[uuid.UUID, set[str]]:
    candidates: dict[uuid.UUID, set[str]] = defaultdict(set)
    tokens = _definition_tokens(definition)
    # A suspected canonical/common/alias spelling can match any trusted exact
    # identity spelling.  Stable identifiers remain identifier-specific.
    spelling_values = tokens["canonical_name"] | tokens["trusted_alias"] | tokens["trusted_common_name"]
    for value in spelling_values:
        for kind in ("canonical_name", "trusted_alias", "trusted_common_name"):
            for candidate_id in indexes[kind].get(value, set()):
                if candidate_id != definition.id:
                    candidates[candidate_id].add(f"exact_{kind}:{value}")
    for kind in ("cas_number", "ec_number"):
        for value in tokens[kind]:
            for candidate_id in indexes[kind].get(value, set()):
                if candidate_id != definition.id:
                    candidates[candidate_id].add(f"exact_{kind}:{value}")
    return candidates


def _suspected_definitions(db: Session) -> list[IngredientDefinition]:
    rows = db.query(IngredientDefinition).join(
        FormulationIngredient,
        FormulationIngredient.ingredient_definition_id == IngredientDefinition.id,
    ).filter(FormulationIngredient.evidence_source == "ai_inference").distinct().all()
    # A historical AI-authored formulation reference is not enough to condemn
    # an independently authoritative glossary record.  This preserves the
    # production inventory boundary while making those false positives safe by
    # construction.
    return sorted((row for row in rows if not _trusted_definition(row)), key=lambda row: str(row.id))


def _active_reference_count(db: Session, definition_id: uuid.UUID) -> int:
    return db.query(FormulationIngredient).join(Formulation).filter(
        FormulationIngredient.ingredient_definition_id == definition_id,
        Formulation.is_deleted == False,
    ).count()


def _build_definition_plan(db: Session) -> list[dict[str, Any]]:
    definitions = db.query(IngredientDefinition).all()
    by_id = {row.id: row for row in definitions}
    indexes = _trusted_indexes(definitions)
    plan: list[dict[str, Any]] = []
    for definition in _suspected_definitions(db):
        references = db.query(FormulationIngredient).filter(
            FormulationIngredient.ingredient_definition_id == definition.id,
        ).order_by(FormulationIngredient.formulation_id, FormulationIngredient.position).all()
        base = {
            "definition_id": str(definition.id),
            "name": definition.name,
            "normalized_name": definition.normalized_name,
            "references": len(references),
            "active_references": _active_reference_count(db, definition.id),
            "reference_ids": [str(row.id) for row in references],
            "current_state": {
                "source_name": definition.source_name,
                "source_record_id": definition.source_record_id,
                "function": definition.function,
                "benefits": definition.benefits,
                "possible_concerns": definition.possible_concerns,
            },
        }
        if _trusted_definition(definition):
            base.update({
                "classification": CLASS_FALSE_POSITIVE,
                "reason": "durable authoritative glossary provenance is present",
                "proposed_state": "retain definition and references unchanged",
                "candidate_ids": [],
            })
        elif _authoritative_identity_without_full_provenance(definition):
            base.update({
                "classification": CLASS_METADATA_CLEANUP,
                "reason": "official CosIng identity is deterministic but historical descriptive metadata lacks independent provenance",
                "proposed_state": "retain identity; clear unsupported function, benefits and possible concerns",
                "candidate_ids": [],
            })
        else:
            matches = _candidate_matches(definition, indexes)
            candidate_ids = sorted(matches, key=str)
            if len(candidate_ids) == 1:
                target = by_id[candidate_ids[0]]
                base.update({
                    "classification": CLASS_SAFE_MERGE,
                    "reason": "; ".join(sorted(matches[target.id])),
                    "trusted_target_id": str(target.id),
                    "trusted_target_name": target.name,
                    "proposed_state": f"repoint references to trusted definition {target.id}",
                    "candidate_ids": [str(target.id)],
                })
            elif candidate_ids:
                base.update({
                    "classification": CLASS_AMBIGUOUS,
                    "reason": "multiple deterministic trusted identities share the supplied exact identifier",
                    "proposed_state": "preserve raw formulation rows; clear ingredient_definition_id; manual review",
                    "candidate_ids": [str(value) for value in candidate_ids],
                    "candidates": [
                        {"id": str(value), "name": by_id[value].name, "signals": sorted(matches[value])}
                        for value in candidate_ids
                    ],
                })
            else:
                base.update({
                    "classification": CLASS_NO_MATCH,
                    "reason": "no deterministic match to an authoritative glossary identity",
                    "proposed_state": "preserve raw formulation rows; clear ingredient_definition_id",
                    "candidate_ids": [],
                })
        plan.append(base)
    return plan


def _key_plan(db: Session) -> dict[str, Any]:
    retained, quarantined = [], []
    for row in db.query(FormulationIngredient).filter(FormulationIngredient.is_key_ingredient == True).all():
        entry = {
            "id": str(row.id), "formulation_id": str(row.formulation_id),
            "raw_inci_name": row.raw_inci_name, "evidence_source": row.evidence_source,
        }
        (retained if is_defensible_key_ingredient(row) else quarantined).append(entry)
    return {
        "retained": retained, "retained_count": len(retained),
        "quarantined": quarantined, "quarantined_count": len(quarantined),
    }


def _legacy_intelligence_plan(db: Session) -> dict[str, Any]:
    categories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows = db.query(FieldValue).filter(
        FieldValue.field_name == "ingredients_intelligence", FieldValue.is_current == True,
    ).order_by(FieldValue.created_at, FieldValue.id).all()
    for row in rows:
        evidence = row.evidence if isinstance(row.evidence, list) else []
        exact = any(isinstance(item, dict) and str(
            item.get("match_type") or item.get("identity_scope") or ""
        ).casefold() in {"exact_gtin", "exact_product", "exact_variant", "exact_resolved_identity"} for item in evidence)
        if row.source_type in {"human_edit", "source_data"} and exact:
            category = "CONTAINS_VERIFIABLE_STRUCTURED_EVIDENCE"
        elif row.source_type in {"ai_inference", "ai_enrichment"}:
            category = "UNSAFE_AI_DERIVED"
        else:
            category = "SAFE_TO_IGNORE_AND_ARCHIVE"
        categories[category].append({
            "id": str(row.id), "product_id": str(row.canonical_product_id),
            "source_type": row.source_type,
            "action": "archive legacy field; do not promote into canonical ingredient data",
        })
    return {
        key: {"count": len(categories[key]), "records": categories[key]}
        for key in (
            "SAFE_TO_IGNORE_AND_ARCHIVE", "CONTAINS_VERIFIABLE_STRUCTURED_EVIDENCE",
            "UNSAFE_AI_DERIVED",
        )
    }


def _ambiguous_formulation_plan(db: Session) -> dict[str, Any]:
    assignable, manual = [], []
    formulations = db.query(Formulation).filter(
        Formulation.is_deleted == False, Formulation.product_variant_id.is_(None),
    ).all()
    variants_by_product: dict[uuid.UUID, list[ProductVariant]] = defaultdict(list)
    for variant in db.query(ProductVariant).filter(ProductVariant.is_deleted == False).all():
        variants_by_product[variant.canonical_product_id].append(variant)
    listing_ids = {row.source_listing_id for row in formulations if row.source_listing_id}
    listings_by_id = {
        row.id: row for row in (
            db.query(SourceListing).filter(SourceListing.id.in_(listing_ids)).all()
            if listing_ids else []
        )
    }
    for formulation in formulations:
        variants = variants_by_product.get(formulation.canonical_product_id, [])
        if len(variants) <= 1:
            continue
        listing = listings_by_id.get(formulation.source_listing_id)
        entry = {
            "formulation_id": str(formulation.id),
            "product_id": str(formulation.canonical_product_id),
            "variant_ids": [str(row.id) for row in variants],
            "source_listing_id": str(formulation.source_listing_id) if formulation.source_listing_id else None,
        }
        valid_variant_ids = {row.id for row in variants}
        linked_variant_id = listing.product_variant_id if listing else None
        raw_data = listing.raw_data if listing and isinstance(listing.raw_data, dict) else {}
        source_gtins = {
            normalized_gtin(value)
            for key, value in raw_data.items()
            if "".join(character for character in str(key).casefold() if character.isalnum())
            in {"ean", "eancode", "gtin", "gtincode", "upc", "barcode"}
        } - {None}
        gtin_matches = {
            row.id for row in variants
            if normalized_gtin(row.gtin) in source_gtins
        }
        if linked_variant_id in valid_variant_ids and (
            not gtin_matches or gtin_matches == {linked_variant_id}
        ):
            entry.update({
                "target_variant_id": str(linked_variant_id),
                "reason": "exact linked SourceListing identifies an active sibling variant",
            })
            assignable.append(entry)
        elif linked_variant_id is None and len(gtin_matches) == 1:
            target_variant_id = next(iter(gtin_matches))
            entry.update({
                "target_variant_id": str(target_variant_id),
                "reason": "normalized exact GTIN in SourceListing raw data identifies one active sibling variant",
            })
            assignable.append(entry)
        else:
            entry.update({
                "reason": (
                    "conflicting linked-variant and source-GTIN evidence"
                    if linked_variant_id in valid_variant_ids and gtin_matches
                    else "no deterministic exact source/GTIN linkage proves variant applicability"
                ),
                "action": "leave product-level; selected-formulation resolver quarantines it from sibling variants",
            })
            manual.append(entry)
    return {
        "safe_assignments": assignable, "safe_assignment_count": len(assignable),
        "manual_review": manual, "manual_review_count": len(manual),
    }


def _coverage_projection(db: Session, definition_plan: list[dict[str, Any]]) -> dict[str, Any]:
    all_rows = db.query(FormulationIngredient).join(Formulation).filter(Formulation.is_deleted == False).all()
    plan_by_id = {row["definition_id"]: row for row in definition_plan}
    definitions = {str(row.id): row for row in db.query(IngredientDefinition).all()}
    before_resolved = after_resolved = before_intelligence = after_intelligence = 0
    for row in all_rows:
        definition_id = str(row.ingredient_definition_id) if row.ingredient_definition_id else None
        current = definitions.get(definition_id or "")
        if current:
            before_resolved += 1
            if current.function or current.benefits or current.possible_concerns:
                before_intelligence += 1
        planned = plan_by_id.get(definition_id or "")
        after = current
        if planned and planned["classification"] in {CLASS_NO_MATCH, CLASS_AMBIGUOUS}:
            after = None
        elif planned and planned["classification"] == CLASS_SAFE_MERGE:
            after = definitions.get(planned.get("trusted_target_id", ""))
        if after:
            after_resolved += 1
            metadata_survives = bool(after.function or after.benefits or after.possible_concerns)
            if planned and planned["classification"] == CLASS_METADATA_CLEANUP:
                metadata_survives = False
            if metadata_survives:
                after_intelligence += 1
    total = len(all_rows)
    return {
        "active_ingredient_references": total,
        "identity_resolution": {
            "before_count": before_resolved, "after_count": after_resolved,
            "before_percent": round(before_resolved * 100 / total, 2) if total else 100.0,
            "after_percent": round(after_resolved * 100 / total, 2) if total else 100.0,
        },
        "trusted_intelligence": {
            "before_count": before_intelligence, "after_count": after_intelligence,
            "before_percent": round(before_intelligence * 100 / before_resolved, 2) if before_resolved else 0.0,
            "after_percent": round(after_intelligence * 100 / after_resolved, 2) if after_resolved else 0.0,
        },
    }


def build_historical_reconciliation_plan(db: Session) -> dict[str, Any]:
    definitions = _build_definition_plan(db)
    summary = {
        classification: {
            "definitions": sum(1 for row in definitions if row["classification"] == classification),
            "references": sum(row["active_references"] for row in definitions if row["classification"] == classification),
        }
        for classification in CLASSIFICATIONS
    }
    metadata = {
        "unsupported_ai_functions_removed_or_quarantined": sum(
            1 for row in definitions
            if row["classification"] != CLASS_FALSE_POSITIVE and row["current_state"].get("function")
        ),
        "unsupported_ai_benefits_removed_or_quarantined": sum(
            1 for row in definitions
            if row["classification"] != CLASS_FALSE_POSITIVE and row["current_state"].get("benefits")
        ),
        "unsupported_ai_concerns_removed_or_quarantined": sum(
            1 for row in definitions
            if row["classification"] != CLASS_FALSE_POSITIVE and row["current_state"].get("possible_concerns")
        ),
        "note": (
            "Metadata on repointed/unresolved orphan definitions is quarantined by removing all active references; "
            "metadata-cleanup identities are cleared in place."
        ),
    }
    return {
        "summary": summary,
        "definitions": definitions,
        "metadata_cleanup": metadata,
        "key_ingredients": _key_plan(db),
        "legacy_ingredients_intelligence": _legacy_intelligence_plan(db),
        "ambiguous_product_level_formulations": _ambiguous_formulation_plan(db),
        "coverage_projection": _coverage_projection(db, definitions),
    }


def inventory_legacy_ingredient_state(db: Session) -> dict[str, Any]:
    findings = _bucket()
    current_ingredients = db.query(FieldValue).filter(
        FieldValue.field_name == "ingredients", FieldValue.is_current == True,
        FieldValue.source_type.in_(["human_edit", "source_data"]),
    ).all()
    for field in current_ingredients:
        product = db.query(CanonicalProduct).filter(CanonicalProduct.id == field.canonical_product_id).first()
        if not product:
            continue
        variants = db.query(ProductVariant).filter(
            ProductVariant.canonical_product_id == product.id, ProductVariant.is_deleted == False,
        ).all()
        variant = variants[0] if len(variants) == 1 else None
        selected = resolve_selected_formulation(db, product.id, variant.id if variant else None)
        if not selected:
            findings["eligible"].append(str(product.id))
        elif str(selected.raw_inci_text).strip() == str(field.value).strip():
            findings["already_correct"].append(str(product.id))
        else:
            findings["conflict"].append(str(product.id))

    for formulation in db.query(Formulation).filter(Formulation.is_deleted == False).all():
        if _kind(formulation) == "verified_evidence" and not str(formulation.source_reference or "").startswith("verified:"):
            findings["outside_canonical_provenance"].append(str(formulation.id))
        variant_count = db.query(ProductVariant).filter(
            ProductVariant.canonical_product_id == formulation.canonical_product_id,
            ProductVariant.is_deleted == False,
        ).count()
        if formulation.product_variant_id is None and variant_count > 1:
            findings["ambiguous_product_level_formulation"].append(str(formulation.id))

    keys = _key_plan(db)
    findings["unsafe_legacy_key_ingredient"] = [row["id"] for row in keys["quarantined"]]
    findings["suspected_ai_definition"] = [str(row.id) for row in _suspected_definitions(db)]
    findings["legacy_ingredients_intelligence"] = [
        str(row.id) for row in db.query(FieldValue).filter(
            FieldValue.field_name == "ingredients_intelligence", FieldValue.is_current == True,
        ).all()
    ]
    return {
        key: {"count": len(findings[key]), "ids": sorted(set(findings[key]))}
        for key in INVENTORY_KEYS
    }


def repair_legacy_ingredient_state(db: Session, *, dry_run: bool = True) -> dict[str, Any]:
    before = inventory_legacy_ingredient_state(db)
    plan = build_historical_reconciliation_plan(db)
    result = _bucket()
    if dry_run:
        return {
            "dry_run": True, "inventory": before, "reconciliation": plan,
            "actions": {key: {"count": 0, "ids": []} for key in ACTION_KEYS},
        }

    # Report already-canonical source formulations without rewriting them.
    result["already_correct"].extend(before.get("already_correct", {}).get("ids", []))

    # Preserve the previous deterministic source-field repair for unambiguous
    # single-variant records, but do not scan/rewrite already-canonical products.
    for product_id in before.get("eligible", {}).get("ids", []):
        product = db.query(CanonicalProduct).filter(CanonicalProduct.id == uuid.UUID(product_id)).first()
        if not product:
            continue
        variants = db.query(ProductVariant).filter(
            ProductVariant.canonical_product_id == product.id, ProductVariant.is_deleted == False,
        ).all()
        if len(variants) > 1:
            result["ambiguous"].append(str(product.id)); continue
        resolution = synchronize_current_source_formulation(db, product, variants[0] if variants else None)
        result["promoted" if resolution.status == "applied" else "already_correct"].append(str(product.id))

    for item in plan["definitions"]:
        classification = item["classification"]
        references = db.query(FormulationIngredient).filter(
            FormulationIngredient.ingredient_definition_id == uuid.UUID(item["definition_id"]),
        ).all()
        if classification == CLASS_SAFE_MERGE:
            target_id = uuid.UUID(item["trusted_target_id"])
            for row in references:
                row.ingredient_definition_id = target_id
                result["references_repointed"].append(str(row.id))
        elif classification in {CLASS_NO_MATCH, CLASS_AMBIGUOUS}:
            for row in references:
                row.ingredient_definition_id = None
                result["references_unresolved"].append(str(row.id))
            result["ambiguous" if classification == CLASS_AMBIGUOUS else "unresolved"].append(item["definition_id"])
        elif classification == CLASS_METADATA_CLEANUP:
            definition = db.query(IngredientDefinition).filter(
                IngredientDefinition.id == uuid.UUID(item["definition_id"]),
            ).first()
            if definition:
                definition.function = None
                definition.benefits = None
                definition.possible_concerns = None
                result["metadata_cleaned"].append(str(definition.id))
        else:
            result["already_correct"].append(item["definition_id"])

    for item in plan["key_ingredients"]["quarantined"]:
        row = db.query(FormulationIngredient).filter(FormulationIngredient.id == uuid.UUID(item["id"])).first()
        if row:
            row.is_key_ingredient = False
            row.key_ingredient_status = "quarantined_legacy_unsupported"
            result["quarantined"].append(str(row.id))

    for category in plan["legacy_ingredients_intelligence"].values():
        for item in category["records"]:
            row = db.query(FieldValue).filter(FieldValue.id == uuid.UUID(item["id"])).first()
            if row and row.is_current:
                row.is_current = False
                result["legacy_fields_archived"].append(str(row.id))

    for item in plan["ambiguous_product_level_formulations"]["safe_assignments"]:
        formulation = db.query(Formulation).filter(Formulation.id == uuid.UUID(item["formulation_id"])).first()
        if formulation and formulation.product_variant_id is None:
            formulation.product_variant_id = uuid.UUID(item["target_variant_id"])
            result["formulations_assigned"].append(str(formulation.id))

    db.commit()
    return {
        "dry_run": False, "inventory": before, "reconciliation": plan,
        "actions": {
            key: {"count": len(result[key]), "ids": sorted(set(result[key]))}
            for key in ACTION_KEYS
        },
    }
