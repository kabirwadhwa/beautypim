import hashlib
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

from app.knowledge_corpus.retrieval import retrieve_corpus_evidence
from app.models import (
    Brand, CanonicalProduct, FieldValue, Formulation, FormulationIngredient,
    ImportJob, ImportJobItem, KnowledgeCorpusImportJob, KnowledgeFormulation,
    KnowledgeProduct, KnowledgeSourceObservation, KnowledgeVariant,
    ProductVariant, SourceListing, ValidationIssue,
)
from app.routes.products import get_product_detail
from app.services.formulation_resolution import (
    promote_exact_corpus_formulation, promote_formulation,
)
from app.services.product_improvement import product_improvement_summary
from app.services.source_data_merge import reprocess_import_job_source_data
from app.worker import run_job_worker


def _product(db, gtin="0123456789012"):
    brand = Brand(id=uuid.uuid4(), name="Test Brand", normalized_name=f"test-brand-{uuid.uuid4()}")
    product = CanonicalProduct(id=uuid.uuid4(), brand=brand, product_name="Test Hydrating Cream",
                               normalized_name="test hydrating cream", review_status="imported")
    variant = ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id, gtin=gtin)
    db.add_all([brand, product, variant]); db.flush()
    return product, variant


def _source_job(db, product, variant, ingredients, *, created_at=None, header="Ingredients"):
    job = ImportJob(id=uuid.uuid4(), filename="ingredients.xlsx", source_name="customer",
                    file_hash=uuid.uuid4().hex, status="pending", total_rows=1, processed_rows=0,
                    column_mapping={"ean": "GTIN", "brand": "Brand", "product_name": "Product Name",
                                    "ingredients": header}, created_at=created_at or datetime.utcnow())
    listing = SourceListing(id=uuid.uuid4(), import_job_id=job.id, source_hash=uuid.uuid4().hex,
                            raw_data={"GTIN": variant.gtin, "Brand": "Test Brand",
                                      "Product Name": product.product_name, header: ingredients},
                            canonical_product_id=product.id, product_variant_id=variant.id)
    item = ImportJobItem(id=uuid.uuid4(), import_job_id=job.id, source_row_number=1,
                         source_listing_id=listing.id, canonical_product_id=product.id,
                         product_variant_id=variant.id, status="pending", match_status="exact_match",
                         enrichment_status="not_requested")
    db.add_all([job, listing]); db.flush(); db.add(item); db.commit()
    return job


def _knowledge(db, product, variant, raw, *, second_raw=None):
    import_job = KnowledgeCorpusImportJob(
        id=uuid.uuid4(), dataset_key="fixture", source_name="Fixture", filename="fixture.xlsx",
        file_hash=uuid.uuid4().hex, adapter_name="fixture", adapter_version="1", status="completed",
    )
    kp = KnowledgeProduct(id=uuid.uuid4(), brand_name="Test Brand", normalized_brand="test brand",
                          product_name=product.product_name, normalized_name="test hydrating cream",
                          category="Skincare", product_type="Cream", identity_key=uuid.uuid4().hex)
    kv = KnowledgeVariant(id=uuid.uuid4(), knowledge_product_id=kp.id, normalized_gtin=variant.gtin,
                          source_product_name=product.product_name, normalized_product_name="test hydrating cream",
                          identity_key=uuid.uuid4().hex)
    db.add_all([import_job, kp, kv]); db.flush()
    raws = [raw] + ([second_raw] if second_raw else [])
    for index, value in enumerate(raws, 1):
        source = KnowledgeSourceObservation(
            id=uuid.uuid4(), import_job_id=import_job.id, knowledge_product_id=kp.id,
            knowledge_variant_id=kv.id, dataset_key="fixture", source_sheet="Products",
            source_row_number=index, source_record_id=f"ROW-{index}", raw_payload={"INCI": value},
            normalized_payload={"raw_inci": value}, source_hash=uuid.uuid4().hex,
            evidence_level="variant",
        )
        db.add(source); db.flush()
        db.add(KnowledgeFormulation(
            id=uuid.uuid4(), source_observation_id=source.id, knowledge_product_id=kp.id,
            knowledge_variant_id=kv.id, raw_inci_text=value,
            normalized_ingredients=[{"position": pos, "normalized_name": name.lower()}
                                    for pos, name in enumerate(value.split(", "), 1)],
            formulation_hash=hashlib.sha256(value.encode()).hexdigest(),
        ))
    db.flush()


def test_real_source_import_promotes_normalizes_and_never_calls_external_services(db):
    product, variant = _product(db)
    raw = "AQUA, GLYCERIN, NIACINAMIDE, PANTHENOL, PARFUM"
    job = _source_job(db, product, variant, raw)
    with patch("app.worker.run_ai_enrichment") as ai, \
         patch("app.worker.queue_exact_formulation_research") as web, \
         patch("app.routes.products._automatic_product_research") as crawl:
        run_job_worker(db, job.id)
        ai.assert_not_called(); web.assert_not_called(); crawl.assert_not_called()
    formulation = db.query(Formulation).filter(Formulation.product_variant_id == variant.id,
                                                Formulation.is_deleted == False).one()
    assert formulation.raw_inci_text == raw
    assert formulation.source_listing_id is not None
    ingredients = db.query(FormulationIngredient).filter(
        FormulationIngredient.formulation_id == formulation.id).order_by(FormulationIngredient.position).all()
    assert [row.raw_inci_name for row in ingredients] == raw.split(", ")
    assert not any(row.is_key_ingredient for row in ingredients)
    detail = get_product_detail(product.id, db, None)
    assert detail.formulations[0].raw_inci_text == raw
    quality = product_improvement_summary(db, product)
    assert "inci" not in quality["missing_high_priority_fields"]
    assert "inci" not in [row["field"] for row in quality["research_objectives"]]
    run_job_worker(db, job.id)
    assert db.query(Formulation).filter(Formulation.canonical_product_id == product.id,
                                        Formulation.is_deleted == False).count() == 1


def test_inci_alias_and_source_chronology_blank_and_old_reprocess(db):
    product, variant = _product(db)
    now = datetime.utcnow()
    old = _source_job(db, product, variant, "AQUA, GLYCERIN", created_at=now, header="INCI List")
    newer = _source_job(db, product, variant, "AQUA, NIACINAMIDE", created_at=now + timedelta(minutes=1))
    blank = _source_job(db, product, variant, "", created_at=now + timedelta(minutes=2))
    for job in (old, newer, blank):
        run_job_worker(db, job.id)
    active = db.query(Formulation).filter(Formulation.canonical_product_id == product.id,
                                          Formulation.is_deleted == False).one()
    assert active.raw_inci_text == "AQUA, NIACINAMIDE"
    reprocess_import_job_source_data(db, old.id)
    active = db.query(Formulation).filter(Formulation.canonical_product_id == product.id,
                                          Formulation.is_deleted == False).one()
    assert active.raw_inci_text == "AQUA, NIACINAMIDE"


def test_human_and_customer_formulations_outrank_corpus_and_web(db):
    product, variant = _product(db)
    human = FieldValue(id=uuid.uuid4(), canonical_product_id=product.id, field_name="ingredients",
                       value="AQUA, HUMANOL", source_type="human_edit", review_status="confirmed", is_current=True)
    db.add(human); db.flush()
    human_result = promote_formulation(db, product=product, variant=variant,
                                       raw_inci_text=str(human.value), source_kind="human_edit",
                                       source_reference=f"human_edit:{human.id}")
    assert human_result.status == "applied"
    assert promote_formulation(db, product=product, variant=variant, raw_inci_text="AQUA, WEBOL",
                               source_kind="licensed_web_search",
                               source_reference="licensed_web_search:https://example.test").status == "rejected"
    _knowledge(db, product, variant, "AQUA, CORPUSOL")
    corpus = retrieve_corpus_evidence(db, gtin=variant.gtin)
    assert promote_exact_corpus_formulation(db, product, variant, corpus).status == "rejected"
    assert db.query(Formulation).filter(Formulation.is_deleted == False).one().raw_inci_text == "AQUA, HUMANOL"


def test_customer_formulation_outranks_corpus_and_web(db):
    product, variant = _product(db)
    job = _source_job(db, product, variant, "AQUA, CUSTOMEROL")
    run_job_worker(db, job.id)
    assert promote_formulation(
        db, product=product, variant=variant, raw_inci_text="AQUA, WEBOL",
        source_kind="licensed_web_search",
        source_reference="licensed_web_search:https://example.test",
    ).status == "rejected"
    _knowledge(db, product, variant, "AQUA, CORPUSOL")
    assert promote_exact_corpus_formulation(
        db, product, variant, retrieve_corpus_evidence(db, gtin=variant.gtin)
    ).status == "rejected"
    assert db.query(Formulation).filter(Formulation.is_deleted == False).one().raw_inci_text == "AQUA, CUSTOMEROL"


def test_exact_knowledge_formulation_promotes_normalizes_and_is_idempotent(db):
    product, variant = _product(db)
    raw = "AQUA, GLYCERIN, CERAMIDE NP"
    _knowledge(db, product, variant, raw)
    corpus = retrieve_corpus_evidence(db, gtin=variant.gtin)
    first = promote_exact_corpus_formulation(db, product, variant, corpus)
    second = promote_exact_corpus_formulation(db, product, variant, corpus)
    assert (first.status, second.status) == ("applied", "unchanged")
    formulation = db.query(Formulation).filter(Formulation.is_deleted == False).one()
    assert formulation.raw_inci_text == raw
    assert formulation.source_reference.startswith("knowledge_corpus:")
    assert db.query(FormulationIngredient).filter(FormulationIngredient.formulation_id == formulation.id).count() == 3
    assert "inci" not in [row["field"] for row in product_improvement_summary(db, product)["research_objectives"]]


def test_conflicting_exact_knowledge_formulations_remain_unresolved(db):
    product, variant = _product(db)
    _knowledge(db, product, variant, "AQUA, GLYCERIN", second_raw="AQUA, RETINOL")
    corpus = retrieve_corpus_evidence(db, gtin=variant.gtin)
    result = promote_exact_corpus_formulation(db, product, variant, corpus)
    assert result.status == "conflicting"
    assert db.query(Formulation).filter(Formulation.is_deleted == False).count() == 0
    assert db.query(ValidationIssue).filter(ValidationIssue.field_name == "inci",
                                            ValidationIssue.resolved == False).count() == 1


def test_family_or_comparable_corpus_cannot_promote_formulation(db):
    product, variant = _product(db)
    for level in ("product_family", "comparable", "unmatched"):
        result = promote_exact_corpus_formulation(db, product, variant, {
            "match_level": level,
            "exact_matches": [],
            "family_matches": [{"formulations": [{"raw_inci_text": "AQUA, WRONG"}]}],
            "comparables": [{"formulations": [{"raw_inci_text": "AQUA, WRONG"}]}],
        })
        assert result.status == "unresolved"
    assert db.query(Formulation).count() == 0


def test_sibling_formulation_does_not_satisfy_requested_variant_completeness(db):
    product, requested = _product(db)
    sibling = ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id, gtin="0123456789098")
    db.add_all([
        sibling,
        FieldValue(id=uuid.uuid4(), canonical_product_id=product.id, field_name="product_type",
                   value="Face Cream", source_type="source_data", review_status="confirmed", is_current=True),
        FieldValue(id=uuid.uuid4(), canonical_product_id=product.id, field_name="product_understanding",
                   value={"identity_status": "resolved", "taxonomy_status": "resolved",
                          "category_module": "skincare"}, source_type="deterministic_rule",
                   review_status="confirmed", is_current=True),
    ]); db.flush()
    promote_formulation(db, product=product, variant=sibling,
                        raw_inci_text="AQUA, SIBLINGOL", source_kind="verified_evidence",
                        source_reference="verified:sibling")
    quality = product_improvement_summary(db, product)
    assert "inci" in [row["field"] for row in quality["research_objectives"]]
