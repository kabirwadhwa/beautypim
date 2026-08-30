from __future__ import annotations

from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.knowledge_corpus.normalization import normalized_brand, normalized_gtin, normalized_text, split_size
from app.models import (
    KnowledgeConflict, KnowledgeFieldObservation, KnowledgeFormulation, KnowledgeMarketObservation,
    KnowledgeProduct, KnowledgeSourceObservation, KnowledgeVariant,
)


DIRECT_ONLY_FIELDS = {"gtin", "claims", "raw_inci", "price", "availability", "clinical_claims", "testing_claims"}
FAMILY_SAFE_FIELDS = {
    "description", "benefits", "targeted_concerns", "directions", "product_positioning",
    "sensory_description", "category", "subcategory", "product_type", "application_area",
    "skin_types", "hair_types", "texture_format", "finish", "coverage",
}


def _category_module(value: Any) -> str:
    text = normalized_text(value)
    if any(token in text for token in ("fragrance", "perfume", "parfum", "eau de", "duft")):
        return "fragrance"
    if any(token in text for token in ("makeup", "make up", "lipstick", "foundation", "mascara", "concealer", "maquillage")):
        return "makeup"
    if any(token in text for token in ("hair", "shampoo", "conditioner", "scalp", "haar", "cheveux")):
        return "haircare"
    if any(token in text for token in ("skin", "face", "cream", "serum", "cleanser", "spf", "pflege", "huid", "visage")):
        return "skincare"
    return "unknown"


def _identity_terms(value: Any) -> set[str]:
    return {
        term for term in normalized_text(value).split()
        if len(term) > 2 and not term.isdigit() and term not in {"the", "with", "for", "and", "new"}
    }


def resolve_exact_field_evidence(result: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic, conflict-aware values from an exact corpus match.

    The LLM may interpret unresolved commercial gaps, but it should never be
    responsible for copying an unambiguous exact-source fact out of a prompt.
    """
    if result.get("match_level") != "exact_product":
        return {"values": {}, "evidence": {}, "conflicts": [], "formulation": None, "market": {}}
    values: dict[str, Any] = {}
    evidence: dict[str, list[dict[str, Any]]] = {}
    conflicts: list[str] = []
    formulations = []
    market_rows = []
    for match in result.get("exact_matches") or []:
        formulations.extend({
            **row,
            "knowledge_product_id": match.get("knowledge_product_id"),
            "knowledge_variant_id": match.get("knowledge_variant_id"),
        } for row in (match.get("formulations") or []))
        market_rows.extend(match.get("market_observations") or [])
        for field_name, rows in (match.get("fields") or {}).items():
            usable = [row for row in rows if row.get("value") not in (None, "", [], {})]
            distinct: dict[str, Any] = {}
            for row in usable:
                key = normalized_text(row.get("value")) if not isinstance(row.get("value"), (list, dict)) else repr(row.get("value"))
                distinct.setdefault(key, row.get("value"))
            if len(distinct) == 1:
                values[field_name] = next(iter(distinct.values()))
                evidence[field_name] = usable[:5]
            elif len(distinct) > 1:
                conflicts.append(field_name)
    formulation = None
    hashes = {row.get("formulation_hash") for row in formulations if row.get("raw_inci_text")}
    if len(hashes) == 1 and formulations:
        formulation = formulations[0]
    elif len(hashes) > 1:
        conflicts.append("raw_inci")
    market: dict[str, Any] = {}
    for key in ("image_url", "price", "availability", "currency"):
        candidates = [row.get(key) for row in market_rows if row.get(key) not in (None, "")]
        if candidates:
            market[key] = candidates[0]
    return {
        "values": values, "evidence": evidence, "conflicts": sorted(set(conflicts)),
        "formulation": formulation, "market": market,
    }


def _serialize_candidate(db: Session, product: KnowledgeProduct, variant: KnowledgeVariant | None, match_type: str) -> dict[str, Any]:
    query = db.query(KnowledgeFieldObservation).filter(KnowledgeFieldObservation.knowledge_product_id == product.id)
    if match_type == "exact_product" and variant:
        query = query.filter(or_(KnowledgeFieldObservation.knowledge_variant_id == variant.id, KnowledgeFieldObservation.knowledge_variant_id.is_(None)))
    elif match_type == "product_family":
        query = query.filter(KnowledgeFieldObservation.field_name.in_(FAMILY_SAFE_FIELDS))
    else:
        query = query.filter(KnowledgeFieldObservation.field_name.in_({"category", "subcategory", "product_type", "application_area", "product_positioning", "sensory_description", "texture_format"}))
    rows = query.order_by(KnowledgeFieldObservation.imported_at.desc()).limit(80).all()
    fields: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if match_type == "comparable" and row.field_name in DIRECT_ONLY_FIELDS:
            continue
        source = db.query(KnowledgeSourceObservation).filter(KnowledgeSourceObservation.id == row.source_observation_id).first()
        fields.setdefault(row.field_name, []).append({
            "value": row.normalized_value, "raw_value": row.raw_value,
            "source_type": "Retail Data", "match_type": match_type,
            "dataset_key": source.dataset_key if source else None,
            "sheet": source.source_sheet if source else None,
            "source_row": source.source_row_number if source else None,
            "source_url": source.source_url if source else None,
            "observed_at": (row.observed_at or row.imported_at).isoformat() if (row.observed_at or row.imported_at) else None,
        })
    # Older corpus imports already preserve complete raw rows. Surface review
    # aggregates from those rows without a destructive or storage-heavy
    # reimport solely to create three new field-observation records.
    if match_type == "exact_product" and variant:
        observations = db.query(KnowledgeSourceObservation).filter(
            KnowledgeSourceObservation.knowledge_product_id == product.id,
            KnowledgeSourceObservation.knowledge_variant_id == variant.id,
        ).order_by(KnowledgeSourceObservation.imported_at.desc()).limit(20).all()
        aliases = {
            "rating": ("rating", "average_rating", "review_rating"),
            "review_count": ("review_count", "reviews_count", "number_of_reviews"),
            "review_summary": ("review_summary", "reviews_summary"),
        }
        for source in observations:
            normalized_raw = {
                normalized_text(key).replace(" ", "_"): value
                for key, value in (source.raw_payload or {}).items()
            }
            for field_name, keys in aliases.items():
                value = next((normalized_raw.get(key) for key in keys if normalized_raw.get(key) not in (None, "")), None)
                if value is None:
                    continue
                fields.setdefault(field_name, []).append({
                    "value": value, "raw_value": value, "source_type": "Retail Data",
                    "match_type": match_type, "dataset_key": source.dataset_key,
                    "sheet": source.source_sheet, "source_row": source.source_row_number,
                    "source_url": source.source_url,
                    "observed_at": (source.observed_at or source.imported_at).isoformat()
                    if (source.observed_at or source.imported_at) else None,
                })
    formulations = []
    if match_type == "exact_product" and variant:
        formulations = [{"raw_inci_text": item.raw_inci_text, "ingredients": item.normalized_ingredients,
                         "market": item.market, "language": item.language, "formulation_hash": item.formulation_hash,
                         "source_type": "Retail Data", "match_type": match_type}
                        for item in db.query(KnowledgeFormulation).filter(KnowledgeFormulation.knowledge_variant_id == variant.id).limit(5)]
    market = []
    if match_type == "exact_product" and variant:
        market = [{"price": float(item.price) if item.price is not None else None, "currency": item.currency,
                   "availability": item.availability, "image_url": item.image_url, "source_url": item.source_url,
                   "market": item.market, "observed_at": (item.observed_at or item.imported_at).isoformat(),
                   "observation_date_type": item.observation_date_type, "source_type": "Retail Data"}
                  for item in db.query(KnowledgeMarketObservation).filter(KnowledgeMarketObservation.knowledge_variant_id == variant.id)
                  .order_by(KnowledgeMarketObservation.observed_at.desc(), KnowledgeMarketObservation.imported_at.desc()).limit(5)]
    conflict_query = db.query(KnowledgeConflict).filter(
        KnowledgeConflict.knowledge_product_id == product.id,
        KnowledgeConflict.status == "open",
    )
    if variant:
        conflict_query = conflict_query.filter(or_(
            KnowledgeConflict.knowledge_variant_id == variant.id,
            KnowledgeConflict.knowledge_variant_id.is_(None),
        ))
    conflicts = [{
        "field_name": item.field_name, "conflict_type": item.conflict_type,
        "values": item.values,
        "severity": "high" if item.field_name in {"gtin", "brand", "product_name", "raw_inci", "claims"}
            else "medium" if item.field_name in {"category", "subcategory", "product_type", "size", "shade"}
            else "low",
    } for item in conflict_query.limit(20).all()]
    return {
        "knowledge_product_id": str(product.id), "knowledge_variant_id": str(variant.id) if variant else None,
        "brand": product.brand_name, "product_name": product.product_name,
        "variant_name": variant.variant_name if variant else None, "gtin": variant.normalized_gtin if variant else None,
        "category": product.category, "subcategory": product.subcategory, "product_type": product.product_type,
        "match_type": match_type, "fields": fields, "formulations": formulations,
        "market_observations": market, "conflicts": conflicts,
    }


def retrieve_corpus_evidence(db: Session, *, gtin: str = "", source_product_id: str = "", source_parent_id: str = "", source_dataset: str = "", brand: str = "", product_name: str = "", size: str = "", shade: str = "", category: str = "", max_comparables: int = 5) -> dict[str, Any]:
    """Indexed exact/family retrieval followed by a small SQL-prefiltered comparable set."""
    normalized_code = normalized_gtin(gtin)
    exact: list[tuple[KnowledgeProduct, KnowledgeVariant]] = []
    if normalized_code:
        exact = db.query(KnowledgeProduct, KnowledgeVariant).join(KnowledgeVariant).filter(KnowledgeVariant.normalized_gtin == normalized_code).limit(5).all()
    if not exact and source_product_id:
        source_query = db.query(KnowledgeProduct, KnowledgeVariant).join(
            KnowledgeSourceObservation, KnowledgeSourceObservation.knowledge_product_id == KnowledgeProduct.id
        ).join(KnowledgeVariant, KnowledgeVariant.id == KnowledgeSourceObservation.knowledge_variant_id).filter(
            KnowledgeSourceObservation.source_record_id == source_product_id
        )
        if source_dataset:
            source_query = source_query.filter(KnowledgeSourceObservation.dataset_key == source_dataset)
        source_rows = source_query.limit(6).all()
        # Source IDs are only exact when dataset-scoped or globally unambiguous.
        if source_dataset or len({variant.id for _, variant in source_rows}) == 1:
            exact = source_rows
    norm_brand, norm_name = normalized_brand(brand), normalized_text(product_name)
    if not exact and norm_brand and norm_name:
        candidate_query = db.query(KnowledgeProduct, KnowledgeVariant).join(KnowledgeVariant).filter(
            KnowledgeProduct.normalized_brand == norm_brand,
            KnowledgeVariant.normalized_product_name == norm_name,
        )
        size_value, size_unit = split_size(size)
        norm_shade = normalized_text(shade)
        if size_value:
            candidate_query = candidate_query.filter(KnowledgeVariant.size_value == size_value)
            if size_unit:
                candidate_query = candidate_query.filter(KnowledgeVariant.size_unit == size_unit)
        if norm_shade:
            candidate_query = candidate_query.filter(KnowledgeVariant.normalized_variant == norm_shade)
        # Fetching two is intentional: one row proves exactness, two prove
        # ambiguity. Never let an arbitrary SQL LIMIT turn ambiguity into fact.
        candidates = candidate_query.limit(2).all()
        if len(candidates) == 1:
            exact = candidates
    if exact:
        serialized = [_serialize_candidate(db, p, v, "exact_product") for p, v in exact]
        has_identity_conflict = any(
            conflict.get("severity") == "high"
            for item in serialized for conflict in item.get("conflicts", [])
        )
        level = "conflict" if has_identity_conflict else "exact_product"
        return {"match_level": level, "exact_matches": serialized, "family_matches": [], "comparables": [],
                "diagnostics": {"normalized_gtin": normalized_code, "normalized_brand": norm_brand,
                                "normalized_product_name": norm_name, "matched_by": "gtin" if normalized_code else "exact_identity",
                                "candidate_count": len(serialized)}}

    family_products: list[KnowledgeProduct] = []
    if source_parent_id:
        family_query = db.query(KnowledgeProduct).join(KnowledgeSourceObservation).filter(
            KnowledgeSourceObservation.source_parent_id == source_parent_id)
        if source_dataset:
            family_query = family_query.filter(KnowledgeSourceObservation.dataset_key == source_dataset)
        family_products = family_query.distinct().limit(6).all()
        if not source_dataset and len(family_products) != 1:
            family_products = []
    if not family_products and norm_brand and norm_name:
        family_products = db.query(KnowledgeProduct).join(KnowledgeVariant).filter(
            KnowledgeProduct.normalized_brand == norm_brand,
            KnowledgeVariant.normalized_product_name == norm_name,
        ).distinct().limit(5).all()
    if family_products:
        return {"match_level": "product_family", "exact_matches": [], "family_matches": [_serialize_candidate(db, product, None, "product_family") for product in family_products], "comparables": [],
                "diagnostics": {"normalized_gtin": normalized_code, "normalized_brand": norm_brand,
                                "normalized_product_name": norm_name, "matched_by": "product_family",
                                "candidate_count": len(family_products)}}

    query = db.query(KnowledgeProduct)
    filters = []
    if norm_brand: filters.append(KnowledgeProduct.normalized_brand == norm_brand)
    category_text = normalized_text(category)
    if category_text: filters.append(or_(KnowledgeProduct.category.ilike(f"%{category_text}%"), KnowledgeProduct.product_type.ilike(f"%{category_text}%")))
    terms = [term for term in norm_name.split() if len(term) > 3][:3]
    if terms: filters.append(or_(*[KnowledgeProduct.searchable_text.contains(term) for term in terms]))
    if filters: query = query.filter(or_(*filters))
    products = query.order_by(KnowledgeProduct.normalized_brand, KnowledgeProduct.normalized_name).limit(max(40, max_comparables * 12)).all()
    # Comparable evidence must be truly comparable. Broad OR-prefilter matches
    # with no lexical overlap or a contradictory beauty module are rejected.
    target = set(norm_name.split()) | set(category_text.split())
    target_module = _category_module(f"{category} {product_name}")
    scored = []
    for item in products:
        candidate_terms = set((item.searchable_text or "").split())
        overlap = len(target & candidate_terms)
        candidate_module = _category_module(f"{item.category or ''} {item.product_type or ''} {item.product_name or ''}")
        if target_module != "unknown" and candidate_module != "unknown" and target_module != candidate_module:
            continue
        brand_match = bool(norm_brand and item.normalized_brand == norm_brand)
        name_overlap = len(_identity_terms(norm_name) & _identity_terms(item.normalized_name))
        score = overlap + (3 if brand_match else 0) + (name_overlap * 2)
        if score < 3 or (not brand_match and name_overlap == 0):
            continue
        scored.append((score, item))
    scored.sort(key=lambda row: (row[0], row[1].normalized_name), reverse=True)
    ranked = [item for _, item in scored[:max_comparables]]
    return {"match_level": "comparable" if ranked else "unmatched", "exact_matches": [], "family_matches": [],
            "comparables": [_serialize_candidate(db, product, None, "comparable") for product in ranked],
            "diagnostics": {"normalized_gtin": normalized_code, "normalized_brand": norm_brand,
                            "normalized_product_name": norm_name, "matched_by": "comparable" if ranked else "none",
                            "prefilter_count": len(products), "qualified_candidate_count": len(scored),
                            "top_scores": [score for score, _ in scored[:max_comparables]]}}


def evidence_is_sufficient(result: dict[str, Any], required_fields: set[str] | None = None) -> bool:
    if result.get("match_level") != "exact_product":
        return False
    matches = result.get("exact_matches") or []
    if not matches:
        return False
    fields = set()
    for item in matches:
        fields.update(item.get("fields", {}).keys())
        if item.get("formulations"): fields.add("raw_inci")
        if any(row.get("image_url") for row in item.get("market_observations", [])): fields.add("image_url")
    wanted = required_fields or {"description", "category", "product_type", "image_url"}
    return len(fields & wanted) >= min(3, len(wanted))


def public_evidence_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Remove dataset/tab/row internals from normal customer-facing responses."""
    def summarize(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "source_type": "Retail Data", "match_type": item.get("match_type"),
            "field_names": sorted(item.get("fields", {}).keys()),
            "formulation_observations": len(item.get("formulations", [])),
            "market_observations": len(item.get("market_observations", [])),
            "conflict_count": len(item.get("conflicts", [])),
            "conflict_fields": sorted({row.get("field_name") for row in item.get("conflicts", []) if row.get("field_name")}),
        }
    return {
        "match_level": result.get("match_level", "unmatched"),
        "exact_matches": [summarize(item) for item in result.get("exact_matches", [])],
        "family_matches": [summarize(item) for item in result.get("family_matches", [])],
        "comparables": [summarize(item) for item in result.get("comparables", [])],
        "diagnostics": result.get("diagnostics") or {},
    }
