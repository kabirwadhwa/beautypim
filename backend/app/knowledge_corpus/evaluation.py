from __future__ import annotations

import random
from collections import Counter
import time
from statistics import mean

from sqlalchemy.orm import Session

from app.knowledge_corpus.retrieval import evidence_is_sufficient, retrieve_corpus_evidence
from app.models import KnowledgeFieldObservation, KnowledgeFormulation, KnowledgeProduct, KnowledgeSourceObservation, KnowledgeVariant


def evaluate_holdout(db: Session, sample_size: int = 100, seed: int = 42) -> dict:
    """Evaluate indexed exact retrieval against known corpus variant identities."""
    rng = random.Random(seed)
    ids = []
    per_category = max(1, sample_size // 5)
    for category in ("Skin Care", "Hair Care", "Makeup", "Fragrance"):
        category_ids = [row[0] for row in db.query(KnowledgeVariant.id).join(KnowledgeProduct).filter(
            KnowledgeVariant.normalized_gtin.isnot(None), KnowledgeProduct.category == category).all()]
        rng.shuffle(category_ids); ids.extend(category_ids[:per_category])
    remaining = [row[0] for row in db.query(KnowledgeVariant.id).filter(
        KnowledgeVariant.normalized_gtin.isnot(None), ~KnowledgeVariant.id.in_(ids or [None])).all()]
    rng.shuffle(remaining); ids.extend(remaining[: max(0, sample_size - len(ids))])
    correct = variant_correct = sufficient = 0
    latencies = []
    field_counts = []
    formulations = identity_conflicts = 0; categories = Counter()
    for variant_id in ids:
        variant = db.query(KnowledgeVariant).filter(KnowledgeVariant.id == variant_id).one()
        product = db.query(KnowledgeProduct).filter(KnowledgeProduct.id == variant.knowledge_product_id).one()
        categories[product.category or "Unclassified"] += 1
        start = time.perf_counter()
        result = retrieve_corpus_evidence(db, gtin=variant.normalized_gtin, brand="deliberately wrong", product_name="wrong")
        latencies.append((time.perf_counter() - start) * 1000)
        matches = result.get("exact_matches") or []
        identity_conflicts += int(result.get("match_level") == "conflict")
        if matches: correct += 1
        if any(item.get("knowledge_variant_id") == str(variant.id) for item in matches): variant_correct += 1
        if evidence_is_sufficient(result): sufficient += 1
        fields = {name for item in matches for name in item.get("fields", {})}
        field_counts.append(len(fields))
        formulations += int(any(item.get("formulations") for item in matches))
    total = len(ids)
    return {
        "sample_size": total,
        "exact_gtin_lookup_success_rate": round(correct / total, 4) if total else 0,
        "correct_variant_match_rate": round(variant_correct / total, 4) if total else 0,
        "incorrect_match_rate": round((total - variant_correct) / total, 4) if total else 0,
        "mean_exact_fields_available": round(mean(field_counts), 2) if field_counts else 0,
        "formulation_coverage": round(formulations / total, 4) if total else 0,
        "identity_conflict_rate": round(identity_conflicts / total, 4) if total else 0,
        "web_search_avoidable_rate": round(sufficient / total, 4) if total else 0,
        "mean_retrieval_latency_ms": round(mean(latencies), 2) if latencies else 0,
        "p95_retrieval_latency_ms": round(sorted(latencies)[max(0, int(len(latencies) * .95) - 1)], 2) if latencies else 0,
        "category_distribution": dict(categories),
        "baseline_without_corpus": {
            "exact_gtin_lookup_success_rate": 0,
            "exact_evidence_fields": 0,
            "web_search_avoidable_rate": 0,
        },
    }


def evaluate_non_ean_matching(db: Session, sample_size: int = 200, seed: int = 73) -> dict:
    """Evaluate exact source IDs, family IDs and non-EAN name/variant matching.

    The test uses corpus identities as a deterministic holdout and deliberately
    rejects ambiguous name-only cases instead of rewarding unsafe recall.
    """
    rng = random.Random(seed)
    observations = db.query(KnowledgeSourceObservation).join(KnowledgeVariant).filter(
        KnowledgeVariant.normalized_gtin.is_(None),
        KnowledgeSourceObservation.source_record_id.isnot(None),
    ).all()
    rng.shuffle(observations)
    source_target = min(80, sample_size)
    source_rows = observations[:source_target]
    source_correct = source_ambiguous = 0
    source_latencies = []
    for observation in source_rows:
        started = time.perf_counter()
        result = retrieve_corpus_evidence(
            db, source_product_id=observation.source_record_id,
            source_dataset=observation.dataset_key,
        )
        source_latencies.append((time.perf_counter() - started) * 1000)
        matches = result.get("exact_matches") or []
        source_correct += int(any(item.get("knowledge_variant_id") == str(observation.knowledge_variant_id) for item in matches))
        source_ambiguous += int(result.get("match_level") != "exact_product")

    parent_candidates = db.query(
        KnowledgeSourceObservation.dataset_key,
        KnowledgeSourceObservation.source_parent_id,
        KnowledgeSourceObservation.knowledge_product_id,
    ).filter(KnowledgeSourceObservation.source_parent_id.isnot(None)).distinct().all()
    rng.shuffle(parent_candidates)
    parent_target = min(60, max(0, sample_size - len(source_rows)))
    parent_rows = parent_candidates[:parent_target]
    parent_correct = 0
    for dataset, parent_id, product_id in parent_rows:
        result = retrieve_corpus_evidence(db, source_parent_id=parent_id, source_dataset=dataset)
        parent_correct += int(any(item.get("knowledge_product_id") == str(product_id) for item in result.get("family_matches", [])))

    variant_candidates = db.query(KnowledgeProduct, KnowledgeVariant).join(KnowledgeVariant).filter(
        KnowledgeVariant.normalized_gtin.is_(None),
        KnowledgeVariant.normalized_product_name.isnot(None),
    ).all()
    rng.shuffle(variant_candidates)
    name_target = min(max(0, sample_size - len(source_rows) - len(parent_rows)), len(variant_candidates))
    name_rows = variant_candidates[:name_target]
    name_correct = name_ambiguous = name_incorrect = 0
    for product, variant in name_rows:
        size = " ".join(filter(None, [variant.size_value, variant.size_unit]))
        result = retrieve_corpus_evidence(
            db, brand=product.brand_name, product_name=variant.source_product_name or product.product_name,
            size=size, shade=variant.shade or variant.colour or "",
        )
        matches = result.get("exact_matches") or []
        if any(item.get("knowledge_variant_id") == str(variant.id) for item in matches):
            name_correct += 1
        elif result.get("match_level") in {"product_family", "comparable", "unmatched"}:
            name_ambiguous += 1
        else:
            name_incorrect += 1

    total = len(source_rows) + len(parent_rows) + len(name_rows)
    return {
        "sample_size": total,
        "exact_source_id": {"sample": len(source_rows), "correct": source_correct,
            "correct_rate": round(source_correct / len(source_rows), 4) if source_rows else 0,
            "ambiguous_rate": round(source_ambiguous / len(source_rows), 4) if source_rows else 0},
        "exact_parent_id": {"sample": len(parent_rows), "correct": parent_correct,
            "correct_rate": round(parent_correct / len(parent_rows), 4) if parent_rows else 0},
        "brand_name_size_shade": {"sample": len(name_rows), "correct": name_correct,
            "correct_rate": round(name_correct / len(name_rows), 4) if name_rows else 0,
            "ambiguous_rate": round(name_ambiguous / len(name_rows), 4) if name_rows else 0,
            "incorrect_rate": round(name_incorrect / len(name_rows), 4) if name_rows else 0},
        "incorrect_match_rate": round(name_incorrect / total, 4) if total else 0,
        "mean_source_id_latency_ms": round(mean(source_latencies), 2) if source_latencies else 0,
    }
