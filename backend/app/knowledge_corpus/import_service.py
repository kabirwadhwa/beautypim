from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.knowledge_corpus.normalization import clean_text, normalized_brand, normalized_text, stable_hash
from app.knowledge_corpus.records import CorpusAdapter, CorpusRecord
from app.models import (
    KnowledgeConflict, KnowledgeCorpusImportJob, KnowledgeFieldObservation,
    KnowledgeFormulation, KnowledgeMarketObservation, KnowledgeProduct,
    KnowledgeSourceObservation, KnowledgeVariant,
)
from app.scraping.ingredients import split_inci


FAMILY_SAFE_FIELDS = {
    "description", "benefits", "targeted_concerns", "directions", "product_positioning",
    "sensory_description", "skin_types", "hair_types", "key_ingredients", "finish",
    "texture_format", "coverage", "fragrance_family", "fragrance_style", "longevity",
    "sillage_projection", "application_area", "routine", "category_path", "product_type",
}
VARIANT_FIELDS = {"shade", "colour", "undertone", "size", "gtin", "images"}
CONFLICT_FIELDS = {"brand", "product_name", "raw_inci", "claims", "category", "subcategory", "product_type"}


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value.isoformat()
    if hasattr(value, "__float__") and not isinstance(value, (str, int, float, bool)):
        try: return float(value)
        except (TypeError, ValueError): pass
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def create_import_job(db: Session, path: str, adapter: CorpusAdapter, dataset_key: str, source_name: str, requested_by_id=None) -> KnowledgeCorpusImportJob:
    file_hash = file_sha256(path)
    existing = db.query(KnowledgeCorpusImportJob).filter(
        KnowledgeCorpusImportJob.dataset_key == dataset_key,
        KnowledgeCorpusImportJob.file_hash == file_hash,
        KnowledgeCorpusImportJob.adapter_version == adapter.version,
    ).first()
    if existing:
        return existing
    job = KnowledgeCorpusImportJob(
        id=uuid.uuid4(), dataset_key=dataset_key, source_name=source_name,
        filename=Path(path).name, file_hash=file_hash, adapter_name=adapter.name,
        adapter_version=adapter.version, status="queued", requested_by_id=requested_by_id,
    )
    db.add(job); db.commit(); db.refresh(job)
    return job


def _merge_products(db: Session, target: KnowledgeProduct, source: KnowledgeProduct, cache: dict[str, dict]) -> KnowledgeProduct:
    """Reconcile families joined by an exact EAN while retaining all evidence."""
    if target.id == source.id:
        return target
    target_variants = {
        item.normalized_gtin: item for item in db.query(KnowledgeVariant).filter(
            KnowledgeVariant.knowledge_product_id == target.id,
            KnowledgeVariant.normalized_gtin.isnot(None),
        ).all()
    }
    for variant in db.query(KnowledgeVariant).filter(KnowledgeVariant.knowledge_product_id == source.id).all():
        existing = target_variants.get(variant.normalized_gtin) if variant.normalized_gtin else None
        if existing:
            db.query(KnowledgeSourceObservation).filter(KnowledgeSourceObservation.knowledge_variant_id == variant.id).update(
                {KnowledgeSourceObservation.knowledge_variant_id: existing.id}, synchronize_session=False)
            db.query(KnowledgeFieldObservation).filter(KnowledgeFieldObservation.knowledge_variant_id == variant.id).update(
                {KnowledgeFieldObservation.knowledge_variant_id: existing.id}, synchronize_session=False)
            db.query(KnowledgeFormulation).filter(KnowledgeFormulation.knowledge_variant_id == variant.id).update(
                {KnowledgeFormulation.knowledge_variant_id: existing.id}, synchronize_session=False)
            db.query(KnowledgeMarketObservation).filter(KnowledgeMarketObservation.knowledge_variant_id == variant.id).update(
                {KnowledgeMarketObservation.knowledge_variant_id: existing.id}, synchronize_session=False)
            db.query(KnowledgeConflict).filter(KnowledgeConflict.knowledge_variant_id == variant.id).update(
                {KnowledgeConflict.knowledge_variant_id: existing.id}, synchronize_session=False)
            cache["gtin_variant"][variant.normalized_gtin] = existing
            cache["identity_variant"].pop(variant.identity_key, None)
            db.delete(variant)
        else:
            variant.knowledge_product_id = target.id
            if variant.normalized_gtin:
                target_variants[variant.normalized_gtin] = variant
                cache["gtin_variant"][variant.normalized_gtin] = variant
    for model in (KnowledgeSourceObservation, KnowledgeFieldObservation, KnowledgeFormulation, KnowledgeConflict):
        db.query(model).filter(model.knowledge_product_id == source.id).update(
            {model.knowledge_product_id: target.id}, synchronize_session=False)
    for mapping in ("identity_product", "gtin_product", "record_product", "parent_product"):
        for key, product in list(cache[mapping].items()):
            if product and product.id == source.id:
                cache[mapping][key] = target
    cache["identity_product"][source.identity_key] = target
    db.delete(source)
    db.flush()
    return target


def _find_or_create_product(db: Session, record: CorpusRecord, cache: dict[str, dict]) -> tuple[KnowledgeProduct, bool]:
    # Cross-dataset exact barcode identity outranks text identity.
    gtin_product = cache["gtin_product"].get(record.gtin) if record.gtin else None
    if record.source_record_id:
        known_record = cache["record_product"].get((record.dataset_key, record.source_record_id))
        if known_record:
            return known_record, False
    # Within a feed, a supplied parent ID is authoritative for the family even
    # when shade/size tokens change the display name on each variant row.
    if record.source_parent_id:
        known_family = cache["parent_product"].get((record.dataset_key, record.source_parent_id))
        if known_family:
            if gtin_product and gtin_product.id != known_family.id:
                known_family = _merge_products(db, known_family, gtin_product, cache)
            return known_family, False
    if gtin_product:
        return gtin_product, False
    # Family identity is intentionally independent of source dataset.
    family_name = normalized_text(record.product_name)
    identity_key = stable_hash(normalized_brand(record.brand), family_name)
    product = cache["identity_product"].get(identity_key)
    if product:
        return product, False
    product = KnowledgeProduct(
        id=uuid.uuid4(), brand_name=clean_text(record.brand), normalized_brand=normalized_brand(record.brand),
        product_name=clean_text(record.product_name), normalized_name=family_name,
        category=record.category, subcategory=record.subcategory, product_type=record.product_type,
        application_area=record.application_area,
        searchable_text=normalized_text(f"{record.brand} {record.product_name} {record.category} {record.product_type}"),
        identity_key=identity_key,
    )
    db.add(product); db.flush()
    cache["identity_product"][identity_key] = product
    return product, True


def _find_or_create_variant(db: Session, product: KnowledgeProduct, record: CorpusRecord, cache: dict[str, dict]) -> tuple[KnowledgeVariant, bool]:
    if record.gtin:
        known = cache["gtin_variant"].get(record.gtin)
        if known and known.knowledge_product_id == product.id:
            return known, False
    # A dataset-scoped source SKU is stronger than a display range/size label.
    # This is especially important for the rich workbook, whose INCI is keyed by
    # SKU but has no direct EAN bridge: two SKUs must never share a formulation
    # merely because their visible range and size happen to match.
    variant_basis = (
        record.gtin
        or (f"{record.dataset_key}:{record.source_record_id}" if record.source_record_id else "")
        or "|".join(filter(None, [normalized_text(record.variant_name), record.size_value, record.size_unit, normalized_text(record.shade)]))
    )
    identity_key = stable_hash(str(product.id), variant_basis)
    variant = cache["identity_variant"].get(identity_key)
    if variant:
        return variant, False
    variant = KnowledgeVariant(
        id=uuid.uuid4(), knowledge_product_id=product.id, normalized_gtin=record.gtin,
        source_product_name=clean_text(record.product_name), normalized_product_name=normalized_text(record.product_name),
        variant_name=record.variant_name, normalized_variant=normalized_text(record.variant_name),
        size_value=record.size_value, size_unit=record.size_unit, shade=record.shade,
        colour=record.colour, undertone=record.undertone, identity_key=identity_key,
    )
    db.add(variant); db.flush()
    cache["identity_variant"][identity_key] = variant
    if record.gtin:
        cache["gtin_product"][record.gtin] = product
        cache["gtin_variant"][record.gtin] = variant
    return variant, True


def _record_conflict(db: Session, product_id, variant_id, field_name: str, value: Any, observation_id, cache: dict) -> bool:
    normalized = _json_value(value)
    def comparable(item: Any) -> Any:
        if isinstance(item, str):
            return normalized_brand(item) if field_name == "brand" else normalized_text(item)
        if isinstance(item, list):
            return sorted(comparable(entry) for entry in item)
        if isinstance(item, dict):
            return {key: comparable(entry) for key, entry in sorted(item.items())}
        return item
    key = (product_id, variant_id, field_name)
    rows = cache.get(key)
    if rows is None:
        rows = db.query(KnowledgeFieldObservation).filter(
            KnowledgeFieldObservation.knowledge_product_id == product_id,
            KnowledgeFieldObservation.field_name == field_name,
            KnowledgeFieldObservation.knowledge_variant_id == variant_id if variant_id else KnowledgeFieldObservation.knowledge_variant_id.is_(None),
        ).limit(20).all()
        cache[key] = rows
    different = [row for row in rows if row.normalized_value not in (None, "", [], {}) and comparable(row.normalized_value) != comparable(normalized)]
    if not different:
        cache[key] = [*rows, type("ObservedValue", (), {"normalized_value": normalized, "source_observation_id": observation_id})()]
        return False
    source_ids = [str(row.source_observation_id) for row in different] + [str(observation_id)]
    values = [row.normalized_value for row in different] + [normalized]
    marker = stable_hash(str(product_id), str(variant_id or ""), field_name, str(sorted(map(str, values))))
    existing = db.query(KnowledgeConflict).filter(
        KnowledgeConflict.knowledge_product_id == product_id,
        KnowledgeConflict.field_name == field_name,
        KnowledgeConflict.status == "open",
    ).all()
    if any(stable_hash(str(item.knowledge_product_id), str(item.knowledge_variant_id or ""), item.field_name, str(sorted(map(str, item.values or [])))) == marker for item in existing):
        return False
    db.add(KnowledgeConflict(
        id=uuid.uuid4(), knowledge_product_id=product_id, knowledge_variant_id=variant_id,
        field_name=field_name, conflict_type="source_disagreement", values=values,
        source_observation_ids=source_ids, status="open",
    ))
    cache[key] = [*rows, type("ObservedValue", (), {"normalized_value": normalized, "source_observation_id": observation_id})()]
    return True


def import_corpus(db: Session, job: KnowledgeCorpusImportJob, path: str, adapter: CorpusAdapter, *, limit: int | None = None, batch_size: int = 250) -> KnowledgeCorpusImportJob:
    if job.status == "completed":
        return job
    job.status = "processing"; job.started_at = job.started_at or datetime.now(timezone.utc); job.heartbeat_at = datetime.now(timezone.utc)
    inspection = adapter.inspect(path)
    # The rich workbook has two joined tabs representing the same SKU records;
    # its progress denominator is the driving attribute tab, not both tabs added.
    job.total_rows = (
        int(inspection.get("PRODUCT ATTRIBUTEN", {}).get("rows", 0))
        if adapter.name == "rich_beauty_workbook"
        else sum(int(item.get("rows", 0)) for item in inspection.values())
    )
    db.commit()
    start_row = job.processed_rows
    errors: list[str] = []
    products = {item.id: item for item in db.query(KnowledgeProduct).all()}
    variants = db.query(KnowledgeVariant).all()
    cache = {
        "identity_product": {item.identity_key: item for item in products.values()},
        "identity_variant": {item.identity_key: item for item in variants},
        "gtin_variant": {item.normalized_gtin: item for item in variants if item.normalized_gtin},
        "gtin_product": {item.normalized_gtin: products.get(item.knowledge_product_id) for item in variants if item.normalized_gtin},
        "record_product": {}, "parent_product": {},
    }
    for observation in db.query(KnowledgeSourceObservation).filter(
        KnowledgeSourceObservation.dataset_key == job.dataset_key
    ).all():
        product = products.get(observation.knowledge_product_id)
        if observation.source_record_id: cache["record_product"][(observation.dataset_key, observation.source_record_id)] = product
        if observation.source_parent_id: cache["parent_product"][(observation.dataset_key, observation.source_parent_id)] = product
    conflict_cache: dict = {}
    for record in adapter.iter_records(path, start_row=start_row, limit=limit):
        savepoint = db.begin_nested()
        try:
            if record.skip_reason:
                job.skipped_rows += 1; job.processed_rows += 1
                savepoint.commit()
                continue
            source_hash = stable_hash(job.file_hash, record.sheet, record.row_number, str(_json_value(record.raw_payload)))
            duplicate = db.query(KnowledgeSourceObservation.id).filter(
                KnowledgeSourceObservation.dataset_key == record.dataset_key,
                KnowledgeSourceObservation.source_hash == source_hash,
            ).first()
            if duplicate:
                job.duplicate_rows += 1; job.processed_rows += 1
                savepoint.commit()
                continue
            product, product_created = _find_or_create_product(db, record, cache)
            variant, variant_created = _find_or_create_variant(db, product, record, cache)
            observation = KnowledgeSourceObservation(
                id=uuid.uuid4(), import_job_id=job.id, knowledge_product_id=product.id,
                knowledge_variant_id=variant.id, dataset_key=record.dataset_key, source_sheet=record.sheet,
                source_row_number=record.row_number, source_record_id=record.source_record_id,
                source_parent_id=record.source_parent_id, source_retailer=record.source_retailer,
                source_url=record.source_url, locale=record.locale, market=record.market,
                raw_payload=_json_value(record.raw_payload), normalized_payload=_json_value(record.fields),
                source_hash=source_hash, evidence_level="variant" if (record.gtin or record.variant_name) else "product_family",
                observed_at=record.observed_at, observation_date_type="source_last_updated" if record.observed_at else "dataset_imported_at",
            )
            db.add(observation); db.flush()
            products[product.id] = product
            if record.source_record_id: cache["record_product"][(record.dataset_key, record.source_record_id)] = product
            if record.source_parent_id: cache["parent_product"][(record.dataset_key, record.source_parent_id)] = product
            core_fields = {"brand": record.brand, "product_name": record.product_name,
                           "category": record.category, "subcategory": record.subcategory, "product_type": record.product_type,
                           "application_area": record.application_area, "gtin": record.gtin, "shade": record.shade,
                           "colour": record.colour, "size": {"value": record.size_value, "unit": record.size_unit} if record.size_value else None,
                           **record.fields}
            for field_name, raw_value in core_fields.items():
                if raw_value in (None, "", [], {}):
                    continue
                scope = "variant" if field_name in VARIANT_FIELDS else "family" if field_name in FAMILY_SAFE_FIELDS else "exact_product"
                normalized_value = _json_value(raw_value)
                conflict_variant_id = variant.id if scope in {"variant", "exact_product"} else None
                if field_name in CONFLICT_FIELDS and _record_conflict(db, product.id, conflict_variant_id, field_name, normalized_value, observation.id, conflict_cache):
                    job.conflicts_detected += 1
                db.add(KnowledgeFieldObservation(
                    id=uuid.uuid4(), source_observation_id=observation.id, knowledge_product_id=product.id,
                    knowledge_variant_id=variant.id if scope in {"variant", "exact_product"} else None,
                    field_name=field_name, raw_value=_json_value(raw_value), normalized_value=normalized_value,
                    evidence_scope=scope, confidence=1.0, observed_at=record.observed_at,
                ))
            if record.raw_inci:
                ingredients = [{"position": index + 1, "name": item, "normalized_name": normalized_text(item)} for index, item in enumerate(split_inci(record.raw_inci))]
                formula_hash = stable_hash(*[item["normalized_name"] for item in ingredients])
                prior = db.query(KnowledgeFormulation).filter(KnowledgeFormulation.knowledge_variant_id == variant.id).all()
                if prior and all(item.formulation_hash != formula_hash for item in prior):
                    db.add(KnowledgeConflict(id=uuid.uuid4(), knowledge_product_id=product.id, knowledge_variant_id=variant.id,
                        field_name="raw_inci", conflict_type="formulation_disagreement",
                        values=[item.raw_inci_text for item in prior] + [record.raw_inci],
                        source_observation_ids=[str(item.source_observation_id) for item in prior] + [str(observation.id)], status="open"))
                    job.conflicts_detected += 1
                db.add(KnowledgeFormulation(
                    id=uuid.uuid4(), source_observation_id=observation.id, knowledge_product_id=product.id,
                    knowledge_variant_id=variant.id, raw_inci_text=record.raw_inci,
                    normalized_ingredients=ingredients, formulation_hash=formula_hash,
                    language=record.inci_language, market=record.market, observed_at=record.observed_at,
                )); job.formulations_created += 1
            if any(value is not None for value in (record.price, record.availability, record.image_url, record.source_url)):
                db.add(KnowledgeMarketObservation(
                    id=uuid.uuid4(), source_observation_id=observation.id, knowledge_variant_id=variant.id,
                    source_retailer=record.source_retailer, market=record.market, currency=record.currency,
                    price=record.price, original_price=record.original_price, availability=record.availability,
                    image_url=record.image_url, source_url=record.source_url, observed_at=record.observed_at,
                    observation_date_type="source_last_updated" if record.observed_at else "dataset_imported_at",
                )); job.market_observations_created += 1
            job.products_created += int(product_created); job.variants_created += int(variant_created)
            job.observations_created += 1; job.imported_rows += 1; job.processed_rows += 1
            if job.processed_rows % batch_size == 0:
                job.heartbeat_at = datetime.now(timezone.utc); db.commit()
            else:
                savepoint.commit()
        except Exception as exc:
            savepoint.rollback()
            job.failed_rows += 1; job.processed_rows += 1
            if len(errors) < 20: errors.append(f"row {record.row_number}: {exc}")
    if limit is None:
        accounted = job.imported_rows + job.duplicate_rows + job.failed_rows
        job.skipped_rows = max(job.skipped_rows, job.total_rows - accounted)
        job.processed_rows = job.total_rows
    job.status = "completed" if not errors else "partially_completed"
    job.completed_at = datetime.now(timezone.utc); job.heartbeat_at = job.completed_at
    job.error_summary = "\n".join(errors) or None
    job.metrics = corpus_metrics(db)
    from app.services.product_understanding import refresh_contracts_after_corpus_import
    job.metrics = {**job.metrics, "product_understanding_contracts_refreshed": refresh_contracts_after_corpus_import(db)}
    db.commit(); db.refresh(job)
    return job


def corpus_metrics(db: Session) -> dict[str, int]:
    from sqlalchemy import func
    parent_count = len({(dataset, parent) for dataset, parent in db.query(
        KnowledgeSourceObservation.dataset_key, KnowledgeSourceObservation.source_parent_id
    ).filter(KnowledgeSourceObservation.source_parent_id.isnot(None)).all()})
    ean_observations = db.query(KnowledgeSourceObservation.id).join(
        KnowledgeVariant, KnowledgeVariant.id == KnowledgeSourceObservation.knowledge_variant_id
    ).filter(KnowledgeVariant.normalized_gtin.isnot(None)).count()
    unique_eans = db.query(KnowledgeVariant.normalized_gtin).filter(KnowledgeVariant.normalized_gtin.isnot(None)).distinct().count()
    return {
        "raw_source_rows": db.query(KnowledgeSourceObservation).count(),
        "normalized_product_identities": db.query(KnowledgeProduct).count(),
        "normalized_variants": db.query(KnowledgeVariant).count(),
        "unique_normalized_eans": unique_eans,
        "unique_source_parent_ids": parent_count,
        "unique_brands": db.query(KnowledgeProduct.normalized_brand).distinct().count(),
        "formulations": db.query(KnowledgeFormulation).count(),
        "description_observations": db.query(KnowledgeFieldObservation).filter(KnowledgeFieldObservation.field_name == "description").count(),
        "category_observations": db.query(KnowledgeFieldObservation).filter(KnowledgeFieldObservation.field_name == "category").count(),
        "market_observations": db.query(KnowledgeMarketObservation).count(),
        "price_observations": db.query(KnowledgeMarketObservation).filter(KnowledgeMarketObservation.price.isnot(None)).count(),
        "availability_observations": db.query(KnowledgeMarketObservation).filter(KnowledgeMarketObservation.availability.isnot(None)).count(),
        "image_observations": db.query(KnowledgeMarketObservation).filter(KnowledgeMarketObservation.image_url.isnot(None)).count(),
        "duplicate_ean_identities": max(0, ean_observations - unique_eans),
        "rows_without_usable_identity": sum(max(0, (total or 0) - (imported or 0) - (duplicates or 0) - (failed or 0))
            for total, imported, duplicates, failed in db.query(
                KnowledgeCorpusImportJob.total_rows, KnowledgeCorpusImportJob.imported_rows,
                KnowledgeCorpusImportJob.duplicate_rows, KnowledgeCorpusImportJob.failed_rows,
            ).all()),
        "conflicts": db.query(KnowledgeConflict).filter(KnowledgeConflict.status == "open").count(),
        "conflicting_formulations": db.query(KnowledgeConflict).filter(
            KnowledgeConflict.status == "open", KnowledgeConflict.conflict_type == "formulation_disagreement"
        ).count(),
    }
