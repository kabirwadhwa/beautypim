"""Single canonical selector for exact-product review evidence.

Every presentation and synthesis surface consumes this result; none may pick
an arbitrary first observation independently.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.models import Brand, CanonicalProduct, Category, ImportJob, ProductVariant, ScrapedProductObservation, SourceListing


def _json_safe(value: Any) -> Any:
    """Return values safe for JSON/JSONB persistence and API serialization."""
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _integer(value: Any) -> int:
    try:
        return int(float(str(value or 0).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def classify_review_quality(rating: Any, count: Any) -> str:
    """Central business-usefulness classification; raw evidence is retained."""
    total = _integer(count)
    has_rating = rating not in (None, "")
    if not has_rating or total <= 1:
        return "insufficient"
    if total >= 50:
        return "strong"
    if total >= 10:
        return "moderate"
    return "weak"


def _with_quality(result: dict[str, Any]) -> dict[str, Any]:
    quality = classify_review_quality(result.get("average_rating"), result.get("review_count"))
    samples = _integer(result.get("review_sample_count"))
    source_count = _integer(result.get("review_source_count") or result.get("source_count"))
    intelligence = "strong" if samples >= 20 and source_count >= 2 else "moderate" if samples >= 5 else "limited" if samples else "insufficient"
    return {
        **result,
        "review_quality": quality,
        "aggregate_strength": quality,
        "review_intelligence_strength": intelligence,
        "rating_available": result.get("average_rating") not in (None, ""),
        "business_display_rating": quality != "insufficient",
    }


def _quality_rank(rating: Any, count: Any) -> int:
    return {"insufficient": 0, "weak": 1, "moderate": 2, "strong": 3}[
        classify_review_quality(rating, count)
    ]


def _summary_or_aggregate_fallback(summary: dict[str, Any] | None, rating: Any, count: Any) -> dict[str, Any] | None:
    """Normalize aggregate metadata without synthesizing absent review text."""
    from app.services.review_evidence import enforce_review_summary_invariants
    result = dict(summary or {})
    numeric_rating = None
    try:
        numeric_rating = float(rating) if rating not in (None, "") else None
    except (TypeError, ValueError):
        pass
    review_count = _integer(count)
    if numeric_rating is None and not review_count and not result:
        return None
    result.update({"average_rating": numeric_rating, "review_count": review_count or None})
    return enforce_review_summary_invariants(result)


def select_review_aggregate(db, product_id) -> dict[str, Any] | None:
    from app.models import FieldValue
    saved_summary = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product_id,
        FieldValue.field_name == "review_summary", FieldValue.is_current == True,
    ).first()
    rows = db.query(ScrapedProductObservation).filter(
        ScrapedProductObservation.canonical_product_id == product_id,
        ScrapedProductObservation.match_status == "matched",
    ).order_by(ScrapedProductObservation.scraped_at.desc()).limit(100).all()
    candidates = []
    for row in rows:
        payload = row.normalized_payload or {}
        summary = payload.get("review_summary") if isinstance(payload.get("review_summary"), dict) else {}
        # First-class normalized review samples are authoritative. Retain the
        # nested fallback only for observations created before this schema.
        top_level_samples = payload.get("review_samples")
        if isinstance(top_level_samples, list):
            summary = {**summary, "review_samples": top_level_samples}
        rating = summary.get("average_rating", payload.get("rating"))
        count = summary.get("review_count", payload.get("review_count"))
        if rating in (None, "") and count in (None, "") and not summary:
            continue
        # Attached variant evidence outranks product-level evidence. Conflicts
        # remain visible but rank below accepted exact matches.
        scope = "exact_variant" if row.product_variant_id else "exact_product"
        score = (
            2 if row.match_status == "matched" else 1,
            2 if scope == "exact_variant" else 1,
            _quality_rank(rating, count),
            1 if summary.get("ai_summary_lines") or summary.get("ai_summary_text") else 0,
            _integer(summary.get("review_sample_count")),
            _integer(count),
            row.scraped_at,
        )
        summary = _summary_or_aggregate_fallback(summary, rating, count) or {}
        candidates.append((score, row, payload, summary, rating, count, scope))
    # Licensed search may expose a cited exact-product aggregate even when the
    # retailer blocks HTML crawling. It is stored as field-level source
    # evidence and participates in this same canonical selector.
    rating_field = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product_id,
        FieldValue.field_name == "rating", FieldValue.is_current == True,
    ).first()
    count_field = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product_id,
        FieldValue.field_name == "review_count", FieldValue.is_current == True,
    ).first()
    if rating_field or count_field:
        rating = rating_field.value if rating_field else None
        count = count_field.value if count_field else None
        source_field = count_field or rating_field
        field_rows = [row for row in (rating_field, count_field) if row]
        evidence_rows = [entry for row in field_rows for entry in (row.evidence or []) if isinstance(entry, dict)]
        accepted_field_evidence = bool(evidence_rows) and all(
            entry.get("match_scope") in {"exact_gtin", "exact_resolved_identity", "exact_product", "exact_variant"}
            and entry.get("evidence_type") in {"licensed_web_search_market_observation", "licensed_search_market_observation"}
            for entry in evidence_rows
        )
        if not accepted_field_evidence:
            rating_field = count_field = None
    if rating_field or count_field:
        rating = rating_field.value if rating_field else None
        count = count_field.value if count_field else None
        source_field = count_field or rating_field
        source_url = source_field.source_reference if source_field else None
        summary = {}
        summary = _summary_or_aggregate_fallback(summary, rating, count)
        result = {
            "average_rating": float(rating) if rating not in (None, "") else None,
            "review_count": _integer(count) if count not in (None, "") else None,
            "review_summary": summary, "source": "Web Research",
            "source_domain": urlparse(source_url).hostname if source_url else None,
            "observation_date": source_field.created_at if source_field else None,
            "match_scope": "exact_product",
            "evidence_reference": source_url or f"field_value:{source_field.id}",
            "observation_id": None,
        }
        candidates.append(((2, 1, _quality_rank(rating, count), 1 if summary else 0, 0, _integer(count), source_field.created_at), None, result, {}, rating, count, "exact_product"))
    # Explicit customer-feed aggregates are exact-product evidence too. They
    # participate in the same ranking instead of being selected by a UI loop.
    listings = db.query(SourceListing).filter(
        SourceListing.canonical_product_id == product_id, SourceListing.is_deleted == False,
        SourceListing.import_job_id.isnot(None), SourceListing.crawl_job_id.is_(None),
    ).order_by(SourceListing.created_at.desc()).limit(30).all()
    for listing in listings:
        job = db.query(ImportJob).filter(ImportJob.id == listing.import_job_id).first() if listing.import_job_id else None
        raw, mapping = listing.raw_data or {}, (job.column_mapping or {}) if job else {}
        def mapped(name):
            column = mapping.get(name)
            return raw.get(column) if column else raw.get(name)
        rating, count, summary = mapped("rating"), mapped("review_count"), mapped("review_summary")
        if rating in (None, "") and count in (None, "") and not summary:
            continue
        summary = summary if isinstance(summary, dict) else ({"summary": summary} if summary else {})
        summary = _summary_or_aggregate_fallback(summary, rating, count) or {}
        result = {
            "average_rating": float(rating) if rating not in (None, "") else None,
            "review_count": _integer(count) if count not in (None, "") else None,
            "review_summary": summary or None, "source": listing.retailer or (job.source_name if job else None),
            "source_domain": urlparse(listing.source_url).hostname if listing.source_url else None,
            "observation_date": listing.created_at, "match_scope": "exact_product",
            "evidence_reference": f"source_listing:{listing.id}", "observation_id": None,
        }
        candidates.append(((2, 1, _quality_rank(rating, count), 1 if summary else 0, 0, _integer(count), listing.created_at), None, result, {}, rating, count, "exact_product"))
    # Imported corpus review aggregates participate only on an exact identity.
    product = db.query(CanonicalProduct).filter(CanonicalProduct.id == product_id).first()
    variant = db.query(ProductVariant).filter(
        ProductVariant.canonical_product_id == product_id, ProductVariant.is_deleted == False,
    ).order_by(ProductVariant.created_at.desc()).first()
    if product and variant:
        brand = db.query(Brand).filter(Brand.id == product.brand_id).first() if product.brand_id else None
        category = db.query(Category).filter(Category.id == product.category_id).first() if product.category_id else None
        from app.knowledge_corpus.retrieval import retrieve_corpus_evidence, resolve_exact_field_evidence
        corpus = resolve_exact_field_evidence(retrieve_corpus_evidence(
            db, gtin=variant.gtin or "", brand=brand.name if brand else "",
            product_name=product.product_name, size=f"{variant.size or ''} {variant.unit or ''}".strip(),
            category=category.path if category else "",
        ))
        values = corpus.get("values") or {}
        rating, count, summary = values.get("rating"), values.get("review_count"), values.get("review_summary")
        if rating not in (None, "") or count not in (None, "") or summary:
            summary = summary if isinstance(summary, dict) else ({"summary": summary} if summary else {})
            result = {
                "average_rating": float(rating) if rating not in (None, "") else None,
                "review_count": _integer(count) if count not in (None, "") else None,
                "review_summary": _summary_or_aggregate_fallback(summary, rating, count),
                "source": "Retail Data", "source_domain": None,
                "observation_date": None, "match_scope": "exact_variant",
                "evidence_reference": "knowledge_corpus:exact_product", "observation_id": None,
            }
            candidates.append(((2, 2, _quality_rank(rating, count), 1 if summary else 0, 0, _integer(count), product.updated_at), None, result, {}, rating, count, "exact_variant"))
    if not candidates:
        return None

    # Build one canonical, exact-identity Review Intelligence object.  Every
    # downstream surface consumes this result rather than independently
    # choosing one convenient retailer row.
    entries: list[dict[str, Any]] = []
    for _, row, payload, summary, rating, count, scope in candidates:
        if row is None:
            source = payload.get("source")
            domain = payload.get("source_domain")
            observed = payload.get("observation_date")
            reference = payload.get("evidence_reference")
        else:
            source = row.source_name
            domain = row.source_domain or (urlparse(row.source_url).hostname if row.source_url else None)
            observed = row.scraped_at
            reference = f"scraped_product_observation:{row.id}"
        domain = str(domain or "").lower().removeprefix("www.") or None
        entries.append({
            "rating": float(rating) if rating not in (None, "") else None,
            "count": _integer(count) if count not in (None, "") else 0,
            "summary": summary or {}, "source": source, "domain": domain,
            "observed_at": observed, "reference": reference, "scope": scope,
            "observation_id": row.id if row else None,
        })

    # Suppress syndicated duplicates without destroying independent evidence.
    unique_aggregates: dict[tuple, dict] = {}
    for entry in entries:
        key = (entry["domain"] or entry["source"] or entry["reference"], entry["rating"], entry["count"])
        previous = unique_aggregates.get(key)
        if not previous or str(entry.get("observed_at") or "") > str(previous.get("observed_at") or ""):
            unique_aggregates[key] = entry
    aggregate_rows = list(unique_aggregates.values())
    weighted = [(entry["rating"], max(entry["count"], 1)) for entry in aggregate_rows if entry["rating"] is not None]
    average = (sum(rating * weight for rating, weight in weighted) / sum(weight for _, weight in weighted)) if weighted else None
    represented = sum(entry["count"] for entry in aggregate_rows)

    samples: list[dict[str, Any]] = []
    seen_text: set[str] = set()
    per_source: dict[str, int] = {}
    for entry in sorted(entries, key=lambda value: (value["count"], str(value.get("observed_at") or "")), reverse=True):
        source_key = entry["domain"] or entry["source"] or "unknown"
        for sample in entry["summary"].get("review_samples") or []:
            if not isinstance(sample, dict):
                continue
            text = " ".join(str(sample.get("text") or "").split())[:4000]
            normalized = "".join(char for char in text.lower() if char.isalnum())
            if len(text) < 20 or not normalized or normalized in seen_text or per_source.get(source_key, 0) >= 25:
                continue
            seen_text.add(normalized)
            per_source[source_key] = per_source.get(source_key, 0) + 1
            samples.append({
                "text": text, "title": sample.get("title"), "rating": sample.get("rating"),
                "date": sample.get("date"), "locale": sample.get("locale"),
                "verified_purchase": sample.get("verified_purchase"),
                "source_domain": sample.get("source_domain") or entry["domain"],
                "source_url": sample.get("source_url"),
                "observation_date": _json_safe(entry.get("observed_at")), "match_scope": entry.get("scope"),
            })
            if len(samples) >= 100:
                break
        if len(samples) >= 100:
            break

    sources = []
    seen_sources = set()
    for entry in aggregate_rows:
        source_key = entry["domain"] or entry["source"] or entry["reference"]
        if source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        sources.append({
            "name": entry["source"], "domain": entry["domain"], "observation_date": _json_safe(entry["observed_at"]),
            "review_count": entry["count"] or None, "average_rating": entry["rating"],
            "evidence_reference": entry["reference"], "match_scope": entry["scope"],
        })
    source_count = len(sources)
    if len(samples) >= 20 and source_count >= 2:
        strength = "strong"
    elif len(samples) >= 5:
        strength = "moderate"
    elif samples:
        strength = "limited"
    elif average is not None or represented:
        strength = "aggregate_only"
    else:
        strength = "none"

    best = max(entries, key=lambda entry: (_quality_rank(entry["rating"], entry["count"]), entry["count"], str(entry.get("observed_at") or "")))
    from app.services.review_evidence import enforce_review_summary_invariants
    summary = enforce_review_summary_invariants(best.get("summary") or {})
    if (saved_summary and saved_summary.source_type == "ai_inference"
            and isinstance(saved_summary.value, dict)):
        # Reuse only synthesized intelligence. Never let an older source-data
        # summary replace the newly combined exact-source review samples.
        for key in (
            "ai_summary_text", "summary", "positive_themes", "negative_themes",
            "mixed_themes", "summary_model", "evidence_limitation",
        ):
            if key in saved_summary.value:
                summary[key] = saved_summary.value[key]
    summary.update({
        "average_rating": round(average, 3) if average is not None else None,
        "review_count": represented or None,
        "represented_review_count": represented or None,
        "review_source_count": source_count,
        "source_count": source_count,
        "review_sample_count": len(samples),
        "review_samples": samples,
        "evidence_strength": strength,
        "sources": sources,
        "source_breakdown": sources,
    })
    if not samples:
        summary = enforce_review_summary_invariants(summary)
        summary["evidence_limitation"] = (
            "Aggregate rating and count evidence is available, but review-text evidence was insufficient for a detailed summary."
            if average is not None or represented else "No reliable review intelligence available yet."
        )
    return _with_quality({
        "average_rating": round(average, 3) if average is not None else None,
        "review_count": represented or None,
        "represented_review_count": represented or None,
        "review_source_count": source_count,
        "source_count": source_count,
        "review_sample_count": len(samples),
        "evidence_strength": strength,
        "aggregate_strength": classify_review_quality(average, represented),
        "review_intelligence_strength": "strong" if len(samples) >= 20 and source_count >= 2 else "moderate" if len(samples) >= 5 else "limited" if samples else "insufficient",
        "sources": sources,
        "source_breakdown": sources,
        "review_summary": _json_safe(summary),
        "source": best["source"], "source_domain": best["domain"],
        "observation_date": _json_safe(best["observed_at"]), "match_scope": best["scope"],
        "evidence_reference": best["reference"], "observation_id": best["observation_id"],
    })
