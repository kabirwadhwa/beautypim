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
from app.schemas import ExportRequest, ExportResponse
from app.limiter import rate_limit
from app.config import settings
from app.services.webhooks import dispatch_webhook_safe

router = APIRouter(prefix="/exports", tags=["Export Center"])

def build_business_export_data(db: Session, include_inferred: bool) -> List[Dict[str, Any]]:
    # 1. Fetch only approved or published products
    products = db.query(CanonicalProduct).filter(
        CanonicalProduct.review_status.in_(["approved", "published"]),
        CanonicalProduct.is_deleted == False
    ).all()

    export_rows = []
    for prod in products:
        # Load active variant
        variant = db.query(ProductVariant).filter(
            ProductVariant.canonical_product_id == prod.id,
            ProductVariant.is_deleted == False
        ).first()
        
        row = {
            "product_id": str(prod.id),
            "product_name": prod.product_name,
            "brand": prod.brand.name,
            "gtin": variant.gtin if variant else "",
            "size": f"{variant.size or ''} {variant.unit or ''}".strip() if variant else ""
        }

        # Query all field values
        fvs = db.query(FieldValue).filter(
            FieldValue.canonical_product_id == prod.id,
            FieldValue.is_current == True
        ).all()

        fields_dict: Dict[str, FieldValue] = {fv.field_name: fv for fv in fvs}

        # Apply strict priority selection algorithm:
        # 1. human_edit
        # 2. source_data or deterministic_rule
        # 3. ai_inference (if permitted)
        # 4. unknown
        enrichment_keys = [
            "subcategory", "product_type", "gender_target", "texture", "application_area", "target_audience",
            "vegan", "cruelty_free", "paraben_free", "sulfate_free", "silicone_free", "alcohol_free", "fragrance_present",
            "hydration", "anti_ageing", "pigmentation", "acne", "redness", "sensitivity", "scalp_care", "hair_growth", "fragrance", "freshness"
        ]

        for key in enrichment_keys:
            fv = fields_dict.get(key)
            val = "UNKNOWN"
            
            if fv:
                if fv.review_status == "conflicting":
                    val = "CONFLICTING"
                elif fv.review_status == "not_applicable":
                    val = "NOT_APPLICABLE"
                elif fv.source_type == "human_edit":
                    val = str(fv.value)
                elif fv.source_type in ["source_data", "deterministic_rule"]:
                    val = str(fv.value)
                elif fv.source_type == "ai_inference" and include_inferred:
                    val = str(fv.value)

            row[key] = val

        export_rows.append(row)
        
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
        webhook_triggered=webhook_triggered
    )

@router.get("/download")
def download_file(
    mode: str = "business",
    format: str = "json",
    inferred: bool = False,
    db: Session = Depends(get_db)
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
        if data:
            writer = csv.DictWriter(output, fieldnames=data[0].keys(), delimiter=";", quoting=csv.QUOTE_MINIMAL)
            writer.writeheader()
            for row in data:
                # Escape values containing semicolons
                escaped_row = {}
                for k, v in row.items():
                    if isinstance(v, str) and ";" in v:
                        escaped_row[k] = f'"{v}"'
                    else:
                        escaped_row[k] = v
                writer.writerow(escaped_row)
        
        return StreamingResponse(
            io.BytesIO(output.getvalue().encode("utf-8")),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=beauty_pim_export_{mode}.csv"}
        )

    elif format == "xlsx":
        df = pd.DataFrame(data)
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
