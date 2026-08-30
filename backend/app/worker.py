import time
import uuid
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import (
    ImportJob, ImportJobItem, SourceListing, CanonicalProduct, 
    ProductVariant, Brand, Category, FieldValue, Formulation,
    ValidationIssue,
    AuditLog, SourcePrice, EnrichmentRun, ScrapedProductObservation, CrawlJob
)
from app.services.deduplication import evaluate_match, normalize_text
from app.services.enrichment import run_ai_enrichment
from app.services.catalogue_knowledge import build_catalogue_knowledge_context
from app.config import settings

logger = logging.getLogger("worker")

CATEGORY_MODULE_FIELDS = {
    "skincare": ("skin_types", "texture", "finish", "key_ingredients"),
    "haircare": ("hair_types", "texture_format", "key_ingredients"),
    "makeup": ("shade_colour", "coverage", "finish", "texture_format"),
    "fragrance": (
        "concentration", "fragrance_family", "top_notes", "heart_notes",
        "base_notes", "longevity", "sillage_projection", "seasonal_fit",
        "occasion_fit",
    ),
}


def _structured_value_present(value: Any) -> bool:
    if value in (None, "", [], {}):
        return False
    return str(value).strip().lower() not in {
        "unknown", "not provided", "not_provided", "unverified", "null", "none",
    }


def structured_module_has_gaps(field_name: str, value: Any) -> bool:
    """Return true when a category block is present but only partly complete."""
    expected = CATEGORY_MODULE_FIELDS.get(field_name)
    if not expected or not isinstance(value, dict):
        return not _structured_value_present(value)
    return any(not _structured_value_present(value.get(name)) for name in expected)


def merge_structured_module(existing: Any, candidate: Any) -> Any:
    """Fill category-module gaps without replacing accepted existing facts."""
    if not isinstance(existing, dict) or not isinstance(candidate, dict):
        return candidate if not _structured_value_present(existing) else existing
    merged = dict(existing)
    for key, candidate_value in candidate.items():
        existing_value = merged.get(key)
        if key == "evidence" and isinstance(candidate_value, list):
            combined = list(existing_value or []) if isinstance(existing_value, list) else []
            for item in candidate_value:
                if item not in combined:
                    combined.append(item)
            merged[key] = combined
        elif isinstance(existing_value, dict) and isinstance(candidate_value, dict):
            merged[key] = merge_structured_module(existing_value, candidate_value)
        elif not _structured_value_present(existing_value) and _structured_value_present(candidate_value):
            merged[key] = candidate_value
    return merged


def queue_exact_formulation_research(db: Session, item: ImportJobItem, job: ImportJob) -> CrawlJob | None:
    """Queue targeted completion for a resolved product's valuable gaps.

    This is intentionally evidence- and gap-driven: exact local corpus data is
    applied first, then one durable research job receives only unresolved
    objectives. It never launches a generic second enrichment pass.
    """
    if settings.ENVIRONMENT == "testing":
        return None
    if not job.created_by_id or not (settings.OPENAI_API_KEY or settings.BRAVE_SEARCH_API_KEY):
        return None
    product = db.query(CanonicalProduct).filter(
        CanonicalProduct.id == item.canonical_product_id,
        CanonicalProduct.is_deleted == False,
    ).first()
    variant = db.query(ProductVariant).filter(
        ProductVariant.id == item.product_variant_id,
        ProductVariant.is_deleted == False,
    ).first()
    if not product or not variant or not variant.gtin:
        return None
    category = db.query(Category).filter(Category.id == product.category_id).first() if product.category_id else None
    from app.knowledge_corpus.retrieval import retrieve_corpus_evidence
    corpus = retrieve_corpus_evidence(
        db, gtin=variant.gtin or "", brand=product.brand.name if product.brand else "",
        product_name=product.product_name, size=f"{variant.size or ''} {variant.unit or ''}".strip(),
        category=category.path if category else "",
    )
    if corpus.get("match_level") != "exact_product":
        return None
    from app.services.product_improvement import product_improvement_summary
    quality = product_improvement_summary(db, product)
    objectives = [
        row.get("field") for row in (quality.get("research_objectives") or [])
        if row.get("field")
    ][:10]
    if not objectives:
        return None
    active = db.query(CrawlJob).filter(
        CrawlJob.domain == "product-research.internal",
        CrawlJob.status.in_(["queued", "discovering", "crawling", "parsing"]),
    ).all()
    if any(str((row.configuration or {}).get("research_product_id")) == str(product.id) for row in active):
        return None
    recent = db.query(CrawlJob).filter(
        CrawlJob.domain == "product-research.internal",
        CrawlJob.created_at >= datetime.utcnow() - timedelta(hours=24),
    ).all()
    if any(str((row.configuration or {}).get("research_product_id")) == str(product.id) for row in recent):
        return None
    research = CrawlJob(
        id=uuid.uuid4(), domain="product-research.internal", starting_urls=[],
        crawl_mode="single_url", status="queued", requested_by_id=job.created_by_id,
        configuration={
            "product_research_job": True, "research_product_id": str(product.id),
            "research_item_id": str(item.id), "requested_mode": "missing_only",
            "selected_fields": [], "research_objectives": objectives,
            "discovery": None, "result": None,
        },
    )
    db.add(research)
    return research

def source_value(raw_data: Dict[str, Any], mapping: Dict[str, str], field_name: str) -> str:
    """Return a clean mapped source value without leaking Python sentinel strings."""
    column = mapping.get(field_name)
    if not column:
        return ""
    value = raw_data.get(column)
    if value is None or str(value).strip().lower() in {"", "none", "nan", "null"}:
        return ""
    return str(value).strip()


def source_alias_value(raw_data: Dict[str, Any], *aliases: str) -> str:
    """Read common identity columns even when an optional field was not mapped."""
    normalized = {
        "".join(char for char in str(key).lower() if char.isalnum()): value
        for key, value in (raw_data or {}).items()
    }
    for alias in aliases:
        value = normalized.get("".join(char for char in alias.lower() if char.isalnum()))
        if value is not None and str(value).strip().lower() not in {"", "none", "nan", "null"}:
            return str(value).strip()
    return ""


def semantic_source_row(raw_data: Dict[str, Any]) -> Dict[str, Any]:
    """Expose every non-empty uploaded column to semantic enrichment, bounded."""
    def bounded(value: Any) -> Any:
        compact = compact_enrichment_value(value)
        return compact[:750] if isinstance(compact, str) else compact
    return {
        str(key): bounded(value)
        for key, value in (raw_data or {}).items()
        if value not in (None, "", [], {}) and str(value).strip().lower() not in {"nan", "null", "none"}
    }


def apply_exact_corpus_evidence(enrichment_result: Dict[str, Any], exact: Dict[str, Any], module: str) -> Dict[str, Any]:
    """Materialize unambiguous exact-source facts deterministically."""
    values, evidence = exact.get("values") or {}, exact.get("evidence") or {}

    def evidence_rows(field_name: str) -> List[Dict[str, Any]]:
        return [{
            "source_field": field_name,
            "supporting_text": str(row.get("raw_value") or row.get("value"))[:1000],
            "evidence_type": "exact_product_retail_data",
            "source_url": row.get("source_url"), "match_type": "exact_product",
        } for row in (evidence.get(field_name) or [])[:5]]

    for field_name in ("subcategory", "product_type", "application_area", "product_positioning", "sensory_description"):
        value = values.get(field_name)
        if value not in (None, "", [], {}) and field_name not in exact.get("conflicts", []):
            enrichment_result[field_name] = {
                "value": value, "value_status": "source_supported", "confidence": 0.98,
                "evidence": evidence_rows(field_name),
                "reasoning_summary": "Copied from unambiguous exact-product retail evidence.",
            }

    benefits = values.get("benefits")
    if benefits not in (None, "", [], {}) and "benefits" not in exact.get("conflicts", []):
        entries = benefits if isinstance(benefits, list) else [benefits]
        enrichment_result["benefits"] = [{
            "statement": str(entry), "source_type": "exact_product_retail_data",
            "confidence": 0.98, "evidence": str((evidence_rows("benefits") or [{}])[0].get("supporting_text") or entry),
        } for entry in entries if entry not in (None, "")]
    concerns = values.get("targeted_concerns")
    if concerns not in (None, "", [], {}) and "targeted_concerns" not in exact.get("conflicts", []):
        entries = concerns if isinstance(concerns, list) else [concerns]
        enrichment_result["targeted_concerns"] = {
            "values": [str(entry) for entry in entries if entry not in (None, "")],
            "value_status": "source_supported", "confidence": 0.98,
            "evidence": evidence_rows("targeted_concerns"),
            "reasoning_summary": "Copied from exact-product retail evidence.",
        }

    directions = values.get("directions") or values.get("usage_instructions")
    if directions not in (None, "", [], {}) and "directions" not in exact.get("conflicts", []):
        enrichment_result["directions"] = {
            "value": directions, "value_status": "source_supported", "confidence": 0.98,
            "evidence": evidence_rows("directions") or evidence_rows("usage_instructions"),
            "reasoning_summary": "Copied from exact-product retail evidence.",
        }

    module_map = {
        "skincare": ("skin_types", "texture", "finish", "key_ingredients"),
        "haircare": ("hair_types", "texture_format", "key_ingredients"),
        "makeup": ("shade_colour", "coverage", "finish", "texture_format"),
        "fragrance": ("concentration", "fragrance_family", "top_notes", "heart_notes", "base_notes",
                      "longevity", "sillage_projection", "seasonal_fit", "occasion_fit"),
    }
    block = dict(enrichment_result.get(module) or {}) if module in module_map else {}
    for field_name in module_map.get(module, ()):
        value = values.get(field_name)
        if value not in (None, "", [], {}) and field_name not in exact.get("conflicts", []):
            if field_name in {"texture", "texture_format", "finish", "coverage", "shade_colour"}:
                block[field_name] = {
                    "value": value, "value_status": "source_supported", "confidence": 0.98,
                    "evidence": evidence_rows(field_name),
                    "reasoning_summary": "Copied from exact-product retail evidence.",
                }
            elif field_name in {"skin_types", "hair_types"}:
                entries = value if isinstance(value, list) else [value]
                block[field_name] = {
                    "applicable": True, "recommended_for": [str(entry) for entry in entries],
                    "not_recommended_for": [], "unknown_for": [],
                    "evidence": evidence_rows(field_name), "confidence": 0.98,
                }
            elif field_name == "key_ingredients":
                entries = value if isinstance(value, list) else [value]
                block[field_name] = [{
                    "ingredient_name": str(entry), "inci_position": None,
                    "short_description": None, "functions": [], "benefits": [],
                    "possible_concerns": [], "is_key_ingredient": True,
                    "key_ingredient_status": "source_supported",
                } for entry in entries if entry not in (None, "")]
            else:
                block[field_name] = value
    if block:
        block["evidence"] = list(block.get("evidence") or []) + [
            row for field_name in module_map.get(module, ()) for row in evidence_rows(field_name)
        ]
        block["confidence"] = max(float(block.get("confidence") or 0), 0.98)
        enrichment_result[module] = block
    return enrichment_result


def normalize_gtin_value(value: Any) -> Optional[str]:
    """Normalize CSV/Excel identifiers without turning a decimal suffix into a digit."""
    import re
    text = str(value or "").strip()
    text = re.sub(r"\.0+$", "", text)
    digits = re.sub(r"\D", "", text)
    return digits if len(digits) in {8, 12, 13, 14} else None

def split_size_and_unit(size: Any, explicit_unit: Any = None) -> tuple[Optional[str], Optional[str]]:
    """Accept legacy separate units while parsing common combined values such as 100 ml."""
    import re
    text = str(size or "").strip()
    unit = str(explicit_unit or "").strip() or None
    if not text:
        return None, unit
    match = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(ml|cl|l|g|kg|oz|fl\.?\s*oz)\s*", text, re.I)
    if not match:
        return text, unit
    value = match.group(1).replace(",", ".")
    return value, unit or re.sub(r"\s+", " ", match.group(2).lower().replace(".", ""))


def inferred_category_name(product_type: str, application_area: str = "") -> str:
    text = f"{product_type} {application_area}".lower()
    category_rules = (
        ("Hair Care", ("shampoo", "conditioner", "hair", "scalp")),
        ("Makeup", ("lipstick", "mascara", "foundation", "concealer", "makeup")),
        ("Fragrance", ("fragrance", "perfume", "eau de", "parfum")),
        ("Body Care", ("body", "deodorant", "underarm")),
        ("Sun Care", ("sunscreen", "sun care", "spf")),
        ("Skin Care", ("cleanser", "serum", "moistur", "cream", "lotion", "toner", "mask", "face")),
    )
    for category, keywords in category_rules:
        if any(keyword in text for keyword in keywords):
            return category
    return "Beauty & Personal Care"


def compact_enrichment_value(value: Any, *, depth: int = 0) -> Any:
    """Bound prompt context without discarding attributable product facts."""
    if depth >= 3:
        return str(value)[:500]
    if isinstance(value, str):
        return value[:3000]
    if isinstance(value, list):
        return [compact_enrichment_value(item, depth=depth + 1) for item in value[:10]]
    if isinstance(value, dict):
        return {
            str(key): compact_enrichment_value(item, depth=depth + 1)
            for key, item in list(value.items())[:30]
            if item not in (None, "", [], {})
        }
    return value


def collect_structured_evidence(value: Any) -> list[dict[str, Any]]:
    """Promote evidence nested inside benefits/ingredients into field provenance."""
    collected: list[dict[str, Any]] = []

    def visit(item: Any):
        if isinstance(item, dict):
            evidence = item.get("evidence")
            if isinstance(evidence, str) and evidence.strip():
                collected.append({
                    "source_field": "structured_enrichment",
                    "supporting_text": evidence.strip(),
                    "evidence_type": "provider_summary",
                })
            elif isinstance(evidence, list):
                for entry in evidence:
                    if isinstance(entry, dict) and entry.get("supporting_text"):
                        collected.append(entry)
                    elif isinstance(entry, str) and entry.strip():
                        collected.append({
                            "source_field": "structured_enrichment",
                            "supporting_text": entry.strip(),
                            "evidence_type": "provider_summary",
                        })
            for key, child in item.items():
                if key != "evidence" and isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    unique = []
    seen = set()
    for item in collected:
        marker = (str(item.get("source_field")), str(item.get("supporting_text")))
        if marker not in seen:
            seen.add(marker)
            unique.append(item)
    return unique[:20]


def normalize_claim_value(value: Any) -> str:
    """Keep binary product claims consistent across model providers."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "y", "1"}:
        return "yes"
    if text in {"false", "no", "n", "0"}:
        return "no"
    return text or "unverified"


def apply_category_specific_enrichment(
    enrichment_result: Dict[str, Any], identity_text: str, raw_ingredients: str,
) -> Dict[str, Any]:
    """Enforce one applicable category module and fragrance-specific semantics."""
    text = identity_text.lower()
    if not any(token in text for token in ("perfume", "parfum", "fragrance", "eau de")):
        return enrichment_result
    enrichment_result["application_area"] = {
        "value": "Pulse points and skin", "value_status": "inferred", "confidence": 0.9,
        "evidence": [], "reasoning_summary": "Category-specific fragrance normalization.",
    }
    existing = enrichment_result.get("fragrance") or enrichment_result.get("fragrance_intelligence") or {}
    concentration = next((label for token, label in (
        ("eau de toilette", "Eau de Toilette"), ("eau de parfum", "Eau de Parfum"),
        ("extrait", "Extrait de Parfum"), ("parfum", "Parfum"), ("cologne", "Eau de Cologne"),
    ) if token in text), existing.get("concentration"))
    enrichment_result["fragrance"] = {
        "concentration": concentration, "fragrance_family": existing.get("fragrance_family"),
        "top_notes": existing.get("top_notes") or [], "heart_notes": existing.get("heart_notes") or existing.get("middle_notes") or [],
        "base_notes": existing.get("base_notes") or [], "longevity": existing.get("longevity") or existing.get("longevity_profile"),
        "sillage_projection": existing.get("sillage_projection"), "seasonal_fit": existing.get("seasonal_fit") or [],
        "occasion_fit": existing.get("occasion_fit") or [], "evidence": existing.get("evidence") or [],
        "confidence": existing.get("confidence") or 0.7,
    }
    enrichment_result.update(skincare=None, haircare=None, makeup=None)
    if not raw_ingredients.strip():
        warnings = enrichment_result.setdefault("warnings_considerations", [])
        warnings.append({"type": "other", "observation": "Ingredient list unavailable; ingredient-related considerations cannot be assessed.",
                         "evidence": [], "source_status": "unknown", "confidence": 1.0})
    return enrichment_result

def record_audit(
    db: Session,
    entity_type: str,
    entity_id: uuid.UUID,
    display_label: str,
    action: str,
    before: Dict[str, Any],
    after: Dict[str, Any],
    changed: Dict[str, Any],
    user_id: Optional[uuid.UUID] = None,
    actor_type: str = "system",
    reason: Optional[str] = None
):
    audit = AuditLog(
        id=uuid.uuid4(),
        entity_type=entity_type,
        entity_id=entity_id,
        entity_display_label=display_label[:255] if display_label else None,
        user_id=user_id,
        actor_type=actor_type,
        action=action, # create, update, merge, approve, reject (lowercase)
        before_snapshot=before,
        after_snapshot=after,
        changed_fields=changed,
        reason=reason
    )
    db.add(audit)
    db.flush()

def is_unknown_or_not_applicable(value: Any, status: Optional[str]) -> bool:
    val_str = str(value).strip().lower() if value is not None else ""
    status_str = str(status).strip().lower() if status is not None else ""
    return (
        val_str in ["", "unknown", "none", "nan", "null", "not_applicable"] or
        status_str in ["unknown", "none", "nan", "null", "not_applicable"]
    )

def is_conflicting(value: Any, status: Optional[str]) -> bool:
    val_str = str(value).strip().lower() if value is not None else ""
    status_str = str(status).strip().lower() if status is not None else ""
    return val_str == "conflicting" or status_str == "conflicting"

def should_create_low_confidence_warning(
    field_name: str,
    value: Any,
    status: Optional[str],
    source_type: str,
    confidence: Optional[float]
) -> bool:
    from app.config import settings
    if source_type == "source_data":
        return False
    if is_unknown_or_not_applicable(value, status):
        return False
    if is_conflicting(value, status):
        return False
    if field_name not in settings.LOW_CONFIDENCE_FIELDS:
        return False
    if confidence is not None and confidence < settings.LOW_CONFIDENCE_THRESHOLD:
        return True
    return False

def map_ai_status_to_db(ai_status: str) -> str:
    mapping = {
        "explicit_brand_claim": "confirmed",
        "explicit_retailer_claim": "confirmed",
        "ingredient_based_inference": "inferred",
        "text_based_inference": "inferred",
        "explicit_source": "confirmed",
        "normalized_source": "confirmed",
        "inferred": "inferred",
        "explicit": "confirmed",
        "not_targeted": "confirmed",
        "confirmed": "confirmed",
        "conflicting": "conflicting",
        "unknown": "unknown",
        "not_applicable": "not_applicable"
    }
    return mapping.get(ai_status, "unknown")

def create_field_value_version(
    db: Session,
    canonical_product_id: Optional[uuid.UUID],
    product_variant_id: Optional[uuid.UUID],
    field_name: str,
    value: Any,
    source_type: str,
    source_ref: str,
    confidence: float,
    status: str,
    run_id: Optional[uuid.UUID] = None,
    evidence: Optional[list] = None,
    reasoning_summary: Optional[str] = None,
    semantic_status: Optional[str] = None,
    semantic_status_type: Optional[str] = None
):
    """Save a candidate without allowing AI to replace accepted direct evidence."""
    db_status = map_ai_status_to_db(status)
    def meaningful(candidate: Any) -> bool:
        if candidate in (None, "", [], {}):
            return False
        return str(candidate).strip().lower() not in {
            "unknown", "not found", "not_found", "not provided", "not_provided",
            "none", "null", "unverified",
        }

    current_query = db.query(FieldValue).filter(
        FieldValue.field_name == field_name,
        FieldValue.is_current == True,
    )
    if canonical_product_id:
        current_query = current_query.filter(FieldValue.canonical_product_id == canonical_product_id)
    elif product_variant_id:
        current_query = current_query.filter(FieldValue.product_variant_id == product_variant_id)
    current_value = current_query.first()
    # A failed/empty research result is evidence about the run, not a command
    # to erase a valid product value.  Improve Product is additive/corrective.
    if current_value and meaningful(current_value.value) and not meaningful(value):
        return current_value

    protected_current = None
    # A new explicit human edit supersedes an older human edit. Automatic
    # sources remain unable to replace protected human evidence.
    protected_sources = [] if source_type == "human_edit" else ["human_edit"]
    if source_type == "ai_inference":
        protected_sources.append("source_data")
    if protected_sources and canonical_product_id:
        protected_current = db.query(FieldValue).filter(
            FieldValue.canonical_product_id == canonical_product_id,
            FieldValue.field_name == field_name,
            FieldValue.source_type.in_(protected_sources),
            FieldValue.review_status == "confirmed",
            FieldValue.is_current == True
        ).first()
    elif protected_sources and product_variant_id:
        protected_current = db.query(FieldValue).filter(
            FieldValue.product_variant_id == product_variant_id,
            FieldValue.field_name == field_name,
            FieldValue.source_type.in_(protected_sources),
            FieldValue.review_status == "confirmed",
            FieldValue.is_current == True
        ).first()

    # Confirmed product facts are immutable through ordinary automated
    # enrichment.  A disagreeing automatic value is retained as a reviewable
    # candidate; explicit human reconciliation remains the only overwrite path.
    if (not protected_current and current_value and source_type != "human_edit"
            and field_name not in {"product_understanding", "schema_org"}
            and current_value.review_status == "confirmed" and meaningful(current_value.value)):
        protected_current = current_value

    # Keep accepted human/direct-source evidence active; retain AI as a reviewable
    # candidate when it disagrees.
    is_current = True
    if protected_current:
        is_current = False
        if protected_current.value != value:
            msg = (
                f"Enrichment produced conflicting candidate value '{value}' for "
                f"field '{field_name}' (accepted value: "
                f"'{protected_current.value}', source: "
                f"{protected_current.source_type})."
            )
            issue = ValidationIssue(
                id=uuid.uuid4(),
                canonical_product_id=canonical_product_id,
                field_name=field_name,
                severity="warning",
                issue_type="conflicting_information",
                message=msg,
                created_by_type="system"
            )
            db.add(issue)

    # Set existing current active values to False if writing new current active
    if is_current:
        if canonical_product_id:
            db.query(FieldValue).filter(
                FieldValue.canonical_product_id == canonical_product_id,
                FieldValue.field_name == field_name
            ).update({"is_current": False}, synchronize_session=False)
        elif product_variant_id:
            db.query(FieldValue).filter(
                FieldValue.product_variant_id == product_variant_id,
                FieldValue.field_name == field_name
            ).update({"is_current": False}, synchronize_session=False)
        # Execute the retirement before the replacement INSERT. In particular,
        # PostgreSQL must never see two rows satisfying the partial unique
        # current-value index during a multi-field identity transaction.
        db.flush()
            
    field_record = FieldValue(
        id=uuid.uuid4(),
        canonical_product_id=canonical_product_id,
        product_variant_id=product_variant_id,
        field_name=field_name,
        value=value,
        source_type=source_type,
        source_reference=source_ref,
        confidence_score=confidence,
        review_status=db_status,
        enrichment_run_id=run_id,
        is_current=is_current,
        override_reason=None,
        evidence=evidence,
        reasoning_summary=reasoning_summary,
        semantic_status=semantic_status,
        semantic_status_type=semantic_status_type
    )
    db.add(field_record)

def process_item_enrichment(
    db: Session,
    item: ImportJobItem,
    mapping: Dict[str, str],
    *,
    mode: str = "full",
    selected_fields: Optional[List[str]] = None,
):
    """Runs AI/rule enrichment on a matched canonical product and variant.
    """
    if mode not in {"full", "missing_only", "selected"}:
        raise ValueError(f"Unsupported enrichment mode: {mode}")
    selected = set(selected_fields or [])
    if mode == "selected" and not selected:
        raise ValueError("Select at least one field for selective enrichment")

    current_values = {
        row.field_name: row
        for row in db.query(FieldValue).filter(
            FieldValue.canonical_product_id == item.canonical_product_id,
            FieldValue.is_current == True,
        ).all()
    }

    def should_write(field_name: str) -> bool:
        if mode == "selected":
            return field_name in selected
        if mode == "missing_only":
            existing = current_values.get(field_name)
            if not existing:
                return True
            value = existing.value
            if field_name in CATEGORY_MODULE_FIELDS:
                return structured_module_has_gaps(field_name, value)
            if value in (None, "", [], {}):
                return True
            return str(value).strip().lower() in {
                "unknown", "not provided", "not_provided", "unverified", "null", "none"
            }
        return True

    listing = db.query(SourceListing).filter(SourceListing.id == item.source_listing_id).first()
    if not listing:
        raise ValueError("Source listing not found")

    raw_data = listing.raw_data
    raw_name = source_value(raw_data, mapping, "product_name")
    raw_brand = source_value(raw_data, mapping, "brand")
    raw_desc = source_value(raw_data, mapping, "description") or source_alias_value(
        raw_data, "description", "product description", "long description", "marketing description",
        "marketing copy", "product information", "prod info descript",
    )
    raw_ingr = source_value(raw_data, mapping, "ingredients") or source_alias_value(
        raw_data, "ingredients", "ingredient list", "ingredients list", "inci", "inci list", "composition",
    )
    raw_ean = normalize_gtin_value(source_value(raw_data, mapping, "ean"))
    raw_size, raw_unit = split_size_and_unit(
        source_value(raw_data, mapping, "size"), source_value(raw_data, mapping, "unit")
    )
    raw_category = source_value(raw_data, mapping, "category")
    raw_product_family = source_value(raw_data, mapping, "product_family")
    raw_product_type = source_value(raw_data, mapping, "product_type") or source_alias_value(
        raw_data, "product_type", "product type", "type", "format", "concentration",
    )
    raw_claims = source_value(raw_data, mapping, "claims") or source_alias_value(
        raw_data, "claims", "product claims", "marketing claims", "features",
    )
    raw_directions = source_value(raw_data, mapping, "directions") or source_alias_value(
        raw_data, "directions", "usage instructions", "how to use", "application",
    )
    raw_market = source_value(raw_data, mapping, "market") or "global"
    raw_language = source_value(raw_data, mapping, "language") or "en"
    raw_image_url = source_value(raw_data, mapping, "image_url") or source_alias_value(
        raw_data, "image url", "image", "main image", "product image", "image link",
    ) or None

    # Exact-product web observations outrank a sparse import row. They supply
    # attributable copy, INCI and imagery to the enrichment prompt.
    research_rows = db.query(ScrapedProductObservation).filter(
        ScrapedProductObservation.canonical_product_id == item.canonical_product_id,
    ).order_by(ScrapedProductObservation.scraped_at.desc()).limit(10).all()
    research_payloads = [row.normalized_payload or {} for row in research_rows]

    def first_researched(*keys):
        for payload in research_payloads:
            for key in keys:
                value = payload.get(key)
                if value not in (None, "", [], {}):
                    return value
        return None

    raw_desc = raw_desc or str(first_researched("description", "subtitle") or "")
    raw_ingr = raw_ingr or str(first_researched("ingredient_text_raw") or "")
    raw_ean = raw_ean or normalize_gtin_value(first_researched("gtin", "ean", "upc"))
    raw_size = raw_size or first_researched("size")
    researched_claims = first_researched("claims") or []
    raw_claims = raw_claims or "; ".join(researched_claims)
    raw_directions = raw_directions or str(first_researched("usage_instructions") or "")
    if not raw_image_url:
        researched_images = first_researched("image_urls") or []
        raw_image_url = researched_images[0] if researched_images else None

    if raw_image_url:
        from app.services.image_urls import normalize_public_image_url
        normalized_image_url = normalize_public_image_url(raw_image_url)
        if normalized_image_url:
            product = db.query(CanonicalProduct).filter(
                CanonicalProduct.id == item.canonical_product_id
            ).first()
            if product:
                product.image_url = normalized_image_url

    # Old enrichment runs could create blank formulation shells. They are not
    # evidence and must not appear in the product page or PDF.
    for formulation in db.query(Formulation).filter(
        Formulation.canonical_product_id == item.canonical_product_id,
        Formulation.is_deleted == False,
    ).all():
        if not (formulation.raw_inci_text or "").strip():
            formulation.is_deleted = True

    variant = db.query(ProductVariant).filter(ProductVariant.id == item.product_variant_id).first()
    if variant:
        if raw_ean and not variant.gtin:
            duplicate = db.query(ProductVariant).filter(
                ProductVariant.gtin == str(raw_ean), ProductVariant.id != variant.id,
            ).first()
            if not duplicate:
                variant.gtin = str(raw_ean)
        if raw_size and not variant.size:
            variant.size = str(raw_size)
        if raw_unit and not variant.unit:
            variant.unit = raw_unit
        researched_unit = first_researched("unit")
        if researched_unit and not variant.unit:
            variant.unit = str(researched_unit)
        researched_variant = first_researched("variant_name", "shade")
        if researched_variant and not variant.variant_name:
            variant.variant_name = str(researched_variant)

    # Resolve semantics before the LLM sees this row.  Exact GTIN evidence may
    # correct a supplier/legal entity, shorthand taxonomy, family or variant;
    # weak source labels such as "STD" are never promoted to product facts.
    product = db.query(CanonicalProduct).filter(CanonicalProduct.id == item.canonical_product_id).first()
    from app.services.product_understanding import (
        is_placeholder, resolve_product_understanding, understanding_snapshot_values,
    )
    product_understanding = resolve_product_understanding(
        db, raw_data=raw_data, mapping=mapping, product=product, variant=variant,
    )
    understood = understanding_snapshot_values(product_understanding)
    if product and product_understanding.get("identity_status") == "resolved":
        human_identity = db.query(FieldValue).filter(
            FieldValue.canonical_product_id == product.id,
            FieldValue.field_name.in_(["brand", "product_name"]),
            FieldValue.source_type == "human_edit", FieldValue.is_current == True,
        ).first()
        if not human_identity:
            before_identity = {"brand": product.brand.name if product.brand else None, "product_name": product.product_name}
            resolved_brand, resolved_name = understood.get("brand"), understood.get("product_name")
            # Ordinary enrichment may fill a placeholder identity, but must
            # never replace a resolved canonical identity with a localized or
            # retailer-specific title.  Such values remain source evidence.
            if resolved_brand and is_placeholder(before_identity["brand"]):
                normalized_brand_name = normalize_text(resolved_brand)
                brand_record = db.query(Brand).filter(Brand.normalized_name == normalized_brand_name).first()
                if not brand_record:
                    brand_record = Brand(id=uuid.uuid4(), name=resolved_brand, normalized_name=normalized_brand_name)
                    db.add(brand_record)
                    db.flush()
                product.brand_id = brand_record.id
            if resolved_name and is_placeholder(product.product_name):
                product.product_name = resolved_name
                product.normalized_name = normalize_text(resolved_name)
            after_identity = {"brand": product.brand.name if product.brand else before_identity["brand"], "product_name": product.product_name}
            if before_identity != after_identity:
                db.add(AuditLog(
                    id=uuid.uuid4(), entity_type="canonical_product", entity_id=product.id,
                    entity_display_label=resolved_name, actor_type="rule", action="update",
                    before_snapshot=before_identity, after_snapshot=after_identity,
                    changed_fields=["brand", "product_name"],
                    reason="Exact-product evidence resolved source semantic roles before enrichment.",
                ))
    raw_brand = understood.get("brand") or raw_brand
    raw_name = understood.get("product_name") or raw_name
    raw_category = understood.get("category") or raw_category
    if not is_placeholder(understood.get("product_type")):
        raw_product_type = understood["product_type"]

    # Retrieve the indexed corpus once and resolve exact, conflict-free values
    # before the prompt. Customer-uploaded facts remain first priority.
    from app.knowledge_corpus.retrieval import retrieve_corpus_evidence, resolve_exact_field_evidence
    corpus_result = retrieve_corpus_evidence(
        db, gtin=raw_ean or "", brand=raw_brand, product_name=raw_name,
        size=f"{raw_size or ''} {raw_unit or ''}".strip(), category=raw_category,
    )
    exact_corpus = resolve_exact_field_evidence(corpus_result)
    exact_values = exact_corpus.get("values") or {}
    exact_applied_fields = set(exact_values) - set(exact_corpus.get("conflicts") or [])
    exact_formulation = exact_corpus.get("formulation") or {}
    exact_market = exact_corpus.get("market") or {}
    raw_desc = raw_desc or str(exact_values.get("description") or "")
    raw_ingr = raw_ingr or str(exact_formulation.get("raw_inci_text") or exact_values.get("ingredient_text_raw") or "")
    # Canonical formulation persistence is exclusively evidence-first.  The
    # enrichment model may interpret an accepted formulation but can never
    # create or replace it from generated ingredient output.
    from app.services.formulation_resolution import (
        promote_exact_corpus_formulation, synchronize_current_source_formulation,
    )
    synchronize_current_source_formulation(db, product, variant)
    promote_exact_corpus_formulation(db, product, variant, corpus_result)
    corpus_claims = exact_values.get("claims")
    if not raw_claims and corpus_claims:
        raw_claims = "; ".join(str(value) for value in corpus_claims) if isinstance(corpus_claims, list) else str(corpus_claims)
    raw_directions = raw_directions or str(exact_values.get("directions") or exact_values.get("usage_instructions") or "")
    raw_image_url = raw_image_url or exact_market.get("image_url")
    if raw_image_url and product:
        from app.services.image_urls import normalize_public_image_url
        product.image_url = normalize_public_image_url(raw_image_url) or product.image_url

    # Start Enrichment Run
    item.enrichment_status = "processing"
    item.started_at = datetime.utcnow()
    db.commit()

    # Ground enrichment with attributable observations already matched to this
    # exact canonical product. Similar-but-unmatched products are never included.
    enrichment_source_context = {
        "imported_product": {
            "gtin": raw_ean, "size": raw_size,
            "category": raw_category, "product_family": raw_product_family,
            "product_type": raw_product_type,
            "claims": raw_claims, "directions": raw_directions,
            "market": raw_market, "language": raw_language, "image_url": raw_image_url,
        },
        "_beautypim_product_understanding": product_understanding,
        "_complete_original_source_row": semantic_source_row(raw_data),
        "_exact_corpus_resolution": compact_enrichment_value(exact_corpus),
    }
    from app.services.category_completeness import build_gap_plan
    gap_snapshot = {
        "brand": raw_brand, "product_name": raw_name, "gtin": raw_ean,
        "size": f"{raw_size or ''} {raw_unit or ''}".strip(), "category": raw_category,
        "product_type": raw_product_type, "description": raw_desc, "image_url": raw_image_url,
        "inci": raw_ingr,
    }
    gap_snapshot.update({key: value for key, value in understood.items() if value not in (None, "")})
    gap_metadata = {}
    for field_name, row in current_values.items():
        gap_snapshot[field_name] = row.value
        gap_metadata[field_name] = {
            "source_type": row.source_type, "semantic_status": row.semantic_status,
            "evidence": row.evidence or [], "researched": bool(row.enrichment_run_id),
        }
    gap_plan = build_gap_plan(gap_snapshot, gap_metadata)
    enrichment_source_context["_beautypim_gap_plan"] = gap_plan
    identity_only = gap_plan.get("phase") == "identity_resolution"
    if research_payloads:
        enrichment_source_context["_exact_product_web_observations"] = [
            compact_enrichment_value(payload) for payload in research_payloads[:5]
        ]
    catalogue_context = build_catalogue_knowledge_context(
        db, item.canonical_product_id,
        product_name=raw_name,
        brand=raw_brand,
        gtin=raw_ean or "",
        category=raw_category,
        product_family=raw_product_family,
        description=raw_desc,
    )
    if catalogue_context:
        enrichment_source_context["_beautypim_catalogue_knowledge"] = compact_enrichment_value(catalogue_context)

    # Trigger LLM/Rule Engine
    if identity_only:
        # The dependency is enforced here, not merely expressed in the prompt.
        # No category/commercial synthesis occurs until research has produced a
        # new authoritative Product Understanding contract.
        enrichment_result, run_id = {}, None
    else:
        enrichment_result, run_id = run_ai_enrichment(
            db=db,
            name=raw_name,
            brand=raw_brand,
            description=raw_desc,
            raw_ingredients=raw_ingr,
            import_job_id=item.import_job_id,
            import_job_item_id=item.id,
            source_listing_id=listing.id,
            canonical_product_id=item.canonical_product_id,
            product_variant_id=item.product_variant_id,
            source_context=enrichment_source_context,
        )
    if run_id:
        db.query(EnrichmentRun).filter(EnrichmentRun.id == run_id).update({
            "requested_fields": {
                "mode": mode,
                "fields": sorted(selected) if selected else [],
            }
        })

    source_ref = f"source_listing_id:{listing.id}"

    # Explicit source hierarchy and instructions outrank model inference.
    explicit_classification = understood.get("product_type")
    if not identity_only and explicit_classification and not is_placeholder(explicit_classification):
        explicit_family = explicit_classification.strip()
        for field_name in ("product_type",):
            enrichment_result[field_name] = {
                "value": explicit_family,
                "value_status": "explicit_source",
                "confidence": 1.0,
                "evidence": [{
                    "source_field": "product_understanding",
                    "supporting_text": explicit_family,
                    "evidence_type": "explicit",
                }],
                "reasoning_summary": "Resolved by the product-understanding service before enrichment.",
            }
    for field_name in (() if identity_only else ("subcategory", "application_area")):
        resolved_value = understood.get(field_name)
        if resolved_value and not is_placeholder(resolved_value):
            enrichment_result[field_name] = {
                "value": resolved_value, "value_status": "normalized_source",
                "confidence": float(product_understanding.get("taxonomy", {}).get(field_name, {}).get("confidence") or .8),
                "evidence": product_understanding.get("taxonomy", {}).get(field_name, {}).get("evidence") or [],
                "reasoning_summary": "Resolved by the product-understanding service before enrichment.",
            }
    if raw_directions and not identity_only:
        enrichment_result["directions"] = {
            "value": raw_directions,
            "value_status": "explicit_source",
            "confidence": 1.0,
            "evidence": [{
                "source_field": mapping.get("directions", "directions"),
                "supporting_text": raw_directions,
                "evidence_type": "explicit",
            }],
            "reasoning_summary": "Copied from mapped source directions.",
        }
    if raw_claims and not identity_only:
        source_claim_entries = [
            {
                "statement": claim.strip(),
                "benefit_status": "explicit_source",
                "confidence": 1.0,
                "evidence": [{
                    "source_field": mapping.get("claims", "claims"),
                    "supporting_text": claim.strip(),
                    "evidence_type": "explicit",
                }],
            }
            for claim in raw_claims.replace("|", ";").split(";")
            if claim.strip()
        ]
        enrichment_result["source_claims"] = source_claim_entries
        structured_claims = enrichment_result.get("claims") if isinstance(enrichment_result.get("claims"), list) else []
        for claim in source_claim_entries:
            structured_claims.append({
                "name": claim["statement"], "value": "Yes", "status": "source_supported",
                "evidence": claim["evidence"], "reasoning_summary": "Explicitly supplied in the company feed.",
                "confidence": 1.0,
            })
        enrichment_result["claims"] = structured_claims
        existing_benefits = enrichment_result.get("benefits")
        if not isinstance(existing_benefits, list):
            existing_benefits = []
        enrichment_result["benefits"] = source_claim_entries + existing_benefits

    # The authoritative module gates generation. Irrelevant category modules
    # are stripped rather than leaked into product detail, PDF or exports.
    authoritative_module = product_understanding.get("category_module", "unknown")
    for module_name in CATEGORY_MODULE_FIELDS:
        if module_name != authoritative_module:
            enrichment_result.pop(module_name, None)
    model_product_type = (enrichment_result.get('product_type') or {}).get('value', '')
    product_identity_text = f"{raw_name} {raw_category} {raw_product_family} " \
        f"__model_type__ {model_product_type}".lower()
    if not identity_only:
        enrichment_result = apply_category_specific_enrichment(
            enrichment_result, product_identity_text, raw_ingr,
        )
        enrichment_result = apply_exact_corpus_evidence(
            enrichment_result, exact_corpus, authoritative_module,
        )
    for module_name in CATEGORY_MODULE_FIELDS:
        if module_name != authoritative_module:
            enrichment_result.pop(module_name, None)
    from app.services.category_completeness import quality_gate
    enrichment_result, quality_rejections = quality_gate(
        enrichment_result, authoritative_module, product_identity_text,
    )
    from app.services.product_understanding import enforce_evidence_scope
    enrichment_result, scope_rejections = enforce_evidence_scope(
        enrichment_result, product_understanding, raw_inci_present=bool(raw_ingr),
    )
    # Product-facing ingredient identity/intelligence is derived exclusively
    # from the accepted canonical formulation and trusted glossary. Generated
    # structures may assist the model internally but are never canonical facts.
    enrichment_result.pop("ingredients_intelligence", None)
    for module_name in ("skincare", "haircare"):
        module_block = enrichment_result.get(module_name)
        if isinstance(module_block, dict):
            module_block.pop("key_ingredients", None)
    quality_rejections.extend(scope_rejections)
    if quality_rejections:
        enrichment_result["_quality_rejections"] = quality_rejections
    from app.services.enrichment import consolidate_enrichment_payload
    enrichment_result = consolidate_enrichment_payload(enrichment_result)

    # Persist the versioned decision contract independently of individual
    # enriched attributes so every downstream consumer can use one decision.
    # Product Understanding is foundational state, not an optional enrichment
    # field.  A missing-only run must refresh it whenever identity/taxonomy
    # evidence changes; otherwise downstream completeness and UI surfaces keep
    # consuming a stale contract even though the canonical category was just
    # corrected.  Avoid needless versions when the contract is unchanged.
    from app.services.product_understanding import understanding_contract_changed
    current_understanding = current_values.get("product_understanding")
    understanding_changed = understanding_contract_changed(
        current_understanding.value if current_understanding else None,
        product_understanding,
    )
    if understanding_changed:
        create_field_value_version(
            db=db, canonical_product_id=item.canonical_product_id, product_variant_id=None,
            field_name="product_understanding", value=product_understanding,
            source_type="deterministic_rule", source_ref=source_ref,
            confidence=float(product_understanding.get("confidence") or 0.0),
            status="conflicting" if product_understanding.get("identity_status") == "conflicting" else
                "confirmed" if product_understanding.get("identity_status") == "resolved" else "inferred",
            run_id=run_id, evidence=[], reasoning_summary="Authoritative pre-enrichment identity and taxonomy decision.",
            semantic_status=product_understanding.get("identity_status"),
            semantic_status_type="product_understanding",
        )
    from app.services.product_identity import product_version_label
    raw_identity_text = f"{raw_name} {raw_category} {raw_product_family} {raw_product_type}"
    if not identity_only and any(token in raw_identity_text.lower() for token in ("perfume", "parfum", "fragrance", "eau de")) \
            and not product_version_label(raw_identity_text):
        enrichment_result["product_type"] = {
            "value": "Perfume (concentration not specified)",
            "value_status": "explicit_source", "confidence": 1.0, "evidence": [],
            "reasoning_summary": "The source identifies a perfume but does not specify EDT, EDP, Parfum or Elixir.",
        }

    # Materialize a useful category even when the incoming feed has no taxonomy.
    # Explicit source hierarchy wins; otherwise transparent AI classification is used.
    inferred_product_type = str((enrichment_result.get("product_type") or {}).get("value") or "")
    inferred_application_area = str((enrichment_result.get("application_area") or {}).get("value") or "")
    root_name = (
        " ".join(raw_category.split()).title()
        if raw_category
        else inferred_category_name(inferred_product_type, inferred_application_area)
    )
    if authoritative_module == "unknown" or identity_only:
        root_name = ""
    if root_name:
        root = db.query(Category).filter(Category.path.ilike(root_name)).first()
        if not root:
            root = Category(id=uuid.uuid4(), name=root_name, level=0, path=root_name)
            db.add(root)
            db.flush()
        family_value = (
            understood.get("subcategory")
            or (enrichment_result.get("subcategory") or {}).get("value")
            or inferred_product_type
        )
        assigned = root
        if family_value:
            family_name = " ".join(str(family_value).replace("_", " ").split()).title()
            family_path = f"{root.path} > {family_name}"
            assigned = db.query(Category).filter(Category.path.ilike(family_path)).first()
            if not assigned:
                assigned = Category(
                    id=uuid.uuid4(), name=family_name, parent_id=root.id,
                    level=1, path=family_path,
                )
                db.add(assigned)
                db.flush()
        product = db.query(CanonicalProduct).filter(CanonicalProduct.id == item.canonical_product_id).first()
        if product and product.category_id is None:
            product.category_id = assigned.id

    # Write core enriched fields
    core_categorical_fields = [
        "subcategory", "product_type", "application_area", "target_audience",
        "product_positioning", "sensory_description", "routine_time", "routine_step"
    ]
    for field in core_categorical_fields:
        if not should_write(field):
            continue
        field_data = enrichment_result.get(field)
        # Category-aware enrichment deliberately returns null for fields that
        # do not apply (for example routine timing on a fragrance).  Treat
        # those as absent instead of trying to persist them as mappings.
        if not isinstance(field_data, dict):
            continue
        status = field_data.get("value_status", "unknown")
        create_field_value_version(
            db=db,
            canonical_product_id=item.canonical_product_id,
            product_variant_id=None,
            field_name=field,
            value=field_data.get("value"),
            source_type="source_data" if status in {"explicit_source", "normalized_source", "source_supported"} else "ai_inference",
            source_ref=source_ref,
            confidence=field_data.get("confidence", 0.0),
            status=status,
            run_id=run_id,
            evidence=field_data.get("evidence", []),
            reasoning_summary=field_data.get("reasoning_summary"),
            semantic_status=status,
            semantic_status_type="value_status"
        )

    # Persist rich enrichment blocks that were previously discarded.
    for field in [
        "source_claims", "benefits", "directions",
        "targeted_concerns", "claims", "warnings_considerations",
        "skincare", "haircare", "makeup", "fragrance", "ingredients_intelligence",
    ]:
        if not should_write(field):
            continue
        field_data = enrichment_result.get(field)
        if field_data is None:
            continue
        if field in CATEGORY_MODULE_FIELDS and isinstance(field_data, dict):
            existing = current_values.get(field)
            if existing and isinstance(existing.value, dict):
                field_data = merge_structured_module(existing.value, field_data)
                if field_data == existing.value:
                    continue
        confidence = 0.0
        if isinstance(field_data, dict):
            confidence = field_data.get("confidence") or 0.0
        elif isinstance(field_data, list) and field_data:
            confidence = max((entry.get("confidence", 0.0) for entry in field_data if isinstance(entry, dict)), default=0.0)
        create_field_value_version(
            db=db,
            canonical_product_id=item.canonical_product_id,
            product_variant_id=None,
            field_name=field,
            value=field_data,
            source_type="source_data" if (
                field in exact_applied_fields
                or field in CATEGORY_MODULE_FIELDS and bool(set(CATEGORY_MODULE_FIELDS[field]) & exact_applied_fields)
            ) else "ai_inference",
            source_ref=source_ref,
            confidence=confidence,
            status="inferred",
            run_id=run_id,
            evidence=collect_structured_evidence(field_data),
            reasoning_summary=f"Structured {field.replace('_', ' ')} enrichment.",
            semantic_status="inferred",
            semantic_status_type="structured_enrichment",
        )

    schema_org_value = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": raw_name,
        "brand": {"@type": "Brand", "name": raw_brand},
        "description": raw_desc,
        "gtin": raw_ean,
        "category": (enrichment_result.get("subcategory") or {}).get("value"),
        "size": raw_size,
    }
    if should_write("schema_org"):
        create_field_value_version(
            db=db, canonical_product_id=item.canonical_product_id, product_variant_id=None,
            field_name="schema_org", value=schema_org_value, source_type="deterministic_rule",
            source_ref=source_ref, confidence=1.0, status="confirmed", run_id=run_id,
            evidence=[], reasoning_summary="Generated deterministically from current catalogue values.",
            semantic_status="confirmed", semantic_status_type="structured_data",
        )

    # No formulation or glossary writes are permitted here.  Ordered
    # ingredients are materialized only by formulation_resolution.py from
    # accepted human/customer/corpus/web evidence.

    # Validation Checks
    # Clean/delete existing system validation issues for this item
    if item.canonical_product_id:
        db.query(ValidationIssue).filter(
            ValidationIssue.canonical_product_id == item.canonical_product_id,
            ValidationIssue.created_by_type == "system"
        ).delete()
    if item.product_variant_id:
        db.query(ValidationIssue).filter(
            ValidationIssue.product_variant_id == item.product_variant_id,
            ValidationIssue.created_by_type == "system"
        ).delete()

    from app.services.product_understanding import semantic_issues
    for semantic_issue in semantic_issues(product_understanding, enrichment_result):
        db.add(ValidationIssue(
            id=uuid.uuid4(), canonical_product_id=item.canonical_product_id,
            field_name=semantic_issue["field"], severity=semantic_issue["severity"],
            issue_type=semantic_issue["type"], message=semantic_issue["message"],
            created_by_type="system",
        ))

    from app.services.deduplication import normalize_volume
    import re

    variant = db.query(ProductVariant).filter(ProductVariant.id == item.product_variant_id).first()
    
    # 1. EAN missing warning
    if variant and not variant.gtin:
        issue = ValidationIssue(
            id=uuid.uuid4(),
            product_variant_id=item.product_variant_id,
            field_name="gtin",
            severity="warning",
            issue_type="missing_ean",
            message="Variant has no GTIN/EAN code.",
            created_by_type="system"
        )
        db.add(issue)
        
    # 2. Invalid GTIN
    if variant and variant.gtin:
        clean_gtin = variant.gtin.strip()
        if not (clean_gtin.isdigit() and len(clean_gtin) in [8, 12, 13, 14]):
            severity = "blocking" if settings.GTIN_MANDATORY else "warning"
            issue = ValidationIssue(
                id=uuid.uuid4(),
                product_variant_id=item.product_variant_id,
                field_name="gtin",
                severity=severity,
                issue_type="invalid_gtin",
                message=f"GTIN/EAN '{variant.gtin}' is invalid. Must be 8, 12, 13, or 14 digits.",
                created_by_type="system"
            )
            db.add(issue)

    # 3. Invalid URL
    raw_url = raw_data.get(mapping.get("product_url", ""))
    if raw_url:
        url_str = str(raw_url).strip()
        if url_str and not (url_str.startswith("http://") or url_str.startswith("https://")):
            issue = ValidationIssue(
                id=uuid.uuid4(),
                canonical_product_id=item.canonical_product_id,
                field_name="product_url",
                severity="warning",
                issue_type="invalid_url",
                message=f"Product URL '{raw_url}' is invalid.",
                created_by_type="system"
            )
            db.add(issue)

    # 4. Invalid price
    raw_price = raw_data.get(mapping.get("price", ""))
    if raw_price is not None:
        price_str = str(raw_price).strip()
        if price_str:
            try:
                price_val = float(price_str)
                if price_val <= 0:
                    raise ValueError()
            except ValueError:
                issue = ValidationIssue(
                    id=uuid.uuid4(),
                    canonical_product_id=item.canonical_product_id,
                    field_name="price",
                    severity="warning",
                    issue_type="invalid_price",
                    message=f"Price '{raw_price}' is invalid. Must be a positive number.",
                    created_by_type="system"
                )
                db.add(issue)

    # 5. Invalid size
    if raw_size:
        size_val = normalize_volume(raw_size)
        if size_val is None:
            issue = ValidationIssue(
                id=uuid.uuid4(),
                product_variant_id=item.product_variant_id,
                field_name="size",
                severity="warning",
                issue_type="invalid_size",
                message=f"Size/volume '{raw_size}' is invalid or cannot be parsed.",
                created_by_type="system"
            )
            db.add(issue)

    # 6. Fragrance-free claims vs Parfum in ingredients
    claims_str = str(raw_data.get("claims", "")).lower()
    desc_str = str(raw_data.get("description", "")).lower()
    ing_str = str(raw_data.get("ingredients", "")).lower()
    
    is_fragrance_free = "fragrance-free" in claims_str or "fragrance-free" in desc_str or "fragrance free" in claims_str or "fragrance free" in desc_str
    frag_pres = enrichment_result.get("fragrance_present", {})
    if isinstance(frag_pres, dict) and frag_pres.get("value") == "no":
        is_fragrance_free = True
        
    if is_fragrance_free:
        if any(x in ing_str for x in ["parfum", "fragrance", "perfume", "aroma"]):
            severity = "blocking" if "ingredients" in settings.MANDATORY_FIELDS else "warning"
            issue = ValidationIssue(
                id=uuid.uuid4(),
                canonical_product_id=item.canonical_product_id,
                field_name="ingredients",
                severity=severity,
                issue_type="conflicting_information",
                message="Product claims to be fragrance-free, but ingredients contain fragrance components (e.g. Parfum).",
                created_by_type="system"
            )
            db.add(issue)

    # 7. Alcohol-free claims vs Alcohol Denat. in ingredients
    is_alcohol_free = "alcohol-free" in claims_str or "alcohol-free" in desc_str or "alcohol free" in claims_str or "alcohol free" in desc_str
    alc_free_field = enrichment_result.get("alcohol_free", {})
    if isinstance(alc_free_field, dict) and alc_free_field.get("value") == "yes":
        is_alcohol_free = True
        
    if is_alcohol_free:
        has_drying_alcohol = False
        if "alcohol denat" in ing_str or "sd alcohol" in ing_str or "ethanol" in ing_str or "ethyl alcohol" in ing_str:
            has_drying_alcohol = True
        else:
            for m in re.finditer(r"\b(\w+\s+)?alcohol\b", ing_str):
                prefix = m.group(1) or ""
                prefix = prefix.strip()
                if prefix not in ["cetearyl", "cetyl", "stearyl", "behenyl", "benzyl", "lanolin", "myristyl", "isopropyl"]:
                    has_drying_alcohol = True
                    break
        if has_drying_alcohol:
            severity = "blocking" if "ingredients" in settings.MANDATORY_FIELDS else "warning"
            issue = ValidationIssue(
                id=uuid.uuid4(),
                canonical_product_id=item.canonical_product_id,
                field_name="ingredients",
                severity=severity,
                issue_type="conflicting_information",
                message="Product claims to be alcohol-free, but ingredients contain drying alcohols (e.g. Alcohol Denat.).",
                created_by_type="system"
            )
            db.add(issue)

    # 8. Missing brand (BLOCKING severity if configured as mandatory)
    if not raw_brand or raw_brand.strip().lower() in ["", "unknown", "missing"]:
        severity = "blocking" if "brand" in settings.MANDATORY_FIELDS else "warning"
        issue = ValidationIssue(
            id=uuid.uuid4(),
            canonical_product_id=item.canonical_product_id,
            field_name="brand",
            severity=severity,
            issue_type="missing_brand",
            message="Product brand is missing or unknown.",
            created_by_type="system"
        )
        db.add(issue)

    # 9. Sparse row: validate the completed canonical record, not only the
    # original import row. A legitimate product can lack GTIN or online INCI
    # while still having strong identity, description, taxonomy and evidence.
    prod = db.query(CanonicalProduct).filter(CanonicalProduct.id == item.canonical_product_id).first()
    current_source_fields = {
        row.field_name for row in db.query(FieldValue).filter(
            FieldValue.canonical_product_id == item.canonical_product_id,
            FieldValue.is_current == True,
        ).all() if row.value not in (None, "", [], {})
    }
    completion_signals = [
        bool(raw_desc.strip()), bool(raw_ingr.strip()), bool(raw_ean), bool(raw_size),
        bool(prod and prod.image_url), bool(prod and prod.category_id),
        "rating" in current_source_fields, "availability" in current_source_fields,
    ]
    is_sparse = not raw_name.strip() or sum(completion_signals) < 2
            
    if is_sparse:
        severity = "blocking" if "product_name" in settings.MANDATORY_FIELDS else "warning"
        issue = ValidationIssue(
            id=uuid.uuid4(),
            canonical_product_id=item.canonical_product_id,
            field_name="product_name",
            severity=severity,
            issue_type="sparse_row",
            message="Product row is sparse (missing crucial metadata fields).",
            created_by_type="system"
        )
        db.add(issue)

    # 10. Missing required Category check
    if prod and prod.category_id is None:
        severity = "blocking" if settings.CATEGORY_MANDATORY else "warning"
        issue = ValidationIssue(
            id=uuid.uuid4(),
            canonical_product_id=item.canonical_product_id,
            field_name="category_id",
            severity=severity,
            issue_type="missing_category",
            message="Product category is missing.",
            created_by_type="system"
        )
        db.add(issue)

    # Check 2: Low-confidence field validation warning and conflicts check
    for field, field_data in enrichment_result.items():
        if isinstance(field_data, dict):
            val = field_data.get("value")
            status = field_data.get("value_status") or field_data.get("claim_status") or field_data.get("targeting_status")
            confidence = field_data.get("confidence")
            
            # Check for conflict
            if is_conflicting(val, status):
                severity = "blocking" if field in settings.MANDATORY_FIELDS else "warning"
                issue = ValidationIssue(
                    id=uuid.uuid4(),
                    canonical_product_id=item.canonical_product_id,
                    field_name=field,
                    severity=severity,
                    issue_type="conflicting_information",
                    message=f"Enriched field '{field}' has conflicting values.",
                    created_by_type="system"
                )
                db.add(issue)
            # Check for low confidence warning on non-unknowns
            elif should_create_low_confidence_warning(field, val, status, "ai_inference", confidence):
                msg = f"Enriched field '{field}' has low confidence score ({confidence})."
                issue = ValidationIssue(
                    id=uuid.uuid4(),
                    canonical_product_id=item.canonical_product_id,
                    field_name=field,
                    severity="warning",
                    issue_type="low_confidence_enrichment",
                    message=msg,
                    created_by_type="system"
                )
                db.add(issue)

    item.enrichment_status = "succeeded"
    item.status = "completed"
    item.completed_at = datetime.utcnow()
    db.commit()

def run_job_worker(db: Session, job_id: uuid.UUID):
    """Executes the complete processing lifecycle for a single ImportJob.
    """
    job = db.query(ImportJob).filter(ImportJob.id == job_id).first()
    if not job:
        return

    job.status = "processing"
    db.commit()

    mapping = job.column_mapping

    # First run stale job recovery to make sure no items are orphaned
    recover_stale_job_items(db)

    while True:
        # Atomic claim of a single item
        if db.bind.dialect.name == "postgresql":
            item = db.query(ImportJobItem).filter(
                ImportJobItem.import_job_id == job_id,
                ImportJobItem.status == "pending"
            ).order_by(ImportJobItem.source_row_number.asc()).with_for_update(skip_locked=True).first()
        else:
            item = db.query(ImportJobItem).filter(
                ImportJobItem.import_job_id == job_id,
                ImportJobItem.status == "pending"
            ).order_by(ImportJobItem.source_row_number.asc()).first()

        if not item:
            break

        try:
            item.status = "processing"
            item.started_at = datetime.utcnow()
            db.commit()

            listing = db.query(SourceListing).filter(SourceListing.id == item.source_listing_id).first()
            raw_data = listing.raw_data
            
            raw_name = source_value(raw_data, mapping, "product_name")
            raw_brand = source_value(raw_data, mapping, "brand")
            source_ean = source_value(raw_data, mapping, "ean")
            # Normalize valid spreadsheet representations for exact matching,
            # while retaining a genuinely invalid supplied identifier so the
            # existing validation workflow can report it to the user.
            raw_ean = normalize_gtin_value(source_ean) or (source_ean or None)
            raw_size, raw_unit = split_size_and_unit(
                source_value(raw_data, mapping, "size"), source_value(raw_data, mapping, "unit")
            )
            raw_price = source_value(raw_data, mapping, "price")

            # Identity resolution precedes canonical matching/creation.  This
            # prevents supplier aliases and shorthand descriptions from
            # becoming permanent brand or family records when exact corpus
            # evidence already identifies the consumer product.
            from app.services.product_understanding import resolve_product_understanding, understanding_snapshot_values
            pre_understanding = resolve_product_understanding(db, raw_data=raw_data, mapping=mapping)
            pre_values = understanding_snapshot_values(pre_understanding)
            if pre_understanding.get("identity_status") == "resolved":
                # Product Understanding may fill genuinely missing identity
                # inputs for matching, but it must never replace explicit
                # customer values during deterministic source ingestion.
                raw_name = raw_name or pre_values.get("product_name")
                raw_brand = raw_brand or pre_values.get("brand")
                raw_ean = raw_ean or pre_values.get("gtin")
                understood_size = pre_values.get("size")
                if understood_size and not raw_size:
                    raw_size, raw_unit = split_size_and_unit(str(understood_size), raw_unit)
            
            # Step 1: Matching / Deduplication
            match_status, score, matched_canonical_id, matched_variant_id = evaluate_match(
                db=db,
                raw_name=raw_name,
                raw_brand=raw_brand,
                raw_gtin=raw_ean,
                raw_size=raw_size
            )

            item.match_status = match_status
            item.duplicate_score = score

            if match_status == "ambiguous":
                # Must halt processing for human review
                item.status = "awaiting_match_review"
                db.commit()
                continue
                
            elif match_status in ["exact_match", "deterministic_match", "candidate"]:
                # Auto-create missing variant if size is new
                if not matched_variant_id and matched_canonical_id:
                    # check if a variant with the same size/gtin already exists
                    variant = None
                    if raw_ean:
                        variant = db.query(ProductVariant).filter(
                            ProductVariant.gtin == raw_ean,
                            ProductVariant.is_deleted == False
                        ).first()
                    if not variant and raw_size:
                        # Find by equivalent size
                        from app.services.deduplication import is_size_equivalent
                        all_vars = db.query(ProductVariant).filter(
                            ProductVariant.canonical_product_id == matched_canonical_id,
                            ProductVariant.is_deleted == False
                        ).all()
                        for v in all_vars:
                            if is_size_equivalent(v.size, raw_size):
                                variant = v
                                break
                    
                    if not variant:
                        variant = ProductVariant(
                            id=uuid.uuid4(),
                            canonical_product_id=matched_canonical_id,
                            variant_name=raw_size or "Standard Size",
                            gtin=raw_ean,
                            size=raw_size,
                            unit=raw_unit,
                        )
                        db.add(variant)
                        db.flush()
                    matched_variant_id = variant.id

                item.canonical_product_id = matched_canonical_id
                item.product_variant_id = matched_variant_id
                item.status = "enriching"
                db.commit()
                
            else: # new_product
                # Find or create Brand
                norm_brand = normalize_text(raw_brand)
                brand = db.query(Brand).filter(Brand.normalized_name == norm_brand).first()
                if not brand:
                    brand = Brand(
                        id=uuid.uuid4(),
                        name=raw_brand,
                        normalized_name=norm_brand
                    )
                    db.add(brand)
                    db.flush()

                # Create new Canonical Product
                canonical = CanonicalProduct(
                    id=uuid.uuid4(),
                    brand_id=brand.id,
                    product_name=raw_name,
                    normalized_name=normalize_text(raw_name),
                    review_status="imported"
                )
                db.add(canonical)
                db.flush()

                # Create new Variant
                variant = ProductVariant(
                    id=uuid.uuid4(),
                    canonical_product_id=canonical.id,
                    variant_name=raw_size,
                    gtin=raw_ean,
                    size=raw_size,
                    unit=raw_unit,
                )
                db.add(variant)
                db.flush()

                # Save price to source context
                if raw_price:
                    try:
                        price_num = float(raw_price)
                        price_rec = SourcePrice(
                            id=uuid.uuid4(),
                            source_listing_id=listing.id,
                            product_variant_id=variant.id,
                            amount=price_num,
                            currency="EUR"
                        )
                        db.add(price_rec)
                    except ValueError:
                        pass

                item.canonical_product_id = canonical.id
                item.product_variant_id = variant.id
                item.status = "enriching"
                db.commit()

                record_audit(
                    db=db,
                    entity_type="CanonicalProduct",
                    entity_id=canonical.id,
                    display_label=raw_name,
                    action="create",
                    before={},
                    after={"product_name": raw_name, "brand": raw_brand},
                    changed={}
                )

            # Update Listing link
            listing.canonical_product_id = item.canonical_product_id
            listing.product_variant_id = item.product_variant_id

            # Feed ingestion is deterministic source-data merge only. Paid AI
            # enrichment and product research are explicit, separate actions.
            from app.services.source_data_merge import merge_source_listing
            merge_source_listing(
                db, listing=listing, mapping=mapping,
                canonical_product_id=item.canonical_product_id,
                product_variant_id=item.product_variant_id,
            )
            item.status = "completed"
            item.enrichment_status = "not_requested"
            item.completed_at = datetime.utcnow()
            
            job.processed_rows += 1
            db.commit()

        except Exception as e:
            db.rollback()
            try:
                # Refresh item/job reference after rollback
                item = db.query(ImportJobItem).filter(ImportJobItem.id == item.id).first()
                job = db.query(ImportJob).filter(ImportJob.id == job_id).first()
                if item and job:
                    item.status = "failed"
                    item.enrichment_status = "permanent_failed"
                    item.failure_code = "processing_error"
                    item.failure_message = str(e)
                    job.processed_rows += 1
                    db.commit()
            except Exception as inner_e:
                logger.error(f"Failed to save failure status for row: {str(inner_e)}")
            logger.error(f"Failed to process row {item.source_row_number if item else 'unknown'}: {str(e)}")

    job.status = "completed"
    db.commit()

def recover_stale_job_items(db: Session, timeout_seconds: int = 600):
    """Finds items stuck in 'processing' or 'enriching' for longer than the timeout and resets them back to 'pending'."""
    cutoff = datetime.utcnow() - timedelta(seconds=timeout_seconds)
    stale_items = db.query(ImportJobItem).filter(
        ImportJobItem.status.in_(["processing", "enriching"]),
        ImportJobItem.updated_at <= cutoff
    ).all()
    for item in stale_items:
        item.status = "pending"
        item.enrichment_status = "not_requested"
        logger.warning(f"Reset stale ImportJobItem {item.id} back to pending (timeout exceeded)")
    if stale_items:
        db.commit()

def run_job_in_background(job_id):
    db = SessionLocal()
    try:
        run_job_worker(db, job_id)
    except Exception as e:
        logger.error(f"Failed background job execution for {job_id}: {str(e)}")
    finally:
        db.close()

def recover_unfinished_jobs():
    """Recovers and processes pending/processing jobs upon application startup.
    """
    db = SessionLocal()
    try:
        recover_stale_job_items(db)
        
        # Find crashed jobs
        jobs = db.query(ImportJob).filter(
            ImportJob.status.in_(["pending", "processing"])
        ).all()
        
        for job in jobs:
            # Set items that were interrupted during execution back to pending
            db.query(ImportJobItem).filter(
                ImportJobItem.import_job_id == job.id,
                ImportJobItem.status.in_(["processing", "enriching"])
            ).update({"status": "pending"})
            db.commit()
            
            # Resume Job in a background thread with a fresh DB session
            import threading
            thread = threading.Thread(target=run_job_in_background, args=(job.id,))
            thread.start()
    except Exception as e:
        logger.error(f"Worker recovery failed: {str(e)}")
    finally:
        db.close()
