"""Evidence-grounded customer-review synthesis for product dossiers.

The crawler retains aggregate signals rather than verbatim customer reviews.
This service turns those retained signals into four or five concise, clearly
labelled lines without inventing product claims or individual quotations.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import requests

from app.config import settings
from app.models import Brand, CanonicalProduct, FieldValue, ScrapedProductObservation


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _whole_number(value: Any) -> int:
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0


def _fallback_lines(summary: dict[str, Any]) -> list[str]:
    rating = _number(summary.get("average_rating"))
    total = _whole_number(summary.get("review_count"))
    sample = _whole_number(summary.get("review_sample_count"))
    praised = [str(value).replace("_", " ") for value in summary.get("frequently_praised_topics") or []]
    complaints = [str(value).replace("_", " ") for value in summary.get("frequent_complaint_topics") or []]
    if rating is not None and total:
        opening = f"Customer response is {'strongly positive' if rating >= 4 else 'mixed' if rating < 3.5 else 'generally positive'}, averaging {rating:.1f}/5 across {total:,} reviews."
    elif rating is not None:
        opening = f"The available aggregate rating is {rating:.1f}/5."
    else:
        opening = "The available review evidence does not include a reliable aggregate rating."
    praise = (
        f"Visible review samples most frequently praise {', '.join(praised[:3])}."
        if praised else "The visible sample does not establish a recurring praised product characteristic."
    )
    concern = (
        f"Recurring reservations most often concern {', '.join(complaints[:3])}."
        if complaints else "No recurring complaint theme was established in the visible sample."
    )
    basis = (
        f"This synthesis is based on {sample:,} visible review samples and the source's aggregate metrics; it does not quote individual reviewers."
        if sample else "This synthesis is limited to source-level aggregate review signals and does not quote individual reviewers."
    )
    return [opening, praise, concern, basis]


def _insufficient_lines(summary: dict[str, Any]) -> list[str]:
    total = _whole_number(summary.get("review_count"))
    first = (
        f"Review evidence is insufficient for a reliable aggregate: {total:,} observed review{'s' if total != 1 else ''}."
        if total else "A review count or usable average rating is not available from current evidence."
    )
    return [
        first,
        "BeautyPIM does not present this limited evidence as a commercially meaningful average rating.",
        "The available aggregate cannot establish recurring praise or complaint themes.",
        "Additional exact-product review evidence is required before customer sentiment is summarized.",
    ]


def _valid_lines(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    lines = [" ".join(str(line).split()) for line in value if str(line).strip()]
    return lines[:5] if 4 <= len(lines) <= 5 and all(len(line) <= 320 for line in lines) else []


def _ai_lines(summary: dict[str, Any], product_name: str, brand: str) -> tuple[list[str], str]:
    fallback = _fallback_lines(summary)
    if not settings.OPENAI_API_KEY:
        return fallback, "deterministic-evidence-summary"
    evidence = {
        key: summary.get(key) for key in (
            "average_rating", "review_count", "review_sample_count", "rating_distribution",
            "frequently_praised_topics", "frequent_complaint_topics", "longevity_mentions",
            "sillage_mentions", "packaging_mentions",
        )
    }
    request = {
        "model": settings.OPENAI_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You summarize aggregate customer-review evidence for a beauty product dossier. "
                    "Return JSON with a summary_lines array containing exactly 4 or 5 concise sentences. "
                    "Use only the supplied aggregate signals. Do not invent benefits, sentiment themes, "
                    "review quotations, demographics or claims. Clearly acknowledge limited samples. "
                    "Write commercially useful but neutral English; include both praise and reservations "
                    "when supported and avoid repeating the rating in multiple lines."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({"brand": brand, "product": product_name, "review_evidence": evidence}),
            },
        ],
    }
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}", "Content-Type": "application/json"},
            json=request, timeout=(10, 30),
        )
        response.raise_for_status()
        payload = json.loads(response.json()["choices"][0]["message"]["content"])
        lines = _valid_lines(payload.get("summary_lines"))
        return (lines or fallback), settings.OPENAI_MODEL if lines else "deterministic-evidence-summary"
    except (requests.RequestException, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return fallback, "deterministic-evidence-summary"


def summarize_product_reviews(db, product_id, *, force: bool = False) -> dict[str, Any] | None:
    """Summarize the strongest current exact-product review observation once."""
    from app.services.review_aggregate import select_review_aggregate
    aggregate = select_review_aggregate(db, product_id)
    if not aggregate:
        return None
    # A count without a rating, or a single review, cannot support sentiment
    # synthesis.  Keep the evidence, but use the canonical truthful disclosure.
    insufficient = aggregate.get("review_quality") == "insufficient"
    if not aggregate.get("observation_id"):
        summary = {**(aggregate.get("review_summary") or {}),
                   "average_rating": aggregate.get("average_rating"),
                   "review_count": aggregate.get("review_count")}
        product = db.query(CanonicalProduct).filter(CanonicalProduct.id == product_id).first()
        brand = db.query(Brand).filter(Brand.id == product.brand_id).first() if product and product.brand_id else None
        lines, model = (
            _insufficient_lines(summary), "deterministic-insufficient-review-evidence"
        ) if insufficient else _ai_lines(summary, product.product_name if product else "", brand.name if brand else "")
        enriched = {**summary, "ai_summary_lines": lines, "ai_summary_text": "\n".join(lines),
                    "summary": "\n".join(lines), "summary_model": model,
                    "summary_generated_at": datetime.now(timezone.utc).isoformat()}
        current = db.query(FieldValue).filter(
            FieldValue.canonical_product_id == product_id,
            FieldValue.field_name == "review_summary", FieldValue.is_current == True,
        ).first()
        if current:
            current.is_current = False
        db.flush()
        db.add(FieldValue(
            canonical_product_id=product_id, product_variant_id=None,
            field_name="review_summary", value=enriched, source_type="ai_inference",
            source_reference=aggregate.get("evidence_reference"), confidence_score=0.9,
            review_status="inferred", is_current=True,
            evidence=[{"source": aggregate.get("source"), "source_domain": aggregate.get("source_domain"),
                       "match_scope": aggregate.get("match_scope")}],
            reasoning_summary="Review summary generated from the canonical exact-product aggregate.",
            semantic_status="inferred", semantic_status_type="review_summary",
        ))
        db.flush()
        return enriched
    observation = db.query(ScrapedProductObservation).filter(
        ScrapedProductObservation.id == aggregate["observation_id"]
    ).one()
    payload = observation.normalized_payload or {}
    summary = {**(aggregate.get("review_summary") or {}),
               "average_rating": aggregate.get("average_rating"),
               "review_count": aggregate.get("review_count")}
    if (summary.get("ai_summary_lines")
            and summary.get("summary_model") != "deterministic-aggregate-summary"
            and not force):
        return summary
    product = db.query(CanonicalProduct).filter(CanonicalProduct.id == product_id).first()
    brand = db.query(Brand).filter(Brand.id == product.brand_id).first() if product and product.brand_id else None
    lines, model = (
        _insufficient_lines(summary), "deterministic-insufficient-review-evidence"
    ) if insufficient else _ai_lines(
        summary, product.product_name if product else "", brand.name if brand else "",
    )
    enriched_summary = {
        **summary,
        "ai_summary_lines": lines,
        "ai_summary_text": "\n".join(lines),
        "summary": "\n".join(lines),
        "summary_model": model,
        "summary_generated_at": datetime.now(timezone.utc).isoformat(),
        "summary_basis": "aggregate rating and deterministic topic signals from visible exact-product review samples",
    }
    observation.normalized_payload = {**payload, "review_summary": enriched_summary}
    db.flush()
    return enriched_summary
