"""Review-evidence invariants shared by parsing, persistence and presentation."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any


def sanitize_review_samples_with_rejections(
    value: Any, *, limit: int = 100,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Retain real review text and explain every deterministic rejection."""
    output: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    accepted_normalized: list[tuple[str, set[str]]] = []
    rows = value if isinstance(value, list) else []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            rejected.append({"index": index, "reason": "invalid_review_record"})
            continue
        text = " ".join(str(row.get("text") or row.get("reviewBody") or "").split())[:4000]
        key = re.sub(r"\W+", "", text.casefold())
        if not text or not key:
            rejected.append({"index": index, "reason": "missing_review_text"})
            continue
        if len(text) < 20:
            rejected.append({"index": index, "reason": "review_text_too_short"})
            continue
        normalized = re.sub(r"\W+", " ", text.casefold()).strip()
        tokens = set(normalized.split())
        near_duplicate = any(
            {token for token in tokens if token.isdigit()} == {
                token for token in other_tokens if token.isdigit()
            }
            and (
                (
                bool(tokens | other_tokens)
                and len(tokens & other_tokens) / len(tokens | other_tokens) >= 0.94
                )
                or SequenceMatcher(None, normalized, other).ratio() >= 0.96
            )
            for other, other_tokens in accepted_normalized
        )
        if key in seen or near_duplicate:
            rejected.append({"index": index, "reason": "duplicate_review_text"})
            continue
        seen.add(key)
        accepted_normalized.append((normalized, tokens))
        output.append({
            "text": text,
            "title": " ".join(str(row.get("title") or "").split())[:300] or None,
            "rating": row.get("rating"), "date": row.get("date"),
            "source_url": row.get("source_url"), "locale": row.get("locale"),
            "source_domain": row.get("source_domain"),
            "verified_purchase": row.get("verified_purchase") if isinstance(row.get("verified_purchase"), bool) else None,
        })
        if len(output) >= limit:
            rejected.extend(
                {"index": remaining, "reason": "review_sample_limit_reached"}
                for remaining in range(index + 1, len(rows))
            )
            break
    return output, rejected


def sanitize_review_samples(value: Any, *, limit: int = 100) -> list[dict[str, Any]]:
    """Compatibility wrapper returning only accepted de-identified texts."""
    return sanitize_review_samples_with_rejections(value, limit=limit)[0]


def enforce_review_summary_invariants(value: Any) -> dict[str, Any]:
    summary = dict(value) if isinstance(value, dict) else {}
    samples, rejected = sanitize_review_samples_with_rejections(summary.get("review_samples"))
    summary["review_samples"] = samples
    summary["review_sample_count"] = len(samples)
    existing_rejections = summary.get("review_sample_rejections")
    summary["review_sample_rejections"] = (
        list(existing_rejections) if isinstance(existing_rejections, list) else []
    ) + rejected
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
