"""Durable, non-blocking Improve Product research worker.

The OpenAI response ID is persisted inside CrawlJob.configuration before polling,
so a process restart resumes the same paid request instead of creating another.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse

from app.database import SessionLocal
from app.config import settings
from app.models import (
    CanonicalProduct, Category, CrawlJob, FieldValue, Formulation, ImportJob, ImportJobItem,
    ProductVariant, SourceListing, User,
)

logger = logging.getLogger("app.product_research_worker")
RESEARCH_DOMAIN = "product-research.internal"
ACTIVE_STATUSES = {"queued", "discovering", "crawling", "parsing"}


def _enforce_discovery_exclusions(discovery: dict, source_memory: dict) -> tuple[dict, list[dict]]:
    from app.services.research_evidence import normalize_research_url
    excluded_urls = set(source_memory.get("attempted_urls") or [])
    excluded_domains = set(source_memory.get("blocked_domains") or [])
    rejected = []

    def allowed(row: dict, url_key: str) -> bool:
        url = str(row.get(url_key) or "")
        normalized = normalize_research_url(url)
        domain = (urlparse(normalized).hostname or "").lower()
        reason = "excluded_url" if normalized in excluded_urls else "blocked_domain" if domain in excluded_domains else None
        if reason:
            rejected.append({"url": url, "domain": domain, "reason": reason})
            return False
        return True

    return {
        **discovery,
        "candidates": [row for row in (discovery.get("candidates") or []) if isinstance(row, dict) and allowed(row, "url")],
        "evidence_claims": [row for row in (discovery.get("evidence_claims") or []) if isinstance(row, dict) and allowed(row, "source_url")],
        "market_observations": [row for row in (discovery.get("market_observations") or []) if isinstance(row, dict) and allowed(row, "source_url")],
    }, rejected


def _persist_discovery_evidence(db, job, product, variant, discovery: dict,
                                requested_fields: list[str]) -> dict:
    """Validate and persist cited objective-specific licensed-search claims."""
    from app.services.research_evidence import EvidenceItem, persist_evidence_item, validate_evidence
    from app.services.image_urls import normalize_public_image_url
    accepted, rejected, resolved = [], [], []
    requested = set(requested_fields or [])
    aliases = {"ingredients": "inci", "ingredient_text_raw": "inci", "image_url": "image"}
    for raw in discovery.get("evidence_claims") or []:
        if not isinstance(raw, dict):
            continue
        field_name = aliases.get(str(raw.get("field_name") or "").strip(), str(raw.get("field_name") or "").strip())
        if field_name not in requested and not (field_name == "inci" and "ingredients" in requested):
            rejected.append({"field": field_name, "url": raw.get("source_url"), "reason": "not_requested"})
            continue
        item = EvidenceItem(
            field_name=field_name, proposed_value=raw.get("proposed_value"),
            source_url=str(raw.get("source_url") or ""), source_domain=str(raw.get("source_domain") or ""),
            source_title=raw.get("source_title"), acquisition_method="licensed_web_search",
            source_authority=str(raw.get("source_authority") or "retailer"),
            matched_gtin=raw.get("matched_gtin"), matched_brand=raw.get("matched_brand"),
            matched_product_name=raw.get("matched_product_name"), matched_product_family=raw.get("matched_product_family"),
            matched_variant=raw.get("matched_variant"), matched_concentration=raw.get("matched_concentration"),
            matched_shade=raw.get("matched_shade"), matched_size=raw.get("matched_size"),
            evidence_excerpt=raw.get("evidence_excerpt"),
            confidence_inputs={"provider": discovery.get("provider"), "response_id": discovery.get("response_id")},
        )
        if field_name == "image":
            valid, reason = validate_evidence(item, product, variant)
            image = normalize_public_image_url(item.proposed_value) if valid else None
            if valid and image:
                if not product.image_url:
                    product.image_url = image
                accepted.append(item.payload()); resolved.append("image")
            else:
                rejected.append({"field": field_name, "url": item.source_url, "reason": reason if not valid else "invalid_image_url"})
            continue
        if field_name == "inci":
            valid, reason = validate_evidence(item, product, variant)
            raw_inci = str(item.proposed_value or "").strip()
            if valid and raw_inci:
                from app.services.formulation_resolution import promote_formulation
                result = promote_formulation(
                    db, product=product, variant=variant, raw_inci_text=raw_inci,
                    source_kind="licensed_web_search",
                    source_reference=f"licensed_web_search:{item.source_url}",
                )
                if result.status in {"applied", "unchanged"}:
                    accepted.append(item.payload()); resolved.append("inci")
                else:
                    rejected.append({"field": field_name, "url": item.source_url,
                                     "reason": result.reason or result.status})
            else:
                rejected.append({"field": field_name, "url": item.source_url, "reason": reason})
            continue
        ok, reason = persist_evidence_item(db, job, product, variant, item)
        (accepted if ok else rejected).append(item.payload() if ok else {
            "field": field_name, "url": item.source_url, "reason": reason,
        })
        if ok:
            resolved.append(field_name)
    db.flush()
    return {"accepted": accepted, "rejected": rejected, "fields_resolved": sorted(set(resolved))}


def _persist_discovery_market_evidence(
    db, product, discovery: dict, *, expected_gtin: str = "",
) -> dict:
    """Persist exact-product image and review aggregates exposed by search.

    These are market observations only. They never authorize formulation,
    claims, price, GTIN, concentration, shade, or other variant facts.
    """
    from app.models import FieldValue
    from app.services.image_urls import normalize_public_image_url
    from app.services.research_evidence import EvidenceItem

    observations = discovery.get("market_observations") or []
    if not isinstance(observations, list):
        return {"image_found": False, "review_evidence_found": False}
    existing_gtins = {
        re.sub(r"\D", "", row.gtin or "")
        for row in db.query(ProductVariant).filter(
            ProductVariant.canonical_product_id == product.id,
            ProductVariant.is_deleted == False,
            ProductVariant.gtin.isnot(None),
        ).all()
        if re.sub(r"\D", "", row.gtin or "")
    }
    normalized_expected_gtin = re.sub(r"\D", "", expected_gtin or "")
    if normalized_expected_gtin:
        existing_gtins = {normalized_expected_gtin}

    understanding_row = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product.id,
        FieldValue.field_name == "product_understanding", FieldValue.is_current == True,
    ).first()
    understanding = understanding_row.value if understanding_row and isinstance(understanding_row.value, dict) else {}
    contract_identity = understanding.get("identity") if isinstance(understanding.get("identity"), dict) else {}
    resolved_brand = ((contract_identity.get("consumer_brand") or {}).get("value")
                      if isinstance(contract_identity.get("consumer_brand"), dict) else None)
    resolved_family = ((contract_identity.get("product_family") or {}).get("value")
                       if isinstance(contract_identity.get("product_family"), dict) else None)
    contract_taxonomy = understanding.get("taxonomy") if isinstance(understanding.get("taxonomy"), dict) else {}
    resolved_format = ((contract_taxonomy.get("product_type") or {}).get("value")
                       if isinstance(contract_taxonomy.get("product_type"), dict) else None)

    def exact_identity(observation: dict) -> bool:
        """Market evidence must prove the identity it is being attached to."""
        observed_gtin = re.sub(r"\D", "", str(observation.get("matched_gtin") or ""))
        if existing_gtins:
            if observed_gtin:
                return observed_gtin in existing_gtins
            # Level B exact resolved identity may support market observations
            # only.  It never authorizes formulation, claims or variant facts.
            if understanding.get("identity_status") != "resolved":
                return False
        target_brand_value = resolved_brand or (product.brand.name if product.brand else "")
        target_name_value = resolved_family or product.product_name
        target_brand = re.sub(r"[^a-z0-9]", "", str(target_brand_value).lower())
        observed_brand = re.sub(r"[^a-z0-9]", "", str(observation.get("matched_brand") or "").lower())
        target_tokens = set(re.findall(r"[a-z0-9]+", str(target_name_value).lower()))
        observed_tokens = set(re.findall(r"[a-z0-9]+", str(observation.get("matched_product_name") or "").lower()))
        generic = {"eau", "de", "parfum", "toilette", "edp", "edt", "spray", "perfume", "fragrance", "ml", "oz"}
        generic.update(re.findall(r"[a-z0-9]+", str(observation.get("matched_brand") or "").lower()))
        extra = observed_tokens - target_tokens - generic
        variant_text = str(observation.get("matched_variant") or "").lower()
        expected_text = f"{target_name_value} {resolved_format or ''}".lower()
        conflicting_format = bool(resolved_format) and any(
            value in variant_text and value not in expected_text
            for value in ("eau de toilette", "eau de parfum", "parfum", "elixir")
        )
        return bool(target_brand and target_brand == observed_brand and target_tokens
                    and target_tokens <= observed_tokens and not extra and not conflicting_format)

    observations = [row for row in observations if isinstance(row, dict) and exact_identity(row)]
    image_found = False
    if not product.image_url:
        for observation in observations:
            image = normalize_public_image_url(observation.get("image_url"))
            image_item = EvidenceItem(
                field_name="image", proposed_value=image, source_url=str(observation.get("source_url") or ""),
                source_domain=str(observation.get("source_domain") or ""), acquisition_method="licensed_web_search",
                source_authority="retailer", source_title=observation.get("source_name"),
                matched_gtin=observation.get("matched_gtin"), matched_brand=observation.get("matched_brand"),
                matched_product_name=observation.get("matched_product_name"), matched_variant=observation.get("matched_variant"),
                evidence_excerpt=observation.get("evidence_excerpt"),
                identity_scope="exact_gtin" if observation.get("matched_gtin") else "exact_resolved_identity",
            )
            if image:
                product.image_url = image
                image_found = True
                break
    else:
        image_found = True

    # Prefer the largest exact-product aggregate because it is generally the
    # most stable customer signal. The source URL remains attached as evidence.
    review_rows = [row for row in observations if row.get("average_rating") is not None or row.get("review_count")]
    review = max(review_rows, key=lambda row: int(row.get("review_count") or 0), default=None)
    if review:
        for field_name, value in (
            ("rating", review.get("average_rating")),
            ("review_count", review.get("review_count")),
        ):
            if value is None:
                continue
            item = EvidenceItem(
                field_name=field_name, proposed_value=value,
                source_url=str(review.get("source_url") or ""), source_domain=str(review.get("source_domain") or ""),
                acquisition_method="licensed_web_search", source_authority="retailer",
                source_title=review.get("source_name"), matched_gtin=review.get("matched_gtin"),
                matched_brand=review.get("matched_brand"), matched_product_name=review.get("matched_product_name"),
                matched_variant=review.get("matched_variant"), evidence_excerpt=review.get("evidence_excerpt"),
                identity_scope="exact_gtin" if review.get("matched_gtin") else "exact_resolved_identity",
            )
            current = db.query(FieldValue).filter(
                FieldValue.canonical_product_id == product.id,
                FieldValue.field_name == field_name,
                FieldValue.is_current == True,
            ).first()
            if current and current.source_type in {"ai_inference", "web_research"}:
                current.is_current = False
                db.flush()
                current = None
            if not current:
                db.add(FieldValue(
                    canonical_product_id=product.id, field_name=field_name,
                    value=value, source_type="source_data",
                    source_reference=review.get("source_url"), confidence_score=0.95,
                    review_status="inferred", is_current=True, evidence=[item.payload()],
                    reasoning_summary="Exact-product aggregate retained from cited web-search evidence.",
                    semantic_status="source_supported", semantic_status_type="market_observation",
                ))
    db.flush()
    return {"image_found": image_found, "review_evidence_found": bool(review)}


def _assign_configuration(job: CrawlJob, **updates) -> dict:
    configuration = {**(job.configuration or {}), **updates}
    job.configuration = configuration
    return configuration


def _research_snapshot(db, product) -> dict:
    from app.services.product_improvement import product_improvement_summary
    from app.services.research_reliability import snapshot_metrics
    from app.services.review_aggregate import select_review_aggregate
    current = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product.id, FieldValue.is_current == True,
    ).all()
    return snapshot_metrics(
        product_improvement_summary(db, product),
        field_values={row.field_name: row.value for row in current},
        image_present=bool(product.image_url),
        review=select_review_aggregate(db, product.id),
        formulation_count=db.query(Formulation).filter(
            Formulation.canonical_product_id == product.id, Formulation.is_deleted == False,
        ).count(),
    )


def recover_product_research_jobs(db) -> int:
    jobs = db.query(CrawlJob).filter(
        CrawlJob.domain == RESEARCH_DOMAIN,
        CrawlJob.status.in_(["discovering", "crawling", "parsing"]),
    ).all()
    for job in jobs:
        job.status = "queued"
        job.error_summary = "Recovered the same background research request after restart."
    if jobs:
        db.commit()
    return len(jobs)


def _claim_job(db) -> CrawlJob | None:
    # Read a bounded durable frontier and prioritize direct product actions
    # over bulk backlog. The row is then claimed under a PostgreSQL lock, so
    # several worker threads cannot process the same paid request.
    candidates = db.query(CrawlJob).filter(
        CrawlJob.domain == RESEARCH_DOMAIN,
        CrawlJob.status == "queued",
    ).order_by(CrawlJob.created_at).limit(250).all()
    if not candidates:
        return None
    target = max(
        candidates,
        key=lambda row: (int((row.configuration or {}).get("research_priority") or 0), -row.created_at.timestamp()),
    )
    query = db.query(CrawlJob).filter(
        CrawlJob.id == target.id, CrawlJob.status == "queued",
    )
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    job = query.first()
    if job:
        job.status = "discovering"
        job.started_at = job.started_at or datetime.utcnow()
        job.heartbeat_at = datetime.utcnow()
        job.error_summary = None
        db.commit()
        db.refresh(job)
    return job


def run_product_research_job(job_id: uuid.UUID, stop_event: threading.Event | None = None) -> None:
    from app.routes.products import _automatic_product_research, _product_expected_format
    from app.services.web_discovery import (
        SearchRateLimited, poll_product_source_discovery, start_product_source_discovery,
    )
    from app.worker import process_item_enrichment
    from app.services.product_research_logging import product_research_log

    db = SessionLocal()
    try:
        job = db.query(CrawlJob).filter(CrawlJob.id == job_id).first()
        if not job or job.domain != RESEARCH_DOMAIN or job.status == "cancelled":
            return
        configuration = job.configuration or {}
        product = db.query(CanonicalProduct).filter(
            CanonicalProduct.id == configuration.get("research_product_id"),
            CanonicalProduct.is_deleted == False,
        ).first()
        user = db.query(User).filter(User.id == job.requested_by_id).first()
        item = db.query(ImportJobItem).filter(
            ImportJobItem.id == configuration.get("research_item_id"),
        ).first()
        if not product or not user or not item:
            raise RuntimeError("The product, user or source record for background research no longer exists.")

        before_metrics = configuration.get("before_metrics") or _research_snapshot(db, product)
        _assign_configuration(job, before_metrics=before_metrics)
        db.commit()

        from app.services.product_identity import preferred_product_variant
        requested_variant_id = configuration.get("research_variant_id") or item.product_variant_id
        variant = db.query(ProductVariant).filter(
            ProductVariant.id == requested_variant_id,
            ProductVariant.canonical_product_id == product.id,
            ProductVariant.is_deleted == False,
        ).first() if requested_variant_id else None
        variant = variant or preferred_product_variant(db, product.id)
        log_context = {
            "job_id": str(job.id), "product_id": str(product.id),
            "gtin": variant.gtin if variant and variant.gtin else None,
            "product_name": product.product_name,
        }
        product_research_log("job_started", **log_context, technical_status=job.status)
        source_listing = db.query(SourceListing).filter(SourceListing.id == item.source_listing_id).first()
        from app.services.product_improvement import product_improvement_summary
        from app.services.research_reliability import build_identity_query_plan
        from app.knowledge_corpus.retrieval import retrieve_corpus_evidence
        from app.services.formulation_resolution import (
            promote_exact_corpus_formulation, synchronize_current_source_formulation,
        )
        category = db.query(Category).filter(Category.id == product.category_id).first() if product.category_id else None
        source_resolution = synchronize_current_source_formulation(db, product, variant)
        corpus_result = retrieve_corpus_evidence(
            db, gtin=variant.gtin if variant else "",
            brand=product.brand.name if product.brand else "",
            product_name=product.product_name,
            category=category.path if category else "",
        )
        corpus_resolution = promote_exact_corpus_formulation(db, product, variant, corpus_result)
        db.flush()
        product_research_log(
            "internal_formulation_resolution", **log_context,
            source_status=source_resolution.status, source_reason=source_resolution.reason,
            corpus_status=corpus_resolution.status, corpus_reason=corpus_resolution.reason,
        )
        initial_quality = product_improvement_summary(db, product)
        identity_only = bool(initial_quality.get("identity_review_required"))
        identity_queries = configuration.get("identity_queries") or build_identity_query_plan(
            brand=product.brand.name if product.brand else "",
            product_name=product.product_name,
            product_format=_product_expected_format(db, product),
            gtin=variant.gtin if variant and variant.gtin else "",
            raw_data=source_listing.raw_data if source_listing else {},
            category=initial_quality.get("category") or "",
            corpus_candidates=initial_quality.get("candidate_products") or [],
        )
        _assign_configuration(job, identity_queries=identity_queries)
        configured_objectives = set(configuration.get("research_objectives") or [])
        recalculated_objectives = [
            entry["field"] for entry in (initial_quality.get("research_objectives") or [])
        ]
        research_objectives = [
            field for field in recalculated_objectives
            if not configured_objectives or field in configured_objectives
        ]
        from app.services.research_reliability import blank_source_memory
        source_memory = {**blank_source_memory(), **(configuration.get("research_state") or {})}
        if identity_only:
            # Foundational research is operationally separate. Market evidence,
            # claims, formulation and commercial synthesis wait for resolution.
            safe = {"consumer_brand", "product_family", "variant", "category", "subcategory", "product_type"}
            research_objectives = [field for field in research_objectives if field in safe]
        product_research_log(
            "research_plan", **log_context,
            phase=configuration.get("research_phase") or initial_quality.get("research_phase"),
            objectives=research_objectives,
            identity_status=(initial_quality.get("product_understanding") or {}).get("identity_status"),
            taxonomy_status=(initial_quality.get("product_understanding") or {}).get("taxonomy_status"),
        )
        if not research_objectives:
            after_metrics = _research_snapshot(db, product)
            result = {
                "web_search_skipped": True,
                "reason": "Source/customer and exact internal evidence resolved the requested gaps.",
                "fields_resolved_from_internal_evidence": ["inci"] if (
                    source_resolution.formulation or corpus_resolution.formulation
                ) else [],
                "before_completeness": before_metrics.get("overall_completeness"),
                "after_completeness": after_metrics.get("overall_completeness"),
                "remaining_important_gaps": initial_quality.get("missing_high_priority_fields") or [],
                "business_outcome": "improved",
            }
            job.status = "completed"
            job.completed_at = datetime.utcnow()
            _assign_configuration(job, result=result, after_metrics=after_metrics)
            db.commit()
            product_research_log("job_finished", **log_context, business_outcome="improved",
                                 external_provider_calls=0, crawl_attempts=0)
            return
        for index, query in enumerate(identity_queries, 1):
            product_research_log("search_query_attempted", **log_context, attempt=index, query=query)
        discovery = configuration.get("discovery")
        if not discovery:
            if int(source_memory.get("provider_searches") or 0) >= int(settings.WEB_RESEARCH_MAX_PROVIDER_SEARCHES):
                raise RuntimeError("Provider-search budget exhausted before discovery could start.")
            discovery = start_product_source_discovery(
                brand=product.brand.name if product.brand else "",
                product_name=product.product_name,
                product_format=_product_expected_format(db, product),
                gtin=variant.gtin if variant and variant.gtin else "",
                approved_domains=[],
                research_objectives=research_objectives,
                identity_queries=identity_queries,
            )
            _assign_configuration(job, discovery=discovery)
            source_memory["provider_searches"] = int(source_memory.get("provider_searches") or 0) + 1
            _assign_configuration(job, research_state=source_memory)
            job.heartbeat_at = datetime.utcnow()
            db.commit()
            product_research_log(
                "search_started", **log_context, provider=discovery.get("provider"),
                response_id=discovery.get("response_id"), objectives=research_objectives,
            )

        rate_limit_retries = int(configuration.get("rate_limit_retries") or 0)
        retry_delays = list(configuration.get("retry_delays") or discovery.get("retry_delays") or [])
        while discovery.get("status") in {"queued", "in_progress"}:
            if stop_event and stop_event.is_set():
                # Leave the persisted provider response ID available for startup recovery.
                return
            try:
                discovery = poll_product_source_discovery(discovery)
            except SearchRateLimited as exc:
                if rate_limit_retries >= int(settings.OPENAI_WEB_RESEARCH_MAX_RETRIES):
                    raise
                rate_limit_retries += 1
                delay = max(
                    exc.retry_after,
                    settings.OPENAI_WEB_RESEARCH_BACKOFF_SECONDS * (2 ** (rate_limit_retries - 1)),
                )
                retry_delays.append(round(delay, 3))
                product_research_log(
                    "provider_rate_limited", level=logging.WARNING, **log_context,
                    retry=rate_limit_retries, retry_after_seconds=round(delay, 3),
                    restart_required=exc.restart_required,
                )
                _assign_configuration(
                    job, rate_limit_retries=rate_limit_retries,
                    retry_delays=retry_delays, waiting_for_rate_limit=True,
                )
                job.heartbeat_at = datetime.utcnow()
                db.commit()
                time.sleep(delay)
                if exc.restart_required:
                    if int(source_memory.get("provider_searches") or 0) >= int(settings.WEB_RESEARCH_MAX_PROVIDER_SEARCHES):
                        raise RuntimeError("Provider-search budget exhausted after a terminal rate limit.")
                    discovery = start_product_source_discovery(
                        brand=product.brand.name if product.brand else "",
                        product_name=product.product_name,
                        product_format=_product_expected_format(db, product),
                        gtin=variant.gtin if variant and variant.gtin else "",
                        approved_domains=[], research_objectives=research_objectives,
                        identity_queries=identity_queries,
                    )
                    source_memory["provider_searches"] = int(source_memory.get("provider_searches") or 0) + 1
                _assign_configuration(job, discovery=discovery, waiting_for_rate_limit=False)
                db.commit()
                continue
            _assign_configuration(job, discovery=discovery)
            job.heartbeat_at = datetime.utcnow()
            db.commit()
            if discovery.get("status") in {"queued", "in_progress"}:
                time.sleep(2)

        product_research_log(
            "search_discovery_result", **log_context, status=discovery.get("status"),
            candidate_count=len(discovery.get("candidates") or []),
            candidate_domains=sorted({str(row.get("domain") or "") for row in (discovery.get("candidates") or []) if isinstance(row, dict)}),
            provider_attempts=discovery.get("provider_attempts"),
            error=discovery.get("error"),
        )

        evidence_result = _persist_discovery_evidence(
            db, job, product, variant, discovery, research_objectives,
        )
        product_research_log(
            "search_evidence_resolution", **log_context,
            accepted=len(evidence_result["accepted"]), rejected=len(evidence_result["rejected"]),
            rejection_reasons=[row.get("reason") for row in evidence_result["rejected"]],
            fields_resolved=evidence_result["fields_resolved"], acquisition_method="licensed_web_search",
        )

        discovery_market = ({"image_found": False, "review_evidence_found": False} if identity_only else
            _persist_discovery_market_evidence(
                db, product, discovery, expected_gtin=variant.gtin if variant else "",
            ))
        db.commit()

        job.status = "crawling"
        job.heartbeat_at = datetime.utcnow()
        db.commit()
        result = _automatic_product_research(
            db, product, user, candidates=discovery.get("candidates") or [],
            research_objectives=research_objectives,
            research_variant_id=variant.id if variant else None,
            identity_only=identity_only,
            research_log_context=log_context,
            research_state=source_memory,
        )
        source_memory = result.get("research_state") or source_memory
        result["search_evidence_claims_accepted"] = len(evidence_result["accepted"])
        result["search_evidence_claims_rejected"] = evidence_result["rejected"]
        result["fields_resolved_from_search"] = evidence_result["fields_resolved"]
        _assign_configuration(job, research_state=source_memory)
        result["image_found"] = bool(result.get("image_found") or discovery_market["image_found"])
        result["review_evidence_found"] = bool(
            result.get("review_evidence_found") or discovery_market["review_evidence_found"]
        )
        result["review_evidence_found_this_run"] = bool(
            result.get("review_evidence_found_this_run") or discovery_market["review_evidence_found"]
        )

        # Re-run Product Understanding after phase-one evidence. The next plan
        # is calculated from the newly persisted evidence, never from the old
        # flat objective list.
        import_job = db.query(ImportJob).filter(ImportJob.id == item.import_job_id).first()
        if import_job:
            process_item_enrichment(
                db, item, import_job.column_mapping or {},
                mode=configuration.get("requested_mode") or "missing_only",
                selected_fields=configuration.get("selected_fields") or [],
            )
        next_plan = product_improvement_summary(db, product)
        from app.services.identity_review import synchronize_blocking_issue
        synchronize_blocking_issue(db, product, next_plan.get("identity_review") or {})
        result["identity_phase_completed"] = configuration.get("research_phase") == "identity_resolution"
        result["taxonomy_phase_completed"] = configuration.get("research_phase") == "taxonomy_resolution"
        result["next_phase"] = next_plan.get("research_phase")
        # A bounded second discovery pass is allowed only after persisted
        # evidence has been reconciled and gaps recalculated.  This prevents a
        # shallow/blocked first source set from falsely ending Improve Product.
        remaining_objectives = [entry["field"] for entry in next_plan.get("research_objectives") or []]
        from app.services.review_aggregate import select_review_aggregate
        current_reviews = select_review_aggregate(db, product.id) or {}
        requested_review_objectives = {
            "reviews", "review_summary", "rating", "review_count"
        } & set(remaining_objectives) & set(research_objectives)
        needs_review_text = bool(
            requested_review_objectives
            and int(current_reviews.get("review_sample_count") or 0) < int(settings.WEB_RESEARCH_REVIEW_SAMPLE_TARGET)
        )
        needs_second_pass = bool(
            remaining_objectives
            and (
                (not identity_only and int(result.get("sources_ingested") or 0) == 0)
                or needs_review_text
            )
            and not configuration.get("second_pass_started")
            and int(source_memory.get("provider_searches") or 0) < int(settings.WEB_RESEARCH_MAX_PROVIDER_SEARCHES)
        )
        if needs_second_pass:
            _assign_configuration(
                job, second_pass_started=True,
                review_text_fallback_started=needs_review_text,
            )
            db.commit()
            # Alternative evidence is a new-source pass, not a reversal of the
            # same queries. Existing exact anchors remain stable while the
            # provider receives and the application enforces source exclusions.
            alternate_queries = identity_queries
            second = start_product_source_discovery(
                brand=product.brand.name if product.brand else "", product_name=product.product_name,
                product_format=_product_expected_format(db, product), gtin=variant.gtin if variant else "",
                approved_domains=[], research_objectives=remaining_objectives,
                identity_queries=alternate_queries,
                excluded_urls=list(source_memory.get("attempted_urls") or []),
                excluded_domains=list(source_memory.get("blocked_domains") or []),
                discovery_purpose="alternative_independent_evidence",
            )
            source_memory["provider_searches"] = int(source_memory.get("provider_searches") or 0) + 1
            _assign_configuration(job, research_state=source_memory)
            db.commit()
            while second.get("status") in {"queued", "in_progress"}:
                if stop_event and stop_event.is_set():
                    return
                time.sleep(2)
                try:
                    second = poll_product_source_discovery(second)
                except SearchRateLimited as exc:
                    if rate_limit_retries >= int(settings.OPENAI_WEB_RESEARCH_MAX_RETRIES):
                        result.setdefault("errors", []).append("Bounded second discovery pass was rate limited after safe retries.")
                        second = {**second, "status": "failed", "candidates": [], "market_observations": []}
                        break
                    rate_limit_retries += 1
                    delay = max(exc.retry_after, settings.OPENAI_WEB_RESEARCH_BACKOFF_SECONDS * (2 ** (rate_limit_retries - 1)))
                    retry_delays.append(round(delay, 3))
                    time.sleep(delay)
                    if exc.restart_required:
                        if int(source_memory.get("provider_searches") or 0) >= int(settings.WEB_RESEARCH_MAX_PROVIDER_SEARCHES):
                            result.setdefault("errors", []).append("Provider-search budget exhausted after a terminal rate limit.")
                            second = {**second, "status": "failed", "candidates": [], "market_observations": [], "evidence_claims": []}
                            break
                        second = start_product_source_discovery(
                            brand=product.brand.name if product.brand else "", product_name=product.product_name,
                            product_format=_product_expected_format(db, product), gtin=variant.gtin if variant else "",
                            approved_domains=[], research_objectives=remaining_objectives,
                            identity_queries=alternate_queries,
                            excluded_urls=list(source_memory.get("attempted_urls") or []),
                            excluded_domains=list(source_memory.get("blocked_domains") or []),
                            discovery_purpose="alternative_independent_evidence",
                        )
                        source_memory["provider_searches"] = int(source_memory.get("provider_searches") or 0) + 1
            second, excluded_second = _enforce_discovery_exclusions(second, source_memory)
            if excluded_second:
                product_research_log(
                    "candidate_exclusions_enforced", **log_context, discovery_pass=2,
                    rejected=excluded_second,
                )
            second_evidence = _persist_discovery_evidence(
                db, job, product, variant, second, remaining_objectives,
            )
            product_research_log(
                "search_evidence_resolution", **log_context, discovery_pass=2,
                accepted=len(second_evidence["accepted"]), rejected=len(second_evidence["rejected"]),
                rejection_reasons=[row.get("reason") for row in second_evidence["rejected"]],
                fields_resolved=second_evidence["fields_resolved"], acquisition_method="licensed_web_search",
            )
            second_market = _persist_discovery_market_evidence(
                db, product, second, expected_gtin=variant.gtin if variant else "",
            )
            db.commit()
            second_result = _automatic_product_research(
                db, product, user, candidates=second.get("candidates") or [],
                research_objectives=remaining_objectives,
                research_variant_id=variant.id if variant else None,
                research_log_context=log_context,
                research_state=source_memory,
            )
            source_memory = second_result.get("research_state") or source_memory
            result["second_pass"] = second_result
            result["second_pass_used"] = True
            result["review_text_fallback_used"] = needs_review_text
            result["sources_ingested"] = int(result.get("sources_ingested") or 0) + int(second_result.get("sources_ingested") or 0)
            result["candidates"] = int(result.get("candidates") or 0) + int(second_result.get("candidates") or 0)
            result["errors"] = list(result.get("errors") or []) + list(second_result.get("errors") or [])
            result["image_found"] = bool(result.get("image_found") or second_result.get("image_found") or second_market.get("image_found"))
            result["review_evidence_found"] = bool(result.get("review_evidence_found") or second_result.get("review_evidence_found") or second_market.get("review_evidence_found"))
            result["review_evidence_found_this_run"] = bool(result.get("review_evidence_found_this_run") or second_result.get("review_evidence_found_this_run") or second_market.get("review_evidence_found"))
            result["review_texts_extracted"] = int(result.get("review_texts_extracted") or 0) + int(second_result.get("review_texts_extracted") or 0)
            result["review_texts_persisted"] = int(second_result.get("review_texts_persisted") or result.get("review_texts_persisted") or 0)
            merged_rejections = dict(result.get("review_sample_rejections") or {})
            for reason, count in (second_result.get("review_sample_rejections") or {}).items():
                merged_rejections[reason] = int(merged_rejections.get(reason) or 0) + int(count or 0)
            result["review_sample_rejections"] = merged_rejections
            if import_job:
                process_item_enrichment(
                    db, item, import_job.column_mapping or {},
                    mode=configuration.get("requested_mode") or "missing_only",
                    selected_fields=configuration.get("selected_fields") or [],
                )
            next_plan = product_improvement_summary(db, product)
            remaining_objectives = [entry["field"] for entry in next_plan.get("research_objectives") or []]
        else:
            result["second_pass_used"] = False
            result["review_text_fallback_used"] = False
        if next_plan.get("research_phase") == "attribute_completion":
            attribute_objectives = [entry["field"] for entry in next_plan.get("research_objectives") or []]
            if attribute_objectives and configuration.get("research_phase") in {"identity_resolution", "taxonomy_resolution"}:
                attribute_result = _automatic_product_research(
                    db, product, user, candidates=discovery.get("candidates") or [],
                    research_objectives=attribute_objectives,
                    research_variant_id=variant.id if variant else None,
                    research_log_context=log_context,
                    research_state=source_memory,
                )
                source_memory = attribute_result.get("research_state") or source_memory
                result["attribute_completion"] = attribute_result
                result["sources_ingested"] = int(result.get("sources_ingested") or 0) + int(attribute_result.get("sources_ingested") or 0)
                result["candidates"] = int(result.get("candidates") or 0) + int(attribute_result.get("candidates") or 0)
                result["errors"] = list(result.get("errors") or []) + list(attribute_result.get("errors") or [])
                result["image_found"] = bool(result.get("image_found") or attribute_result.get("image_found"))
                result["review_evidence_found"] = bool(
                    result.get("review_evidence_found") or attribute_result.get("review_evidence_found")
                )
                result["review_evidence_found_this_run"] = bool(
                    result.get("review_evidence_found_this_run")
                    or attribute_result.get("review_evidence_found_this_run")
                )
                result["review_texts_extracted"] = int(result.get("review_texts_extracted") or 0) + int(attribute_result.get("review_texts_extracted") or 0)
                result["review_texts_persisted"] = int(attribute_result.get("review_texts_persisted") or result.get("review_texts_persisted") or 0)
                merged_rejections = dict(result.get("review_sample_rejections") or {})
                for reason, count in (attribute_result.get("review_sample_rejections") or {}).items():
                    merged_rejections[reason] = int(merged_rejections.get(reason) or 0) + int(count or 0)
                result["review_sample_rejections"] = merged_rejections
                if import_job:
                    process_item_enrichment(
                        db, item, import_job.column_mapping or {},
                        mode=configuration.get("requested_mode") or "missing_only",
                        selected_fields=configuration.get("selected_fields") or [],
                    )
            # Review synthesis is downstream product intelligence and therefore
            # cannot run during an unresolved identity phase.
            try:
                from app.services.review_summarization import summarize_product_reviews
                review_summary = summarize_product_reviews(db, product.id)
                result["review_summary_generated"] = bool(review_summary and int(review_summary.get("review_sample_count") or 0) > 0)
                result["review_summary_mode"] = "text_synthesis" if result["review_summary_generated"] else "aggregate_only"
            except Exception as exc:
                logger.warning("Review synthesis failed for %s: %s", product.id, exc)
                result["review_summary_generated"] = False
                result["review_summary_error"] = str(exc)
        else:
            # Aggregate reviews and imagery are safe product-family market
            # observations even when an EDT/EDP distinction is unresolved.
            if result.get("review_evidence_found"):
                try:
                    from app.services.review_summarization import summarize_product_reviews
                    review_summary = summarize_product_reviews(db, product.id)
                    result["review_summary_generated"] = bool(review_summary and int(review_summary.get("review_sample_count") or 0) > 0)
                    result["review_summary_mode"] = "text_synthesis" if result["review_summary_generated"] else "aggregate_only"
                except Exception as exc:
                    logger.warning("Review synthesis failed for %s: %s", product.id, exc)
                    result["review_summary_generated"] = False
                    result["review_summary_error"] = str(exc)
            else:
                result["review_summary_generated"] = False
            latest_understanding = next_plan.get("product_understanding") or {}
            result["identity_unresolved"] = latest_understanding.get("identity_status") != "resolved"
            result["taxonomy_unresolved"] = (
                latest_understanding.get("taxonomy_status") == "needs_review"
                or latest_understanding.get("category_module") == "unknown"
            )
        result["identity_queries_tried"] = discovery.get("identity_queries_tried") or identity_queries
        result["fields_still_missing"] = remaining_objectives
        result["web_requests_started"] = int(discovery.get("provider_attempts") or 1) + rate_limit_retries
        result["web_requests_retried"] = rate_limit_retries + len(discovery.get("retry_delays") or [])
        result["retry_delays"] = retry_delays
        result["provider"] = discovery.get("provider")
        result["active_allowed_domains"] = discovery.get("domains") or []
        result["queries_attempted"] = len(result.get("identity_queries_tried") or []) + max(0, len(research_objectives) * 2)
        result["provider_usage"] = discovery.get("usage") or {}
        result["research_state"] = source_memory
        result["research_fingerprint"] = configuration.get("research_fingerprint")
        result["duplicate_work_avoided"] = int(source_memory.get("duplicate_work_avoided") or 0)
        result["crawl_attempts"] = int(source_memory.get("crawl_attempts") or 0)
        result["blocked_domains"] = sorted({
            re.sub(r"^www\.", "", match.group(1).lower())
            for error in (result.get("errors") or [])
            for match in [re.search(r"(?:https?://)?([a-z0-9.-]+\.[a-z]{2,})", str(error), re.I)] if match
        })
        result["blocked_domains"] = sorted(set(result["blocked_domains"]) | set(source_memory.get("blocked_domains") or []))
        rejection_by_field: dict[str, list[str]] = {}
        for rejection in (result.get("search_evidence_claims_rejected") or []):
            if isinstance(rejection, dict):
                rejection_by_field.setdefault(str(rejection.get("field") or "unknown"), []).append(str(rejection.get("reason") or "rejected"))
        result["unresolved_reasons"] = {
            field: sorted(set(rejection_by_field.get(field) or (
                ["budget_exhausted"] if int(source_memory.get("provider_searches") or 0) >= int(settings.WEB_RESEARCH_MAX_PROVIDER_SEARCHES)
                else ["no_policy-compliant_evidence_found"]
            )))
            for field in result.get("fields_still_missing") or []
        }
        after_metrics = _research_snapshot(db, product)
        from app.services.research_reliability import evaluate_research_outcome, public_business_status
        outcome_metrics = evaluate_research_outcome(
            before_metrics, after_metrics, result=result, errors=result.get("errors") or [],
        )
        result.update(outcome_metrics)
        result["business_status"] = public_business_status(outcome_metrics["business_outcome"])
        result["research_status"] = outcome_metrics["business_outcome"]
        result["research_job_id"] = str(job.id)
        result["research_pending"] = False
        _assign_configuration(job, discovery=discovery, result=result, after_metrics=after_metrics)
        outcome = outcome_metrics["business_outcome"]
        if outcome == "blocked_sources":
            job.status = "blocked"
        elif outcome == "failed":
            job.status = "failed"
        elif outcome == "partially_improved":
            job.status = "partially_completed"
        else:
            # completed is a technical terminal state only. The business result
            # remains explicit in configuration.result.business_outcome.
            job.status = "completed"
        job.completed_at = datetime.utcnow()
        job.heartbeat_at = job.completed_at
        job.error_summary = "\n".join(result.get("errors") or []) or None
        db.commit()
        product_research_log(
            "fields_result", **log_context,
            fields_added=result.get("fields_added") or [],
            fields_changed=result.get("fields_changed") or [],
            evidence_gate_rejections=result.get("fields_rejected") or result.get("evidence_gate_rejections") or [],
            remaining_important_gaps=result.get("fields_still_missing") or [],
            completeness_before=before_metrics.get("completeness"),
            completeness_after=after_metrics.get("completeness"),
            sources_ingested=result.get("sources_ingested"), blocked_domains=result.get("blocked_domains"),
            review_summary_generated=bool(result.get("review_summary_generated")),
            research_fingerprint=result.get("research_fingerprint"),
            acquisition_methods=["licensed_web_search", "crawl_html"],
            provider_usage=result.get("provider_usage"), crawl_attempts=result.get("crawl_attempts"),
            duplicate_work_avoided=result.get("duplicate_work_avoided"),
            unresolved_reasons=result.get("unresolved_reasons"),
        )
        product_research_log(
            "job_finished", **log_context, technical_status=job.status,
            business_outcome=outcome, completeness_before=before_metrics.get("completeness"),
            completeness_after=after_metrics.get("completeness"),
        )
    except Exception as exc:
        db.rollback()
        job = db.query(CrawlJob).filter(CrawlJob.id == job_id).first()
        if job:
            from app.services.research_reliability import classify_research_error, public_business_status
            failure_class = classify_research_error(str(exc))
            outcome = "rate_limited_retriable" if failure_class == "rate_limited" else "failed"
            job.status = "failed"
            job.completed_at = datetime.utcnow()
            job.heartbeat_at = job.completed_at
            job.error_summary = str(exc)
            _assign_configuration(job, result={
                "research_job_id": str(job.id), "research_status": outcome,
                "business_outcome": outcome, "business_status": public_business_status(outcome),
                "research_pending": False, "sources_ingested": 0,
                "errors": [str(exc)], "failure_class": failure_class,
                "web_requests_retried": int((job.configuration or {}).get("rate_limit_retries") or 0),
                "retry_delays": (job.configuration or {}).get("retry_delays") or [],
            })
            db.commit()
        product_research_log(
            "job_error", level=logging.ERROR, job_id=str(job_id),
            product_id=str((job.configuration or {}).get("research_product_id")) if job else None,
            error_type=type(exc).__name__, error=str(exc),
        )
        logger.exception("Background product research failed for %s", job_id)
    finally:
        db.close()


def run_product_research_worker(stop_event: threading.Event) -> None:
    db = SessionLocal()
    try:
        recover_product_research_jobs(db)
    finally:
        db.close()
    while not stop_event.is_set():
        db = SessionLocal()
        try:
            job = _claim_job(db)
            job_id = job.id if job else None
        except Exception:
            logger.exception("Unable to claim a background product-research job")
            job_id = None
        finally:
            db.close()
        if job_id:
            run_product_research_job(job_id, stop_event)
        else:
            stop_event.wait(1.0)


def start_product_research_worker() -> tuple[threading.Event, list[threading.Thread]]:
    stop_event = threading.Event()
    worker_count = max(1, min(int(settings.OPENAI_WEB_RESEARCH_CONCURRENCY), 4))
    threads = []
    for index in range(worker_count):
        thread = threading.Thread(
            target=run_product_research_worker,
            args=(stop_event,),
            name=f"product-research-worker-{index + 1}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    logger.info("Started %s durable product-research workers", worker_count)
    return stop_event, threads
