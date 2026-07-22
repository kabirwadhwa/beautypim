"""Retrieval helpers for source-grounded cosmetic ingredient enrichment."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from app.models import IngredientDefinition
from app.services.deduplication import normalize_text


COSING_SOURCE_URL = (
    "https://single-market-economy.ec.europa.eu/sectors/cosmetics/"
    "cosmetic-ingredient-database_en"
)


def split_inci(raw_ingredients: str) -> List[str]:
    """Split the common comma/semicolon INCI representation without guessing."""
    return [part.strip() for part in re.split(r"[,;]", raw_ingredients or "") if part.strip()]


def retrieve_ingredient_knowledge(
    db: Session, raw_ingredients: str, limit: int = 40
) -> List[Dict[str, Any]]:
    """Return exact-name reference matches; fuzzy regulatory matching is unsafe."""
    requested = split_inci(raw_ingredients)[:limit]
    normalized = [normalize_text(name) for name in requested]
    if not normalized:
        return []

    rows = (
        db.query(IngredientDefinition)
        .filter(IngredientDefinition.normalized_name.in_(normalized))
        .all()
    )
    by_name = {row.normalized_name: row for row in rows}
    matches: List[Dict[str, Any]] = []
    for raw_name, normalized_name in zip(requested, normalized):
        row = by_name.get(normalized_name)
        if not row:
            continue
        matches.append(
            {
                "raw_name": raw_name,
                "inci_name": row.name,
                "functions": [
                    value.strip() for value in (row.function or "").split(",") if value.strip()
                ],
                "source": row.source_name or "ingredient glossary",
                "source_record_id": row.source_record_id,
                "source_url": row.source_url,
                "regulatory_status": row.regulatory_status,
                "cas_number": row.cas_number,
                "ec_number": row.ec_number,
            }
        )
    return matches


def build_ingredient_grounding_context(matches: List[Dict[str, Any]]) -> str:
    if not matches:
        return "No exact ingredient glossary matches were found."
    lines = []
    for match in matches:
        functions = ", ".join(match["functions"]) or "not specified"
        lines.append(
            f"- Raw: {match['raw_name']} | INCI: {match['inci_name']} | "
            f"Functions: {functions} | Status: {match['regulatory_status'] or 'not specified'} | "
            f"Source: {match['source']} record {match['source_record_id'] or 'unknown'}"
        )
    return "\n".join(lines)


def ground_fallback_ingredients(
    fallback: Dict[str, Any], matches: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Apply exact authoritative glossary matches when no LLM is available."""
    by_raw = {normalize_text(match["raw_name"]): match for match in matches}
    for ingredient in fallback.get("ingredients_intelligence", []):
        match = by_raw.get(normalize_text(ingredient.get("ingredient_name", "")))
        if not match:
            continue
        ingredient["normalized_inci_name"] = match["inci_name"]
        ingredient["functions"] = match["functions"]
        ingredient["confidence"] = 1.0
        ingredient["evidence"] = [
            {
                "source_reference": match["source_url"],
                "source_field": "ingredients",
                "supporting_text": f"Exact INCI glossary match for {match['inci_name']}.",
                "evidence_type": "authoritative_glossary_exact_match",
                "char_offsets": None,
            }
        ]
    return fallback
