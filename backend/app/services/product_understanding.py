"""Authoritative product identity, taxonomy and semantic-consistency service.

This service deliberately runs before generative enrichment.  It distinguishes
source roles from product facts, resolves exact corpus identities first and
produces one contract consumed by every downstream surface.
"""
from __future__ import annotations

import re
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.knowledge_corpus.retrieval import retrieve_corpus_evidence
from app.models import CanonicalProduct, Category, FieldValue, ProductVariant, ScrapedProductObservation


PLACEHOLDERS = {
    "", "-", "--", "n/a", "na", "none", "null", "nan", "unknown",
    "not provided", "not_provided", "not available", "std", "standard",
    "c", "both", "other", "misc", "miscellaneous",
}
LEGAL_ENTITY = re.compile(
    r"\b(?:gmbh|ltd|limited|llc|inc|incorporated|corp|corporation|sarl|sas|sa|bv|nv|plc|ag|spa)\b",
    re.I,
)

MODULE_TERMS = {
    "beauty_accessory": (
        "beauty tool", "beauty accessory", "cosmetic tool", "eyelash curler", "lash curler",
        "curler pad", "replacement pad", "refill pad", "makeup brush", "cosmetic brush",
        "sponge", "applicator", "tweezer", "tweezers", "sharpener",
    ),
    "fragrance": ("fragrance", "perfume", "parfum", "eau de", "cologne", "duft", "geur"),
    "haircare": ("hair", "shampoo", "conditioner", "scalp", "styling", "cheveux", "haar", "haare"),
    "makeup": ("makeup", "make-up", "maquillage", "foundation", "concealer", "lipstick", "lip", "lips", "lèvres", "lippen", "mascara", "eyeshadow", "blush"),
    "skincare": (
        "skincare", "skin care", "moistur", "serum", "cleanser", "cream", "lotion", "toner",
        "hyaluronic acid", "retinol", "hautpflege", "soin", "huidsverzorging",
        "body milk", "body lotion", "shower gel", "bath & shower", "bath and shower",
        "hand cream", "hand care", "body care", "bodycare", "duschgel", "körperpflege",
    ),
}
AREA_TERMS = {
    "Lips": ("lip", "lips", "lèvres", "lippen"),
    "Face": ("face", "facial", "visage", "gesicht", "gelaat"),
    "Eyes": ("eye", "eyes", "yeux", "augen", "ogen", "mascara"),
    "Hair": ("hair", "cheveux", "haar", "haare", "scalp"),
    "Body": ("body", "corps", "körper", "lichaam"),
    "Nails": ("nail", "nails", "ongles", "nägel", "nagels"),
}


def normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def is_placeholder(value: Any) -> bool:
    if value is None or value == [] or value == {}:
        return True
    if isinstance(value, dict):
        value = value.get("value", value.get("text", value.get("name")))
    text = normalized_text(value).lower().strip(" ._-/")
    return text in PLACEHOLDERS or len(text) == 1


def _source_value(raw: dict[str, Any], mapping: dict[str, str], key: str, *aliases: str) -> str:
    lookup = {re.sub(r"\W+", "", str(k).lower()): v for k, v in (raw or {}).items()}
    names = [mapping.get(key), key, *aliases]
    for name in names:
        if not name:
            continue
        value = raw.get(name) if name in raw else lookup.get(re.sub(r"\W+", "", str(name).lower()))
        if not is_placeholder(value):
            return normalized_text(value)
    return ""


def _field(value: Any = None, confidence: float = 0.0, status: str = "unresolved", evidence=None):
    return {"value": value, "confidence": confidence, "status": status, "evidence": evidence or []}


def _first_candidate_value(candidate: dict[str, Any], name: str) -> Any:
    direct = candidate.get(name)
    if not is_placeholder(direct):
        return direct
    for observation in (candidate.get("fields") or {}).get(name, []):
        value = observation.get("value")
        if not is_placeholder(value):
            return value
    return None


def infer_module(*values: Any) -> str:
    text = " ".join(normalized_text(v).lower() for v in values if not is_placeholder(v))
    scores = {name: sum(term in text for term in terms) for name, terms in MODULE_TERMS.items()}
    best = max(scores, key=scores.get) if scores else "unknown"
    return best if scores.get(best, 0) else "unknown"


def infer_area(*values: Any) -> str | None:
    text = " ".join(normalized_text(v).lower() for v in values if not is_placeholder(v))
    for area, terms in AREA_TERMS.items():
        if any(re.search(rf"\b{re.escape(term)}\b", text) for term in terms):
            return area
    return None


def infer_product_type(module: str, *values: Any) -> str | None:
    text = " ".join(normalized_text(v).lower() for v in values if not is_placeholder(v))
    rules = (
        ("Eyelash Curler Refill Pad", ("eyelash curler pad", "lash curler pad", "curler refill pad", "replacement curler pad", "refill pad")),
        ("Eyelash Curler", ("eyelash curler", "lash curler")),
        ("Makeup Brush", ("makeup brush", "cosmetic brush")),
        ("Makeup Sponge", ("makeup sponge", "beauty sponge", "beauty blender")),
        ("Applicator", ("applicator",)),
        ("Tweezers", ("tweezer", "tweezers")),
        ("Cosmetic Sharpener", ("cosmetic sharpener", "makeup sharpener", "pencil sharpener")),
        ("Liquid Lipstick", ("liquid lipstick", "lip maestro", "lip color", "lip colour")),
        ("Lipstick", ("lipstick", "rouge à lèvres")),
        ("Foundation", ("foundation", "fond de teint")),
        ("Mascara", ("mascara",)),
        ("Shampoo", ("shampoo", "shampoing")),
        ("Conditioner", ("conditioner", "après-shampooing")),
        ("Body Milk", ("body milk", "bodymilk")),
        ("Shower Gel", ("shower gel", "duschgel")),
        ("Hand Cream", ("hand cream", "handcreme")),
        ("Body Lotion", ("body lotion",)),
        ("Face Serum", ("face serum", "facial serum", "sérum visage")),
        ("Moisturizer", ("moisturizer", "moisturiser", "hydrating cream")),
        ("Eau de Toilette", ("eau de toilette", " edt ")),
        ("Eau de Parfum", ("eau de parfum", " edp ")),
        ("Parfum", ("parfum",)),
    )
    padded = f" {text} "
    for label, terms in rules:
        if any(term in padded for term in terms):
            return label
    return {"fragrance": "Fragrance", "makeup": "Makeup", "haircare": "Hair Care", "skincare": "Skin Care", "beauty_accessory": "Beauty Accessory"}.get(module)


def resolve_product_understanding(
    db: Session, *, raw_data: dict[str, Any] | None = None, mapping: dict[str, str] | None = None,
    product: CanonicalProduct | None = None, variant: ProductVariant | None = None,
) -> dict[str, Any]:
    raw, mapping = raw_data or {}, mapping or {}
    supplied_brand = _source_value(raw, mapping, "brand", "brand name", "manufacturer brand")
    supplier = _source_value(raw, mapping, "supplier", "supplier name", "vendor", "manufacturer")
    name = _source_value(raw, mapping, "product_name", "product name", "name", "description", "article description", "article name")
    gtin = _source_value(raw, mapping, "ean", "gtin", "ean", "upc", "barcode") or (variant.gtin if variant else "")
    category = _source_value(raw, mapping, "category", "department", "division", "bgb subgroup", "category group")
    source_subcategory = _source_value(raw, mapping, "subcategory", "bgb typegroup", "type group", "category subgroup")
    source_type = _source_value(raw, mapping, "product_type", "product type", "type", "format", "concentration", "sku type")
    source_family = _source_value(raw, mapping, "product_family", "product family", "family", "base product")
    size = _source_value(raw, mapping, "size", "volume", "net content") or (variant.size if variant else "")
    sku = _source_value(raw, mapping, "sku", "article number", "item number")
    source_parent_id = _source_value(raw, mapping, "parent_id", "parent id", "base product id", "base product code")
    source_product_id = _source_value(raw, mapping, "source_product_id", "product id", "retailer product id")

    if not supplied_brand and product and product.brand:
        supplied_brand = product.brand.name
    if not name and product:
        name = product.product_name

    corpus = retrieve_corpus_evidence(
        db, gtin=gtin, source_product_id=source_product_id, source_parent_id=source_parent_id,
        brand=supplied_brand, product_name=name, size=size, category=category, max_comparables=5,
    )
    exact = (corpus.get("exact_matches") or [None])[0]
    conflicts = list(exact.get("conflicts") or []) if exact else []
    exact_safe = bool(exact) and corpus.get("match_level") == "exact_product" and not any(
        row.get("severity") == "high" for row in conflicts
    )
    if not exact_safe and product:
        researched = db.query(ScrapedProductObservation).filter(
            ScrapedProductObservation.canonical_product_id == product.id,
            ScrapedProductObservation.match_status == "matched",
        ).order_by(ScrapedProductObservation.scraped_at.desc()).first()
        if researched:
            payload = researched.normalized_payload or {}
            observed_gtin = normalized_text(payload.get("gtin") or payload.get("ean") or payload.get("upc"))
            # A persisted exact match may resolve identity, but only when its
            # identifier agrees or no conflicting identifier was supplied.
            if not gtin or not observed_gtin or re.sub(r"\D", "", gtin) == re.sub(r"\D", "", observed_gtin):
                exact = {
                    **payload,
                    "brand": payload.get("brand"), "product_name": payload.get("product_name"),
                    "variant_name": payload.get("variant_name") or payload.get("shade"),
                    "match_type": "exact_product_research", "conflicts": [],
                }
                exact_safe = bool(payload.get("brand") and payload.get("product_name"))
                if exact_safe:
                    corpus = {**corpus, "match_level": "exact_product_research"}
    evidence = [{
        "source_type": "Retail Data" if corpus.get("match_level") == "exact_product" else "Web Research",
        "match_type": "exact_gtin" if gtin else "exact_identity",
    }] if exact_safe else []

    brand_value = _first_candidate_value(exact, "brand") if exact_safe else supplied_brand
    product_value = _first_candidate_value(exact, "product_name") if exact_safe else name
    category_value = _first_candidate_value(exact, "category") if exact_safe else category
    subcategory_value = _first_candidate_value(exact, "subcategory") if exact_safe else source_subcategory
    type_value = _first_candidate_value(exact, "product_type") if exact_safe else None
    variant_value = _first_candidate_value(exact, "variant_name") if exact_safe else (variant.variant_name if variant else None)

    human: dict[str, Any] = {}
    if product:
        rows = db.query(FieldValue).filter(
            FieldValue.canonical_product_id == product.id,
            FieldValue.source_type == "human_edit", FieldValue.is_current == True,
            FieldValue.field_name.in_([
                "brand", "product_name", "product_family", "variant", "gtin",
                "category", "subcategory", "product_type", "application_area", "category_module",
            ]),
        ).all()
        human = {row.field_name: row.value for row in rows if not is_placeholder(row.value)}
    if variant:
        variant_rows = db.query(FieldValue).filter(
            FieldValue.product_variant_id == variant.id,
            FieldValue.source_type == "human_edit", FieldValue.is_current == True,
            FieldValue.field_name.in_(["variant", "gtin", "size"]),
        ).all()
        human.update({row.field_name: row.value for row in variant_rows if not is_placeholder(row.value)})
    evidence_conflicts = list(conflicts)

    def human_wins(field: str, automatic: Any) -> Any:
        value = human.get(field)
        if is_placeholder(value):
            return automatic
        if not is_placeholder(automatic) and normalized_text(value).casefold() != normalized_text(automatic).casefold():
            evidence_conflicts.append({
                "field_name": field, "severity": "high", "type": "human_vs_automatic",
                "human_value": value, "automatic_value": automatic,
                "resolution": "human_value_preserved",
            })
        return value

    brand_value = human_wins("brand", brand_value)
    product_value = human_wins("product_name", human_wins("product_family", product_value))
    variant_value = human_wins("variant", variant_value)
    gtin = human_wins("gtin", gtin)
    category_value = human_wins("category", category_value)
    subcategory_value = human_wins("subcategory", subcategory_value)
    type_value = human_wins("product_type", type_value)

    # An existing canonical record is the authoritative identity presented by
    # every business surface.  Research titles (including translated/localized
    # names) are evidence and aliases, never implicit rename instructions.
    # New imports still resolve normally because ``product`` is absent during
    # their pre-canonical understanding pass.
    if product:
        canonical_brand = product.brand.name if product.brand else None
        if not is_placeholder(canonical_brand):
            brand_value = canonical_brand
        if not is_placeholder(product.product_name):
            product_value = product.product_name
        if variant and not is_placeholder(variant.gtin):
            gtin = variant.gtin
        if variant and not is_placeholder(variant.variant_name):
            variant_value = variant.variant_name
        canonical_category = db.query(Category).filter(Category.id == product.category_id).first() if db and product.category_id else None
        if canonical_category:
            category_parts = [part.strip() for part in str(canonical_category.path or canonical_category.name or "").split(">") if part.strip()]
            if category_parts:
                category_value = category_parts[0]
                if len(category_parts) > 1:
                    subcategory_value = category_parts[-1]

    role_warnings = []
    if supplier and LEGAL_ENTITY.search(supplier):
        role_warnings.append("Supplier/manufacturer legal entity retained as source metadata; not used as consumer brand.")
    if supplied_brand and LEGAL_ENTITY.search(supplied_brand) and exact_safe and brand_value != supplied_brand:
        supplier = supplier or supplied_brand
        role_warnings.append("Mapped brand was a legal entity and was corrected using exact-product evidence.")
    elif supplied_brand and LEGAL_ENTITY.search(supplied_brand) and not exact_safe:
        supplier = supplier or supplied_brand
        brand_value = None
        role_warnings.append("Mapped brand appears to be a legal entity; consumer brand remains unresolved.")
    if exact_safe and supplied_brand and brand_value and supplied_brand.lower() != str(brand_value).lower():
        role_warnings.append("Source brand differs from exact-product consumer brand; source value retained as evidence.")

    signal_values = (category_value, subcategory_value, type_value, source_family, source_type, product_value, name)
    human_taxonomy_module = infer_module(human.get("category"), human.get("subcategory"), human.get("product_type"))
    module = human_wins(
        "category_module",
        human_taxonomy_module if human_taxonomy_module != "unknown" else infer_module(*signal_values),
    )
    if human_taxonomy_module != "unknown":
        automatic_child_module = infer_module(subcategory_value, type_value)
        if automatic_child_module not in {"unknown", human_taxonomy_module}:
            for field, value in (("subcategory", subcategory_value), ("product_type", type_value)):
                if field not in human and not is_placeholder(value):
                    evidence_conflicts.append({
                        "field_name": field, "severity": "high", "type": "human_taxonomy_vs_automatic",
                        "human_value": human.get("category"), "automatic_value": value,
                        "resolution": "human_taxonomy_preserved",
                    })
            subcategory_value = human.get("subcategory")
            type_value = human.get("product_type")
    if is_placeholder(type_value):
        type_value = infer_product_type(module, *signal_values)
    area = infer_area(*signal_values)
    if module == "beauty_accessory":
        accessory_text = " ".join(normalized_text(value).lower() for value in signal_values if not is_placeholder(value))
        if any(term in accessory_text for term in ("eyelash", "lash curler", "eye applicator")):
            area = "Eyes"
            if is_placeholder(subcategory_value):
                subcategory_value = "Eye Tools & Accessories"
        elif is_placeholder(subcategory_value):
            subcategory_value = "Cosmetic Tools & Accessories"
    if module == "makeup" and not area and any(term in normalized_text(product_value).lower() for term in ("lip", "rouge")):
        area = "Lips"

    source_identity_sufficient = bool(
        product_value and brand_value and re.sub(r"\D", "", str(gtin or ""))
        and module != "unknown"
    )
    # GTIN is the strongest route, but it is not universally available. A
    # human-confirmed taxonomy/type plus an established brand and product can
    # also safely resolve the foundational gate without fabricating an ID.
    human_identity_sufficient = bool(
        product_value and brand_value and module != "unknown"
        and (human.get("category") or human.get("category_module"))
        and (human.get("product_type") or human.get("subcategory"))
    )
    identity_status = "resolved" if exact_safe or source_identity_sufficient or human_identity_sufficient else "partial" if product_value and brand_value else "unresolved"
    if corpus.get("match_level") == "conflict":
        identity_status = "conflicting"
    confidence = 0.99 if exact_safe else 0.96 if human_identity_sufficient else 0.9 if source_identity_sufficient else 0.82 if identity_status == "partial" else 0.0
    source_status = "source_supported" if exact_safe or source_identity_sufficient else "human_confirmed" if human_identity_sufficient else "source_interpreted" if identity_status == "partial" else "unresolved"
    taxonomy_confidence = 0.98 if exact_safe and category_value else 0.82 if module != "unknown" else 0.0
    taxonomy_resolved = bool(module != "unknown" and (category_value or type_value or infer_product_type(module, *signal_values)))
    taxonomy_status = "source_supported" if exact_safe and taxonomy_resolved else "inferred" if taxonomy_resolved else "unresolved"
    research = []
    if identity_status != "resolved":
        research.extend(["consumer_brand", "product_family", "variant"])
    if module == "unknown":
        research.extend(["category", "subcategory", "product_type", "application_area"])

    return {
        "contract_version": "1.1", "identity_status": identity_status,
        "taxonomy_status": "resolved" if taxonomy_resolved else "needs_review",
        "match_type": "exact_gtin" if exact_safe and gtin else corpus.get("match_level", "unmatched"),
        "identity": {
            "consumer_brand": _field(brand_value or None, confidence, source_status, evidence),
            "product_family": _field(product_value or None, confidence, source_status, evidence),
            "variant": _field(variant_value or None, confidence if variant_value else 0.0, source_status if variant_value else "unresolved", evidence),
            "gtin": _field(gtin or None, 1.0 if gtin else 0.0, "source_supported" if gtin else "unresolved", evidence),
            "sku": _field(sku or None, 1.0 if sku else 0.0, "source_supported" if sku else "unresolved"),
            "size": _field(size or None, 1.0 if size else 0.0, "source_supported" if size else "unresolved"),
        },
        "taxonomy": {
            "category": _field(category_value or ({"fragrance": "Fragrance", "makeup": "Makeup", "haircare": "Hair Care", "skincare": "Skin Care", "beauty_accessory": "Beauty Tools & Accessories"}.get(module)), taxonomy_confidence, taxonomy_status, evidence),
            "subcategory": _field(subcategory_value or area, taxonomy_confidence if (subcategory_value or area) else 0.0, taxonomy_status if (subcategory_value or area) else "unresolved", evidence),
            "product_type": _field(type_value, taxonomy_confidence if type_value else 0.0, taxonomy_status if type_value else "unresolved", evidence),
            "application_area": _field(area, 0.88 if area else 0.0, "inferred" if area else "unresolved", evidence),
        },
        "category_module": module, "confidence": min(confidence or 1.0, taxonomy_confidence or 1.0),
        "conflicts": evidence_conflicts, "warnings": role_warnings,
        "reconciliation_reason": (
            "Human-confirmed identity/taxonomy values were preserved; contradictory automatic evidence was routed to review."
            if human and evidence_conflicts else
            "Exact product evidence resolved identity; taxonomy requires separate review."
            if exact_safe and not taxonomy_resolved else
            "Exact product evidence resolved identity and taxonomy before enrichment."
            if exact_safe else
            "Human-confirmed foundational identity and taxonomy resolved the enrichment dependency gate."
            if human_identity_sufficient else
            "Customer source identity was used; unresolved identity/taxonomy fields require targeted research."
        ),
        "source_interpretation": {
            "supplier": supplier or None, "source_brand": supplied_brand or None,
            "source_product_family": source_family or None, "source_product_type": source_type or None,
            "source_category": category or None, "source_subcategory": source_subcategory or None,
            "raw_source_preserved": True,
        },
        "research_plan": {"identity_first": bool(research), "objectives": list(dict.fromkeys(research))},
        "corpus_match_level": corpus.get("match_level", "unmatched"),
        "foundational_fingerprint": hashlib.sha256(json.dumps({
            "raw": {"brand": supplied_brand, "name": name, "gtin": gtin, "category": category,
                    "subcategory": source_subcategory, "type": source_type, "family": source_family,
                    "size": size, "sku": sku},
            "human": human, "variant": variant_value,
            "corpus": {"match_level": corpus.get("match_level"),
                       "knowledge_product_id": exact.get("knowledge_product_id") if exact else None,
                       "knowledge_variant_id": exact.get("knowledge_variant_id") if exact else None},
        }, sort_keys=True, default=str).encode()).hexdigest(),
    }


def refresh_contracts_after_corpus_import(db: Session) -> int:
    """Refresh affected customer contracts when stronger exact corpus data arrives."""
    from app.models import ImportJob, SourceListing
    refreshed = 0
    products = db.query(CanonicalProduct).join(ProductVariant).filter(
        CanonicalProduct.is_deleted == False, ProductVariant.is_deleted == False,
        ProductVariant.gtin.isnot(None),
    ).distinct().all()
    for product in products:
        variant = db.query(ProductVariant).filter(
            ProductVariant.canonical_product_id == product.id, ProductVariant.is_deleted == False,
            ProductVariant.gtin.isnot(None),
        ).first()
        listing = db.query(SourceListing).filter(
            SourceListing.canonical_product_id == product.id, SourceListing.is_deleted == False,
        ).order_by(SourceListing.created_at.desc()).first()
        job = db.query(ImportJob).filter(ImportJob.id == listing.import_job_id).first() if listing and listing.import_job_id else None
        contract = resolve_product_understanding(
            db, raw_data=(listing.raw_data or {}) if listing else {}, mapping=(job.column_mapping or {}) if job else {},
            product=product, variant=variant,
        )
        current = db.query(FieldValue).filter(
            FieldValue.canonical_product_id == product.id, FieldValue.field_name == "product_understanding",
            FieldValue.is_current == True,
        ).first()
        if current and isinstance(current.value, dict) and current.value.get("foundational_fingerprint") == contract.get("foundational_fingerprint"):
            continue
        if current:
            current.is_current = False
        db.add(FieldValue(
            id=uuid.uuid4(), canonical_product_id=product.id,
            field_name="product_understanding", value=contract, source_type="deterministic_rule",
            source_reference="knowledge-corpus-import", confidence_score=float(contract.get("confidence") or 0),
            review_status="conflicting" if contract.get("conflicts") else "confirmed" if contract.get("identity_status") == "resolved" else "inferred",
            is_current=True, evidence=[], reasoning_summary="Recalculated after knowledge-corpus evidence changed.",
            semantic_status=contract.get("identity_status"), semantic_status_type="product_understanding",
        ))
        refreshed += 1
    return refreshed


def understanding_snapshot_values(understanding: dict[str, Any]) -> dict[str, Any]:
    identity, taxonomy = understanding.get("identity", {}), understanding.get("taxonomy", {})
    value = lambda block, key: (block.get(key) or {}).get("value")
    return {
        "brand": value(identity, "consumer_brand"), "product_name": value(identity, "product_family"),
        "gtin": value(identity, "gtin"), "size": value(identity, "size"),
        "category": value(taxonomy, "category"), "subcategory": value(taxonomy, "subcategory"),
        "product_type": value(taxonomy, "product_type"), "application_area": value(taxonomy, "application_area"),
        "category_module": understanding.get("category_module"), "product_understanding": understanding,
    }


def understanding_contract_changed(current: Any, resolved: dict[str, Any]) -> bool:
    """Return whether foundational Product Understanding must be versioned.

    This deliberately ignores the caller's enrichment-field selection mode.
    Identity/taxonomy state is authoritative orchestration state and therefore
    must never remain stale merely because the user chose ``missing_only``.
    """
    if not isinstance(current, dict):
        return True
    return current != resolved


def semantic_issues(understanding: dict[str, Any], payload: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Deterministic high-severity contradictions and placeholder leakage."""
    payload = payload or {}
    module = understanding.get("category_module", "unknown")
    issues: list[dict[str, str]] = []
    for section in (understanding.get("identity", {}), understanding.get("taxonomy", {})):
        for name, item in section.items():
            if item and is_placeholder(item.get("value")) and item.get("value") not in (None, ""):
                issues.append({"field": name, "severity": "blocking", "type": "placeholder_as_fact",
                               "message": f"Placeholder '{item.get('value')}' cannot be a canonical {name}."})
    incompatible = {
        "fragrance": {"skincare", "haircare", "makeup"},
        "makeup": {"skincare", "haircare", "fragrance"},
        "haircare": {"skincare", "makeup", "fragrance"},
        "skincare": {"haircare", "makeup", "fragrance"},
        "unknown": {"skincare", "haircare", "makeup", "fragrance"},
    }
    for leaked in incompatible.get(module, set()):
        if payload.get(leaked) not in (None, {}, []):
            issues.append({"field": leaked, "severity": "blocking", "type": "category_module_contradiction",
                           "message": f"{leaked.title()} attributes contradict authoritative {module} classification."})
    if understanding.get("identity_status") == "conflicting":
        issues.append({"field": "identity", "severity": "blocking", "type": "identity_conflict",
                       "message": "Exact identity evidence conflicts; enrichment requires review."})
    elif understanding.get("identity_status") == "unresolved":
        issues.append({"field": "identity", "severity": "warning", "type": "identity_unresolved",
                       "message": "Product identity is unresolved; only universal conservative enrichment is allowed."})
    return issues


def enforce_evidence_scope(payload: dict[str, Any], understanding: dict[str, Any],
                           *, raw_inci_present: bool = False) -> tuple[dict[str, Any], list[str]]:
    """Deterministically prevent weak-scope evidence becoming exact facts."""
    data, rejected = dict(payload or {}), []
    exact_identity = understanding.get("match_type") in {"exact_gtin", "exact_identity", "exact_product"}
    human_fields = {
        field for block in (understanding.get("identity", {}), understanding.get("taxonomy", {}))
        for field, item in block.items() if (item or {}).get("status") == "human_confirmed"
    }

    def direct(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        if str(item.get("value_status") or item.get("status") or item.get("source_status") or "").lower() in {
            "explicit_source", "source_supported", "verified", "human_confirmed",
        }:
            return True
        for evidence in item.get("evidence") or []:
            scope = str((evidence or {}).get("match_type") or (evidence or {}).get("evidence_type") or "").lower()
            if scope in {"exact_product", "exact_variant", "exact_gtin", "official", "explicit", "customer"}:
                return True
        return False

    # Exact ingredient/formulation content is never inherited from siblings or comparables.
    if not raw_inci_present and not exact_identity:
        if data.get("ingredients_intelligence"):
            data["ingredients_intelligence"] = []
            rejected.append("ingredients_intelligence:insufficient_scope")
        for module in ("skincare", "haircare"):
            block = data.get(module)
            if isinstance(block, dict) and block.get("key_ingredients"):
                data[module] = {**block, "key_ingredients": []}
                rejected.append(f"{module}.key_ingredients:insufficient_scope")
    # Variant-specific module facts require direct evidence, not a family/comparable prompt context.
    protected = {"makeup": ("shade_colour",), "fragrance": ("concentration",)}
    for module, fields in protected.items():
        block = data.get(module)
        if not isinstance(block, dict):
            continue
        for field in fields:
            value = block.get(field)
            if value not in (None, "", [], {}) and field not in human_fields and not direct(value):
                block[field] = None
                rejected.append(f"{module}.{field}:insufficient_scope")
    # Positive claims/testing/regulatory assertions require their own direct evidence.
    safe_claims = []
    for claim in data.get("claims") or []:
        affirmative = isinstance(claim, dict) and str(claim.get("value") or "").lower() in {"yes", "true", "verified", "1"}
        if affirmative and not direct(claim):
            safe_claims.append({**claim, "value": "Unknown", "status": "unverified", "confidence": 0.0})
            rejected.append(f"claim:{claim.get('name')}:insufficient_scope")
        else:
            safe_claims.append(claim)
    data["claims"] = safe_claims
    safe_warnings = []
    for warning in data.get("warnings_considerations") or []:
        warning_type = str(warning.get("type") or "").lower() if isinstance(warning, dict) else ""
        if warning_type in {"regulatory", "pregnancy", "clinical", "testing"} and not direct(warning):
            rejected.append(f"warning:{warning_type}:insufficient_scope")
            continue
        safe_warnings.append(warning)
    data["warnings_considerations"] = safe_warnings
    return data, rejected
