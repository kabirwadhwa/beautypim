import uuid
import io
import csv
import json
import requests
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import List, Dict, Any
from datetime import datetime
from app.database import get_db
from app.auth import get_current_user, require_viewer_or_above
from app.models import CanonicalProduct, ProductVariant, FieldValue, Brand, Category, ValidationIssue
from app.services.business_export import BUSINESS_EXPORT_COLUMNS, build_business_row
from app.schemas import ExportRequest, ExportResponse
from app.limiter import rate_limit
from app.config import settings
from app.services.webhooks import dispatch_webhook_safe

router = APIRouter(prefix="/exports", tags=["Export Center"])


def _tabular_rows(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Use valid JSON inside CSV/XLSX cells for structured business values."""
    return [{
        key: json.dumps(value, ensure_ascii=False, default=str)
        if isinstance(value, (dict, list)) else value
        for key, value in row.items()
    } for row in data]

def build_business_export_data(db: Session, include_inferred: bool) -> List[Dict[str, Any]]:
    # 1. Fetch only approved or published products
    products = db.query(CanonicalProduct).filter(
        CanonicalProduct.review_status.in_(["approved", "published"]),
        CanonicalProduct.is_deleted == False
    ).all()

    from app.routes.products import get_product_detail
    export_rows = [build_business_row(db, get_product_detail(prod.id, db, None), include_inferred) for prod in products]
        
    return export_rows

def build_audit_export_data(db: Session) -> List[Dict[str, Any]]:
    # Fetch all products
    products = db.query(CanonicalProduct).filter(CanonicalProduct.is_deleted == False).all()
    export_rows = []

    for prod in products:
        variant = db.query(ProductVariant).filter(
            ProductVariant.canonical_product_id == prod.id,
            ProductVariant.is_deleted == False
        ).first()

        row = {
            "product_id": str(prod.id),
            "product_name": prod.product_name,
            "brand": prod.brand.name,
            "review_status": prod.review_status,
            "gtin": variant.gtin if variant else "",
            "size": f"{variant.size or ''} {variant.unit or ''}".strip() if variant else ""
        }

        # Validation issues summary (semicolon delimited)
        issues = db.query(ValidationIssue).filter(
            ValidationIssue.canonical_product_id == prod.id,
            ValidationIssue.resolved == False
        ).all()
        row["validation_issues"] = "; ".join(f"[{i.severity}] {i.message}" for i in issues)

        # Field Values with confidence score
        fvs = db.query(FieldValue).filter(
            FieldValue.canonical_product_id == prod.id
        ).all()

        fields_history = []
        for fv in fvs:
            fields_history.append({
                "field": fv.field_name,
                "value": fv.value,
                "source": fv.source_type,
                "confidence": float(fv.confidence_score) if fv.confidence_score is not None else 1.0,
                "is_current": fv.is_current
            })
        
        row["provenance_history"] = json.dumps(fields_history)
        export_rows.append(row)

    return export_rows

@router.post("/run", response_model=ExportResponse, dependencies=[Depends(rate_limit("export", "10/minute"))])
def execute_export(
    req: ExportRequest,
    db: Session = Depends(get_db),
    current_user: Any = Depends(require_viewer_or_above)
):
    # Fetch products mapping
    if req.export_mode == "business":
        data = build_business_export_data(db, req.include_inferred)
    else:
        data = build_audit_export_data(db)

    # Webhook triggers
    webhook_triggered = False
    if req.webhook_url:
        try:
            payload = {"exported_rows": len(data), "timestamp": str(datetime.utcnow())}
            webhook_triggered = dispatch_webhook_safe(req.webhook_url, payload)
        except Exception:
            pass

    # Simple local download routing
    download_url = f"/api/exports/download?mode={req.export_mode}&format={req.file_format}&inferred={req.include_inferred}"
    return ExportResponse(
        download_url=download_url,
        webhook_triggered=webhook_triggered,
        row_count=len(data),
    )

@router.get("/download")
def download_file(
    mode: str = "business",
    format: str = "json",
    inferred: bool = False,
    db: Session = Depends(get_db),
    current_user: Any = Depends(require_viewer_or_above),
):
    if mode == "business":
        data = build_business_export_data(db, inferred)
    else:
        data = build_audit_export_data(db)

    if format == "json":
        json_str = json.dumps(data, indent=2)
        return StreamingResponse(
            io.BytesIO(json_str.encode("utf-8")),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename=beauty_pim_export_{mode}.json"}
        )

    elif format == "csv":
        output = io.StringIO()
        fieldnames = BUSINESS_EXPORT_COLUMNS if mode == "business" else (tuple(data[0]) if data else ())
        if fieldnames:
            writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            writer.writeheader()
            writer.writerows(_tabular_rows(data))
        
        return StreamingResponse(
            io.BytesIO(output.getvalue().encode("utf-8")),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=beauty_pim_export_{mode}.csv"}
        )

    elif format == "xlsx":
        df = pd.DataFrame(
            _tabular_rows(data),
            columns=BUSINESS_EXPORT_COLUMNS if mode == "business" else None,
        )
        excel_io = io.BytesIO()
        with pd.ExcelWriter(excel_io, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Export")
        
        excel_io.seek(0)
        return StreamingResponse(
            excel_io,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=beauty_pim_export_{mode}.xlsx"}
        )

    else:
        raise HTTPException(status_code=400, detail="Unsupported download format")
