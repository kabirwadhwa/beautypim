"""Canonical, evidence-grounded multi-source Review Intelligence synthesis."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import requests

from app.config import settings
from app.models import Brand, CanonicalProduct, Category, FieldValue, ScrapedProductObservation


def _words(value: str) -> int:
    return len(str(value or "").split())


def _valid_themes(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [" ".join(str(item).split())[:180] for item in value if str(item).strip()][:5]


def _insufficient_lines(summary: dict[str, Any]) -> list[str]:
    """Backward-compatible truthful disclosure used by API/tests."""
    total = int(summary.get("review_count") or 0)
    return [
        f"Review evidence is insufficient for a reliable aggregate: {total:,} observed review{'s' if total != 1 else ''}.",
        "BeautyPIM does not present this limited evidence as a commercially meaningful average rating.",
        "The available aggregate cannot establish recurring praise or complaint themes.",
        "Additional exact-product review text is required before customer sentiment is summarized.",
    ]


def _sample_fallback(samples: list[dict], rating: Any, count: int) -> tuple[str, list[str], list[str], list[str]]:
    """A truthful fallback that only describes the actual retained sample."""
    positive = [s for s in samples if s.get("rating") is not None and float(s["rating"]) >= 4]
    negative = [s for s in samples if s.get("rating") is not None and float(s["rating"]) <= 2]
    mixed = [s for s in samples if s not in positive and s not in negative]
    basis = f"{len(samples)} de-identified review samples"
    aggregate = f" and an aggregate of {count:,} represented reviews" if count else ""
    text = (
        f"BeautyPIM collected {basis}{aggregate} for this exact product. "
        f"Within the retained sample, {len(positive)} reviews were strongly positive, {len(negative)} were strongly negative, "
        f"and {len(mixed)} were mixed or did not include a usable star rating. "
        "The available text is sufficient to describe the balance of the observed sample, but an AI theme synthesis was unavailable; "
        "BeautyPIM therefore does not infer specific praise or complaint themes that were not reliably established. "
        "This summary is intentionally limited to the collected exact-product evidence and should be read alongside the source count and evidence-strength label."
    )
    return text, [], [], []


def _ai_intelligence(summary: dict, product_name: str, brand: str, category: str) -> tuple[str, list[str], list[str], list[str], str]:
    samples = list(summary.get("review_samples") or [])[:100]
    fallback = _sample_fallback(samples, summary.get("average_rating"), int(summary.get("review_count") or 0))
    if not settings.OPENAI_API_KEY:
        return (*fallback, "deterministic-evidence-summary")
    evidence = {
        "average_rating": summary.get("average_rating"),
        "represented_review_count": summary.get("review_count"),
        "source_count": summary.get("review_source_count"),
        "review_samples": [{"text": row.get("text"), "rating": row.get("rating"), "title": row.get("title")} for row in samples],
    }
    request = {
        "model": settings.OPENAI_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": (
                "Create evidence-grounded Review Intelligence from actual de-identified exact-product review text. "
                "Return JSON keys summary, positive_themes, negative_themes, mixed_themes. The summary must be one coherent "
                "120-220 word paragraph. Themes must be concise, product/category-specific, non-duplicative and supported by "
                "multiple samples where possible. Discuss both strengths and weaknesses when present. Never invent opinions, "
                "claims, quotations, demographics or reviewer identities. If evidence is limited, say so explicitly."
            )},
            {"role": "user", "content": json.dumps({"brand": brand, "product": product_name, "category": category, "evidence": evidence})},
        ],
    }
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}", "Content-Type": "application/json"},
            json=request, timeout=(10, 45),
        )
        response.raise_for_status()
        payload = json.loads(response.json()["choices"][0]["message"]["content"])
        paragraph = " ".join(str(payload.get("summary") or "").split())
        if not 100 <= _words(paragraph) <= 240:
            raise ValueError("Review summary length was outside the safe range")
        return (paragraph, _valid_themes(payload.get("positive_themes")),
                _valid_themes(payload.get("negative_themes")), _valid_themes(payload.get("mixed_themes")),
                settings.OPENAI_MODEL)
    except (requests.RequestException, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return (*fallback, "deterministic-evidence-summary")


def summarize_product_reviews(db, product_id, *, force: bool = False) -> dict[str, Any] | None:
    from app.services.review_aggregate import select_review_aggregate
    aggregate = select_review_aggregate(db, product_id)
    if not aggregate:
        return None
    base = {**(aggregate.get("review_summary") or {}),
            "average_rating": aggregate.get("average_rating"),
            "review_count": aggregate.get("review_count"),
            "represented_review_count": aggregate.get("represented_review_count"),
            "review_source_count": aggregate.get("review_source_count"),
            "review_sample_count": aggregate.get("review_sample_count"),
            "review_samples": (aggregate.get("review_summary") or {}).get("review_samples") or [],
            "evidence_strength": aggregate.get("evidence_strength"),
            "sources": aggregate.get("sources") or []}
    from app.services.review_evidence import enforce_review_summary_invariants
    base = enforce_review_summary_invariants(base)
    samples = base["review_samples"]
    if samples:
        product = db.query(CanonicalProduct).filter(CanonicalProduct.id == product_id).first()
        brand = db.query(Brand).filter(Brand.id == product.brand_id).first() if product and product.brand_id else None
        category_row = db.query(Category).filter(Category.id == product.category_id).first() if product and product.category_id else None
        category = category_row.path if category_row else ""
        paragraph, positive, negative, mixed, model = _ai_intelligence(
            base, product.product_name if product else "", brand.name if brand else "", category,
        )
        base.update({"ai_summary_text": paragraph, "summary": paragraph,
                     "positive_themes": positive, "negative_themes": negative, "mixed_themes": mixed,
                     "summary_model": model,
                     "evidence_limitation": ("The synthesis reflects a bounded sample of publicly available exact-product reviews; it may not represent every customer." if len(samples) < 20 else None)})
    else:
        base.update({
            "ai_summary_text": None, "summary": None,
            "positive_themes": [], "negative_themes": [], "mixed_themes": [],
            "summary_model": "aggregate-only-no-synthesis",
            "evidence_limitation": "Aggregate rating and count evidence is available, but review-text evidence was insufficient for a detailed summary.",
        })
    base["summary_generated_at"] = datetime.now(timezone.utc).isoformat()
    base["generated_at"] = base["summary_generated_at"]
    base["limitations"] = [base["evidence_limitation"]] if base.get("evidence_limitation") else []

    if aggregate.get("observation_id"):
        observation = db.query(ScrapedProductObservation).filter(
            ScrapedProductObservation.id == aggregate["observation_id"]
        ).first()
        if observation:
            observation.normalized_payload = {**(observation.normalized_payload or {}), "review_summary": base}

    current = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product_id,
        FieldValue.field_name == "review_summary", FieldValue.is_current == True,
    ).first()
    if current:
        current.is_current = False
    db.flush()
    db.add(FieldValue(
        canonical_product_id=product_id, field_name="review_summary", value=base,
        source_type="ai_inference" if samples else "source_data",
        source_reference=aggregate.get("evidence_reference"), confidence_score=0.9 if samples else 0.7,
        review_status="inferred", is_current=True,
        evidence=[{"source": row.get("name"), "source_domain": row.get("domain"),
                   "match_scope": row.get("match_scope")} for row in aggregate.get("sources") or []],
        reasoning_summary="Review Intelligence generated only from canonical exact-product review evidence.",
        semantic_status="inferred" if samples else "source_supported", semantic_status_type="review_summary",
    ))
    db.flush()
    return base
