from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth import require_admin, require_viewer_or_above
from app.database import get_db
from app.knowledge_corpus.import_service import corpus_metrics
from app.knowledge_corpus.retrieval import retrieve_corpus_evidence
from app.models import KnowledgeConflict, KnowledgeCorpusImportJob, KnowledgeProduct, User

router = APIRouter(prefix="/knowledge-corpus", tags=["Knowledge Corpus"])


def _job(item: KnowledgeCorpusImportJob):
    return {column.name: getattr(item, column.name) for column in item.__table__.columns if column.name not in {"requested_by_id"}}


@router.get("/metrics")
def metrics(db: Session = Depends(get_db), _: User = Depends(require_viewer_or_above)):
    return corpus_metrics(db)


@router.get("/imports")
def imports(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return [_job(item) for item in db.query(KnowledgeCorpusImportJob).order_by(KnowledgeCorpusImportJob.created_at.desc()).limit(100)]


@router.get("/search")
def search(
    gtin: str = "", brand: str = "", product_name: str = "", category: str = "",
    db: Session = Depends(get_db), _: User = Depends(require_admin),
):
    if not any((gtin, brand, product_name, category)):
        raise HTTPException(422, "Provide GTIN, brand, product name or category")
    return retrieve_corpus_evidence(db, gtin=gtin, brand=brand, product_name=product_name, category=category)


@router.get("/products")
def products(
    query: str = "", limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db), _: User = Depends(require_admin),
):
    rows = db.query(KnowledgeProduct)
    if query:
        term = f"%{query.lower().strip()}%"
        rows = rows.filter(KnowledgeProduct.searchable_text.ilike(term))
    return [{"id": str(item.id), "brand": item.brand_name, "product_name": item.product_name,
             "category": item.category, "subcategory": item.subcategory, "product_type": item.product_type}
            for item in rows.order_by(KnowledgeProduct.brand_name, KnowledgeProduct.product_name).limit(limit)]


@router.get("/conflicts")
def conflicts(
    status: Optional[str] = "open", limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db), _: User = Depends(require_admin),
):
    rows = db.query(KnowledgeConflict)
    if status: rows = rows.filter(KnowledgeConflict.status == status)
    return [{"id": str(item.id), "knowledge_product_id": str(item.knowledge_product_id),
             "knowledge_variant_id": str(item.knowledge_variant_id) if item.knowledge_variant_id else None,
             "field_name": item.field_name, "conflict_type": item.conflict_type,
             "values": item.values, "status": item.status, "created_at": item.created_at}
            for item in rows.order_by(KnowledgeConflict.created_at.desc()).limit(limit)]


@router.post("/conflicts/{conflict_id}/{decision}")
def review_conflict(conflict_id: uuid.UUID, decision: str, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    if decision not in {"accepted", "dismissed"}:
        raise HTTPException(422, "Decision must be accepted or dismissed")
    item = db.query(KnowledgeConflict).filter(KnowledgeConflict.id == conflict_id).first()
    if not item: raise HTTPException(404, "Conflict not found")
    item.status = decision
    from datetime import datetime, timezone
    item.resolved_at = datetime.now(timezone.utc)
    db.commit()
    return {"id": str(item.id), "status": item.status}
