"""Deterministic inventory/repair for legacy ingredient state.

This module has no dependency on AI, web discovery or crawling.  Dry-run is
the caller-facing default and reports every affected identifier.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any
import uuid

from sqlalchemy.orm import Session

from app.models import (
    CanonicalProduct, FieldValue, Formulation, FormulationIngredient,
    IngredientDefinition, ProductVariant,
)
from app.services.formulation_resolution import (
    _kind, resolve_selected_formulation,
    synchronize_current_key_ingredients, synchronize_current_source_formulation,
)


def _bucket() -> dict[str, list[str]]:
    return defaultdict(list)


INVENTORY_KEYS = (
    "eligible", "already_correct", "conflict", "outside_canonical_provenance",
    "ambiguous_product_level_formulation", "unsafe_legacy_key_ingredient",
    "suspected_ai_definition", "legacy_ingredients_intelligence",
)
ACTION_KEYS = (
    "eligible", "promoted", "already_correct", "skipped", "unresolved",
    "ambiguous", "conflict", "quarantined", "failed",
)


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
            ProductVariant.canonical_product_id == product.id,
            ProductVariant.is_deleted == False,
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

    for row in db.query(FormulationIngredient).filter(
        FormulationIngredient.is_key_ingredient == True,
    ).all():
        evidence = row.evidence if isinstance(row.evidence, list) else []
        exact = any(isinstance(item, dict) and str(
            item.get("match_type") or item.get("identity_scope") or ""
        ).lower() in {"exact_product", "exact_variant", "exact_gtin", "exact_resolved_identity"} for item in evidence)
        if row.evidence_source == "ai_inference" or not exact:
            findings["unsafe_legacy_key_ingredient"].append(str(row.id))

    for definition in db.query(IngredientDefinition).all():
        ai_reference = db.query(FormulationIngredient).filter(
            FormulationIngredient.ingredient_definition_id == definition.id,
            FormulationIngredient.evidence_source == "ai_inference",
        ).first()
        if ai_reference and not definition.source_name and not definition.source_record_id:
            findings["suspected_ai_definition"].append(str(definition.id))

    legacy_intelligence = db.query(FieldValue).filter(
        FieldValue.field_name == "ingredients_intelligence",
        FieldValue.is_current == True,
    ).all()
    findings["legacy_ingredients_intelligence"] = [str(row.id) for row in legacy_intelligence]
    return {
        key: {"count": len(findings[key]), "ids": sorted(set(findings[key]))}
        for key in INVENTORY_KEYS
    }


def repair_legacy_ingredient_state(db: Session, *, dry_run: bool = True) -> dict[str, Any]:
    before = inventory_legacy_ingredient_state(db)
    result = _bucket()
    if dry_run:
        return {
            "dry_run": True, "inventory": before,
            "actions": {key: {"count": 0, "ids": []} for key in ACTION_KEYS},
        }

    products = db.query(CanonicalProduct).filter(CanonicalProduct.is_deleted == False).all()
    for product in products:
        variants = db.query(ProductVariant).filter(
            ProductVariant.canonical_product_id == product.id,
            ProductVariant.is_deleted == False,
        ).all()
        # A product-level legacy field does not identify which sibling variant it
        # belongs to.  Even GTIN-bearing siblings remain ambiguous here; repair
        # must never pick the first database row and contaminate a variant.
        if len(variants) > 1:
            result["ambiguous"].append(str(product.id))
            continue
        variant = variants[0] if variants else None
        resolution = synchronize_current_source_formulation(db, product, variant)
        if resolution.status == "applied":
            result["promoted"].append(str(product.id))
        elif resolution.status == "unchanged":
            result["already_correct"].append(str(product.id))
        elif resolution.status == "conflicting":
            result["conflict"].append(str(product.id))
        elif resolution.reason != "no_current_source_formulation":
            result["unresolved"].append(str(product.id))
        synchronize_current_key_ingredients(db, product, variant)

    for row_id in before.get("unsafe_legacy_key_ingredient", {}).get("ids", []):
        row = db.query(FormulationIngredient).filter(FormulationIngredient.id == uuid.UUID(row_id)).first()
        if row:
            row.is_key_ingredient = False
            row.key_ingredient_status = "quarantined_legacy_unsupported"
            result["quarantined"].append(str(row.id))
    db.commit()
    return {
        "dry_run": False, "inventory": before,
        "actions": {
            key: {"count": len(result[key]), "ids": sorted(set(result[key]))}
            for key in ACTION_KEYS
        },
    }
