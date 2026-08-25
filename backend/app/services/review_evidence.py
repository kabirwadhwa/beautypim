"""Review-evidence invariants shared by parsing, persistence and presentation."""
from __future__ import annotations

import re
from typing import Any


def sanitize_review_samples(value: Any, *, limit: int = 100) -> list[dict[str, Any]]:
    """Retain only real, de-identified review text; declared counts are ignored."""
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in value if isinstance(value, list) else []:
        if not isinstance(row, dict):
            continue
        text = " ".join(str(row.get("text") or row.get("reviewBody") or "").split())[:4000]
        key = re.sub(r"\W+", "", text.casefold())
        if len(text) < 20 or not key or key in seen:
            continue
        seen.add(key)
        output.append({
            "text": text,
            "title": " ".join(str(row.get("title") or "").split())[:300] or None,
            "rating": row.get("rating"), "date": row.get("date"),
            "source_url": row.get("source_url"), "locale": row.get("locale"),
            "verified_purchase": row.get("verified_purchase") if isinstance(row.get("verified_purchase"), bool) else None,
        })
        if len(output) >= limit:
            break
    return output


def enforce_review_summary_invariants(value: Any) -> dict[str, Any]:
    summary = dict(value) if isinstance(value, dict) else {}
    samples = sanitize_review_samples(summary.get("review_samples"))
    summary["review_samples"] = samples
    summary["review_sample_count"] = len(samples)
    if not samples:
        for key in (
            "positive_themes", "negative_themes", "mixed_themes",
            "frequently_praised_topics", "frequent_complaint_topics",
        ):
            summary[key] = []
        summary["ai_summary_text"] = None
        summary["summary"] = None
        summary["summary_model"] = "aggregate-only-no-synthesis"
        summary["evidence_limitation"] = (
            "Aggregate rating and review-count evidence is available, but review-text evidence "
            "was insufficient for a detailed summary."
        )
    return summary
