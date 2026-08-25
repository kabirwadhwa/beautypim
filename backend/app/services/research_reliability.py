"""Business-level reliability helpers for durable product research.

This module does not decide product truth.  It plans conservative identity
queries and evaluates whether an already-safe research run materially improved
the dossier.
"""
from __future__ import annotations

import re
from typing import Any


TERMINAL_OUTCOMES = {
    "improved", "partially_improved", "no_material_improvement",
    "needs_identity_resolution", "needs_taxonomy_resolution", "blocked_sources", "rate_limited_retriable", "failed",
}


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _raw_value(raw: dict[str, Any], *names: str) -> str:
    lookup = {re.sub(r"\W+", "", str(key).lower()): value for key, value in (raw or {}).items()}
    for name in names:
        value = lookup.get(re.sub(r"\W+", "", name.lower()))
        if _text(value):
            return _text(value)
    return ""


def clean_enterprise_product_name(name: str, *, context: str = "") -> str:
    """Remove source-system noise and expand only context-supported shorthand."""
    value = f" {_text(name)} "
    context_text = f" {_text(context).lower()} "
    # Retailer prefixes are query noise, not asserted brands.  Keep the raw
    # name separately in every plan for provenance and fallback searching.
    value = re.sub(r"^\s*(?:LAT|CL|M)\s+", "", value, flags=re.I)
    safe_expansions = {
        r"\bHYDR\.?\b": "Hydrating",
        r"\bEDT\b": "Eau de Toilette",
        r"\bEDP\b": "Eau de Parfum",
    }
    if re.search(r"\b(?:makeup|foundation|fond de teint|teint)\b", context_text, re.I):
        safe_expansions[r"\bFDT\.?\b"] = "Foundation"
    if re.search(r"\b(?:body|bath|corps|körper|pflege)\b", context_text, re.I):
        safe_expansions.update({r"\bBM\b": "Body Milk", r"\bDG\b": "Shower Gel"})
    for pattern, replacement in safe_expansions.items():
        value = re.sub(pattern, replacement, value, flags=re.I)
    value = re.sub(r"\b(?:L1C|L1C|DOUBL)\b", " ", value, flags=re.I)
    value = re.sub(r"\b(Body Milk|Shower Gel)\s+\1\b", r"\1", value, flags=re.I)
    return _text(value.strip(" ._-/"))


def build_identity_query_plan(*, brand: str, product_name: str, gtin: str = "",
                              product_format: str = "", raw_data: dict[str, Any] | None = None,
                              category: str = "", corpus_candidates: list[dict] | None = None) -> list[dict[str, str]]:
    """Create a bounded, ordered set of identity searches without asserting guesses."""
    raw = raw_data or {}
    raw_name = _raw_value(raw, "article description", "product name", "product_name", "description", "name") or product_name
    supplier = _raw_value(raw, "supplier", "supplier name", "manufacturer", "vendor")
    source_brand = _raw_value(raw, "brand", "brand name") or brand
    source_category = _raw_value(raw, "category", "bgb subgroup", "bgb typegroup", "subcategory") or category
    context = " ".join((source_category, product_format, raw_name, supplier))
    cleaned = clean_enterprise_product_name(raw_name, context=context)
    resolved_candidates = [
        " ".join(_text(row.get(key)) for key in ("brand", "product_name", "product_type") if _text(row.get(key)))
        for row in (corpus_candidates or [])[:3]
    ]
    attempts: list[tuple[str, str]] = []
    digits = re.sub(r"\D", "", gtin or "")
    if digits:
        attempts.append(("exact_gtin", digits))
        if source_brand:
            attempts.append(("gtin_brand", f"{digits} {source_brand}"))
    for candidate in resolved_candidates:
        if candidate:
            attempts.append(("corpus_identity", candidate))
    if source_brand and cleaned:
        attempts.append(("brand_clean_name", f"{source_brand} {cleaned}"))
    if supplier and cleaned:
        attempts.append(("supplier_clean_name", f"{supplier} {cleaned}"))
    if cleaned:
        attempts.append(("clean_name_category", " ".join(v for v in (cleaned, source_category, product_format) if v)))
    if raw_name:
        attempts.append(("raw_source_fallback", raw_name))
    output, seen = [], set()
    for strategy, query in attempts:
        normalized = _text(query)
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            output.append({"strategy": strategy, "query": normalized})
    return output[:7]


def classify_research_error(message: str) -> str:
    lowered = _text(message).lower()
    if any(term in lowered for term in ("rate limit", "http 429", "tokens per min", "tpm")):
        return "rate_limited"
    if any(term in lowered for term in ("http 403", "captcha", "bot challenge", "refused browser crawling")):
        return "source_blocked"
    if any(term in lowered for term in ("timeout", "timed out", "temporarily", "unavailable", "connection")):
        return "transient"
    if any(term in lowered for term in ("page is navigating", "changing the content", "redirect")):
        return "source_transient"
    if any(term in lowered for term in ("gtin mismatch", "wrong product", "unsafe identity", "invalid payload")):
        return "validation"
    return "technical"


def snapshot_metrics(quality: dict[str, Any], *, field_values: dict[str, Any], image_present: bool,
                     review: dict[str, Any] | None, formulation_count: int) -> dict[str, Any]:
    review = review or {}
    return {
        "completeness": quality.get("overall_completeness", 0),
        "identity_status": quality.get("identity_status"),
        "identity_completeness": quality.get("identity_completeness", 0),
        "category_module": quality.get("category_module", "unknown"),
        "taxonomy_status": quality.get("taxonomy_status"),
        "fields": field_values,
        "missing_high_priority_fields": list(quality.get("missing_high_priority_fields") or []),
        "image_present": image_present,
        "review_evidence_present": bool(review),
        "review_sample_count": int(review.get("review_sample_count") or 0),
        "review_intelligence_strength": review.get("review_intelligence_strength") or "insufficient",
        "formulation_count": formulation_count,
    }


def evaluate_research_outcome(before: dict[str, Any], after: dict[str, Any], *, result: dict[str, Any],
                              errors: list[str] | None = None) -> dict[str, Any]:
    """Return an honest business outcome independent of worker lifecycle state."""
    errors = [str(value) for value in (errors or []) if value]
    before_fields, after_fields = before.get("fields") or {}, after.get("fields") or {}
    fields_added = sorted(name for name, value in after_fields.items() if value not in (None, "", [], {}) and before_fields.get(name) in (None, "", [], {}))
    fields_changed = sorted(name for name, value in after_fields.items()
                            if name in before_fields and before_fields.get(name) not in (None, "", [], {})
                            and value != before_fields.get(name))
    image_added = bool(after.get("image_present") and not before.get("image_present"))
    review_sample_delta = int(after.get("review_sample_count") or 0) - int(before.get("review_sample_count") or 0)
    review_added = bool(
        (after.get("review_evidence_present") and not before.get("review_evidence_present"))
        or review_sample_delta > 0
        or int(result.get("review_texts_persisted") or 0) > 0
    )
    formulation_added = int(after.get("formulation_count") or 0) > int(before.get("formulation_count") or 0)
    completeness_delta = int(after.get("completeness") or 0) - int(before.get("completeness") or 0)
    identity_improved = (
        before.get("identity_status") != after.get("identity_status")
        and after.get("identity_status") == "complete"
    ) or int(after.get("identity_completeness") or 0) > int(before.get("identity_completeness") or 0)
    taxonomy_improved = bool(
        (before.get("taxonomy_status") != "resolved" or before.get("category_module") == "unknown")
        and after.get("taxonomy_status") == "resolved"
        and after.get("category_module") not in {None, "", "unknown"}
    )
    meaningful = bool(
        fields_added or image_added or review_added or formulation_added
        or identity_improved or taxonomy_improved or completeness_delta >= 5
    )
    classifications = [classify_research_error(value) for value in errors]
    blocked_count = sum(value in {"source_blocked", "source_transient"} for value in classifications)
    identity_unresolved = bool(result.get("identity_unresolved") or after.get("identity_status") in {"ambiguous", "incomplete", "unresolved", "conflicting"})
    taxonomy_unresolved = bool(after.get("taxonomy_status") == "needs_review" or after.get("category_module") == "unknown")
    if identity_unresolved:
        outcome = "needs_identity_resolution"
    elif taxonomy_unresolved:
        outcome = "needs_taxonomy_resolution" if not meaningful else "partially_improved"
    elif meaningful and (int(after.get("completeness") or 0) >= 75 or not after.get("missing_high_priority_fields")):
        outcome = "improved"
    elif meaningful:
        outcome = "partially_improved"
    elif blocked_count and not int(result.get("sources_ingested") or 0):
        outcome = "blocked_sources"
    elif any(value == "rate_limited" for value in classifications):
        outcome = "rate_limited_retriable"
    elif errors and all(value in {"technical", "validation"} for value in classifications):
        outcome = "failed"
    else:
        outcome = "no_material_improvement"
    return {
        "business_outcome": outcome,
        "before_completeness": before.get("completeness", 0),
        "after_completeness": after.get("completeness", 0),
        "completeness_delta": completeness_delta,
        "before_identity_status": before.get("identity_status"),
        "after_identity_status": after.get("identity_status"),
        "fields_added": fields_added,
        "fields_changed": fields_changed,
        "fields_still_missing": list(after.get("missing_high_priority_fields") or []),
        "image_added": image_added,
        "review_evidence_added": review_added,
        "review_samples_added": max(0, review_sample_delta),
        "formulation_added": formulation_added,
        "taxonomy_resolved": taxonomy_improved,
        "sources_discovered": int(result.get("candidates") or 0),
        "sources_ingested": int(result.get("sources_ingested") or 0),
        "sources_blocked": blocked_count,
        "failure_reason": errors[0] if errors else None,
    }


def public_business_status(outcome: str) -> str:
    return {
        "improved": "READY",
        "partially_improved": "IMPROVED_BUT_INCOMPLETE",
        "needs_identity_resolution": "REVIEW_REQUIRED",
        "needs_taxonomy_resolution": "REVIEW_REQUIRED",
        "no_material_improvement": "NO_EVIDENCE_FOUND",
        "rate_limited_retriable": "TECHNICAL_RETRY_REQUIRED",
        "blocked_sources": "BLOCKED",
        "failed": "TECHNICAL_RETRY_REQUIRED",
    }.get(outcome, "PROCESSING")
