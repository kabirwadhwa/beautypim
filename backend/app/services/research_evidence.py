"""Typed, policy-gated evidence used by Improve Product research.

Acquisition adapters produce :class:`EvidenceItem` objects.  They never write
canonical truth directly.  This module validates identity/scope, resolves
conflicts and persists accepted source-backed values without bypassing the
existing human/customer precedence contract.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.orm import Session

from app.models import CanonicalProduct, CrawlJob, FieldValue, ProductVariant, ValidationIssue
from app.scraping.url_safety import UnsafeUrl, validate_public_url

IdentityScope = Literal["exact_gtin", "exact_resolved_identity", "family_only", "unknown"]
AcquisitionMethod = Literal[
    "customer_source", "crawl_html", "json_ld", "embedded_json",
    "licensed_web_search", "internal_corpus", "ai_inference",
]


@dataclass(frozen=True)
class EvidencePolicy:
    minimum_scope: IdentityScope
    allowed_methods: frozenset[str]
    variant_specific: bool = False
    allow_family: bool = False
    inference_only: bool = False


@dataclass
class EvidenceItem:
    field_name: str
    proposed_value: Any
    source_url: str
    source_domain: str
    acquisition_method: AcquisitionMethod
    source_authority: str = "retailer"
    source_title: str | None = None
    matched_gtin: str | None = None
    matched_brand: str | None = None
    matched_product_name: str | None = None
    matched_product_family: str | None = None
    matched_concentration: str | None = None
    matched_shade: str | None = None
    matched_size: str | None = None
    matched_variant: str | None = None
    identity_scope: IdentityScope = "unknown"
    evidence_excerpt: str | None = None
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    confidence_inputs: dict[str, Any] = field(default_factory=dict)
    validation_result: dict[str, Any] = field(default_factory=dict)
    evidence_hash: str | None = None

    def finalize(self) -> "EvidenceItem":
        if not self.evidence_hash:
            value = json.dumps({
                "field": self.field_name, "value": self.proposed_value,
                "url": normalize_research_url(self.source_url),
                "scope": self.identity_scope, "method": self.acquisition_method,
            }, sort_keys=True, default=str, ensure_ascii=False)
            self.evidence_hash = hashlib.sha256(value.encode()).hexdigest()
        return self

    def payload(self) -> dict[str, Any]:
        self.finalize()
        return {
            **asdict(self),
            # Backwards-compatible provenance aliases used by existing API,
            # review and audit consumers.
            "source_reference": self.source_url,
            "supporting_text": self.evidence_excerpt,
            "evidence_type": self.acquisition_method,
            "match_scope": self.identity_scope,
        }


EXACT_ONLY_METHODS = frozenset({"customer_source", "crawl_html", "json_ld", "embedded_json", "licensed_web_search"})
SOURCE_METHODS = EXACT_ONLY_METHODS | {"internal_corpus"}
FIELD_POLICIES: dict[str, EvidencePolicy] = {
    "gtin": EvidencePolicy("exact_gtin", EXACT_ONLY_METHODS, variant_specific=True),
    "sku": EvidencePolicy("exact_gtin", EXACT_ONLY_METHODS, variant_specific=True),
    "inci": EvidencePolicy("exact_resolved_identity", EXACT_ONLY_METHODS, variant_specific=True),
    "ingredients": EvidencePolicy("exact_resolved_identity", EXACT_ONLY_METHODS, variant_specific=True),
    "formulation": EvidencePolicy("exact_resolved_identity", EXACT_ONLY_METHODS, variant_specific=True),
    "shade": EvidencePolicy("exact_resolved_identity", EXACT_ONLY_METHODS, variant_specific=True),
    "concentration": EvidencePolicy("exact_resolved_identity", EXACT_ONLY_METHODS, variant_specific=True),
    "fragrance_concentration": EvidencePolicy("exact_resolved_identity", EXACT_ONLY_METHODS, variant_specific=True),
    "top_notes": EvidencePolicy("exact_resolved_identity", SOURCE_METHODS, variant_specific=True),
    "heart_notes": EvidencePolicy("exact_resolved_identity", SOURCE_METHODS, variant_specific=True),
    "base_notes": EvidencePolicy("exact_resolved_identity", SOURCE_METHODS, variant_specific=True),
    "rating": EvidencePolicy("exact_resolved_identity", EXACT_ONLY_METHODS, variant_specific=True),
    "review_count": EvidencePolicy("exact_resolved_identity", EXACT_ONLY_METHODS, variant_specific=True),
    "review_samples": EvidencePolicy("exact_resolved_identity", EXACT_ONLY_METHODS, variant_specific=True),
    "image": EvidencePolicy("exact_resolved_identity", EXACT_ONLY_METHODS, variant_specific=True),
    "image_url": EvidencePolicy("exact_resolved_identity", EXACT_ONLY_METHODS, variant_specific=True),
    "claims": EvidencePolicy("exact_resolved_identity", EXACT_ONLY_METHODS),
    "clinical_claims": EvidencePolicy("exact_resolved_identity", EXACT_ONLY_METHODS),
    "regulatory": EvidencePolicy("exact_resolved_identity", EXACT_ONLY_METHODS),
    "description": EvidencePolicy("family_only", SOURCE_METHODS, allow_family=True),
    "benefits": EvidencePolicy("family_only", SOURCE_METHODS, allow_family=True),
    "directions": EvidencePolicy("family_only", SOURCE_METHODS, allow_family=True),
    "usage_instructions": EvidencePolicy("family_only", SOURCE_METHODS, allow_family=True),
    "product_positioning": EvidencePolicy("family_only", SOURCE_METHODS | {"ai_inference"}, allow_family=True),
    "target_audience": EvidencePolicy("family_only", SOURCE_METHODS | {"ai_inference"}, allow_family=True),
    "routine_step": EvidencePolicy("family_only", SOURCE_METHODS | {"ai_inference"}, allow_family=True, inference_only=True),
}
DEFAULT_POLICY = EvidencePolicy("exact_resolved_identity", SOURCE_METHODS)
SCOPE_RANK = {"unknown": 0, "family_only": 1, "exact_resolved_identity": 2, "exact_gtin": 3}
AUTHORITY_RANK = {
    "official_brand": 5, "official_retailer/private_label_owner": 5,
    "authorized_retailer": 4, "specialist_database": 3, "retailer": 2, "unknown": 1,
}


def normalize_research_url(value: str) -> str:
    """Normalize safely for job-local dedupe without changing URL semantics."""
    parsed = urlsplit(str(value or "").strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    port = f":{parsed.port}" if parsed.port and not (scheme == "https" and parsed.port == 443) else ""
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urlunsplit((scheme, f"{host}{port}", path.rstrip("/") or "/", parsed.query, ""))


def policy_for(field_name: str) -> EvidencePolicy:
    return FIELD_POLICIES.get(field_name, DEFAULT_POLICY)


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _tokens(value: Any) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", str(value or "").casefold()) if len(token) > 2}


def infer_identity_scope(item: EvidenceItem, product: CanonicalProduct, variant: ProductVariant | None) -> IdentityScope:
    expected_gtin = _digits(variant.gtin if variant else "")
    observed_gtin = _digits(item.matched_gtin)
    if expected_gtin and observed_gtin:
        return "exact_gtin" if expected_gtin == observed_gtin else "unknown"
    brand_ok = bool(product.brand and _tokens(product.brand.name) <= _tokens(item.matched_brand))
    product_tokens = _tokens(product.product_name)
    name_tokens = _tokens(item.matched_product_name or item.matched_product_family)
    if brand_ok and product_tokens and product_tokens <= name_tokens:
        expected_variant = " ".join(filter(None, [variant.variant_name if variant else None, variant.size if variant else None]))
        observed_variant = " ".join(filter(None, [item.matched_variant, item.matched_concentration, item.matched_shade, item.matched_size]))
        if expected_variant and observed_variant and not _tokens(expected_variant) <= _tokens(observed_variant):
            return "family_only"
        return "exact_resolved_identity"
    if brand_ok and name_tokens and product_tokens.intersection(name_tokens):
        return "family_only"
    return "unknown"


def validate_evidence(item: EvidenceItem, product: CanonicalProduct, variant: ProductVariant | None) -> tuple[bool, str]:
    if item.proposed_value in (None, "", [], {}):
        return False, "empty_value"
    # Licensed-search URLs have not passed through the crawler, so validate
    # them here. Crawl evidence inherits the fetcher's per-hop DNS/redirect
    # validation and must not perform a second, time-varying DNS lookup at the
    # persistence boundary.
    if item.acquisition_method == "licensed_web_search":
        try:
            validate_public_url(item.source_url, expected_domain=item.source_domain, allow_subdomains=False)
        except UnsafeUrl:
            return False, "unsafe_url"
    policy = policy_for(item.field_name)
    if item.acquisition_method not in policy.allowed_methods:
        return False, "acquisition_method_not_allowed"
    inferred = infer_identity_scope(item, product, variant)
    item.identity_scope = inferred
    if SCOPE_RANK[inferred] < SCOPE_RANK[policy.minimum_scope]:
        return False, f"insufficient_identity_scope:{inferred}<{policy.minimum_scope}"
    if policy.variant_specific and inferred == "family_only":
        return False, "variant_specific_field_from_family"
    if item.field_name in {"inci", "ingredients", "formulation"} and inferred != "exact_gtin":
        expected_variant = " ".join(filter(None, [
            variant.variant_name if variant else None, variant.size if variant else None,
        ]))
        observed_variant = " ".join(filter(None, [
            item.matched_variant, item.matched_concentration, item.matched_shade, item.matched_size,
        ]))
        if not expected_variant or not observed_variant or not _tokens(expected_variant) <= _tokens(observed_variant):
            return False, "exact_variant_formulation_not_proven"
    item.validation_result = {"accepted": True, "policy_scope": policy.minimum_scope}
    item.finalize()
    return True, "accepted"


def _current_field(db: Session, product_id, variant_id, field_name: str) -> FieldValue | None:
    query = db.query(FieldValue).filter(FieldValue.field_name == field_name, FieldValue.is_current == True)
    return query.filter(FieldValue.product_variant_id == variant_id).first() if variant_id else query.filter(
        FieldValue.canonical_product_id == product_id
    ).first()


def persist_evidence_item(db: Session, job: CrawlJob, product: CanonicalProduct,
                          variant: ProductVariant | None, item: EvidenceItem) -> tuple[bool, str]:
    """Persist an accepted claim without overriding human/customer truth."""
    accepted, reason = validate_evidence(item, product, variant)
    if not accepted:
        return False, reason
    policy = policy_for(item.field_name)
    variant_id = variant.id if variant and policy.variant_specific else None
    current = _current_field(db, product.id, variant_id, item.field_name)
    if current and current.source_type == "human_edit":
        return False, "protected_human_edit"
    current_evidence_rows = [row for row in (current.evidence or []) if isinstance(row, dict)] if current else []
    if current and current.source_type == "source_data" and any(
        row.get("source_origin") == "customer_import"
        or row.get("evidence_type") == "explicit_customer_source"
        or row.get("import_job_id")
        for row in current_evidence_rows
    ):
        return False, "protected_customer_source"
    evidence_payload = item.payload()
    if current and current.value == item.proposed_value:
        existing = list(current.evidence or [])
        if not any(row.get("evidence_hash") == item.evidence_hash for row in existing if isinstance(row, dict)):
            current.evidence = existing + [evidence_payload]
        return True, "corroborated"
    if current and current.value != item.proposed_value:
        current_evidence = next((row for row in (current.evidence or []) if isinstance(row, dict)), {})
        current_authority = AUTHORITY_RANK.get(str(current_evidence.get("source_authority") or "unknown"), 1)
        incoming_authority = AUTHORITY_RANK.get(item.source_authority, 1)
        db.add(ValidationIssue(
            id=uuid.uuid4(), canonical_product_id=product.id, field_name=item.field_name,
            severity="warning", issue_type="research_evidence_conflict",
            message=f"Conflicting source-backed values for {item.field_name} require review.",
            created_by_type="system",
        ))
        db.flush()
        if incoming_authority <= current_authority:
            return False, "conflicting_evidence_review_required"
        # Even stronger web evidence is not allowed to silently replace an
        # accepted exact fact; retain the conflict for explicit review.
        return False, "conflicting_evidence_review_required"
    db.add(FieldValue(
        id=uuid.uuid4(), canonical_product_id=None if variant_id else product.id,
        product_variant_id=variant_id, field_name=item.field_name,
        value=item.proposed_value, source_type="source_data",
        source_reference=item.source_url, confidence_score=0.95,
        review_status="inferred", is_current=True, evidence=[evidence_payload],
        reasoning_summary="Accepted by deterministic research evidence policy.",
        semantic_status="source_supported", semantic_status_type=item.acquisition_method,
    ))
    return True, "persisted"


def requested_scope(fields: list[str]) -> tuple[str, list[str]]:
    normalized = sorted({str(value).strip() for value in fields if str(value).strip()})
    return ("variant" if any(policy_for(name).variant_specific for name in normalized) else "family", normalized)


def research_fingerprint(product_id: Any, variant_id: Any, fields: list[str]) -> str:
    scope, normalized = requested_scope(fields)
    payload = {
        "product_id": str(product_id), "fields": normalized, "scope": scope,
        "variant_id": str(variant_id) if scope == "variant" and variant_id else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
