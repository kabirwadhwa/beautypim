import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status, BackgroundTasks
from sqlalchemy.orm import Session
from typing import Dict, Any, List
import json
from app.database import get_db
from app.auth import get_current_user, require_editor_or_admin
from app.models import ImportJob, ImportJobItem, MappingTemplate, User
from app.services.ingestion import compute_file_hash, read_preview, suggest_mapping, ingest_file_to_source_listings
from app.worker import run_job_worker
from app.schemas import ImportJobOut, ImportJobItemOut, MappingTemplateOut, MappingTemplateCreate, IngestProcessRequest

router = APIRouter(prefix="/feeds", tags=["Feeds Ingestion"])

# Temporary in-memory file cache for process phase
# In production, files would be written to a temp folder or S3 bucket.
# For MVP, we cache file bytes indexed by file_hash
file_cache: Dict[str, bytes] = {}

@router.post("/upload", status_code=status.HTTP_200_OK)
async def upload_file_preview(
    file: UploadFile = File(...),
    current_user: User = Depends(require_editor_or_admin)
):
    # Validate format
    ext = file.filename.split(".")[-1].lower()
    if ext not in ["csv", "json", "xlsx"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file format '.{ext}'. Supported formats: CSV, JSON, XLSX"
        )
        
    contents = await file.read()
    
    # Validate size limits (50MB)
    if len(contents) > 50 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File size exceeds the 50MB limit."
        )

    file_hash = compute_file_hash(contents)
    file_cache[file_hash] = contents

    # Get preview rows and columns suggestions
    try:
        headers, preview_rows, total_rows = read_preview(contents, ext)
        suggestions = suggest_mapping(headers)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to read file preview: {str(e)}"
        )

    return {
        "filename": file.filename,
        "file_hash": file_hash,
        "file_type": ext,
        "headers": headers,
        "preview_rows": preview_rows,
        "total_rows": total_rows,
        "suggested_mapping": suggestions
    }

@router.get("/templates", response_model=List[MappingTemplateOut])
def get_templates(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return db.query(MappingTemplate).all()

@router.post("/templates", response_model=MappingTemplateOut)
def create_template(
    template_in: MappingTemplateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin)
):
    template = MappingTemplate(
        name=template_in.name,
        source_name=template_in.source_name,
        file_type=template_in.file_type,
        column_mapping=template_in.column_mapping,
        transformation_rules=template_in.transformation_rules,
        created_by_id=current_user.id
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template

@router.post("/process", response_model=ImportJobOut)
def process_ingest(
    request: IngestProcessRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin)
):
    file_bytes = file_cache.get(request.file_hash)
    if not file_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File data expired or not found. Please upload the file again."
        )

    # Check for existing job (idempotency check)
    existing_job = db.query(ImportJob).filter(ImportJob.file_hash == request.file_hash).first()
    if existing_job:
        if existing_job.status in ["pending", "processing", "completed"]:
            return existing_job
        else:
            db.delete(existing_job)
            db.commit()

    # Create Job record
    job = ImportJob(
        id=uuid.uuid4(),
        filename=request.filename,
        file_hash=request.file_hash,
        status="pending",
        column_mapping=request.column_mapping,
        created_by_id=current_user.id
    )
    db.add(job)
    db.commit()

    # Save mapping template if configured
    if request.save_template and request.template_name:
        template = MappingTemplate(
            name=request.template_name,
            source_name=request.source_name or "uploaded_file",
            file_type=request.filename.split(".")[-1].lower(),
            column_mapping=request.column_mapping,
            created_by_id=current_user.id
        )
        db.add(template)
        db.commit()

    # Parse and save Listings and items
    try:
        ext = request.filename.split(".")[-1].lower()
        total_rows = ingest_file_to_source_listings(
            db=db,
            file_bytes=file_bytes,
            file_type=ext,
            job_id=job.id,
            column_mapping=request.column_mapping
        )
        job.total_rows = total_rows
        db.commit()
    except Exception as e:
        db.rollback()
        try:
            db.query(ImportJob).filter(ImportJob.id == job.id).update({
                "status": "failed",
                "error_message": f"Failed during ingest parsing: {str(e)}"
            })
            db.commit()
        except Exception:
            db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed during ingest parsing: {str(e)}"
        )

    # Dispatch to background task worker thread
    background_tasks.add_task(run_job_worker, db, job.id)

    return job

@router.get("/jobs", response_model=List[ImportJobOut])
def list_jobs(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return db.query(ImportJob).order_by(ImportJob.created_at.desc()).all()

@router.get("/jobs/{job_id}", response_model=ImportJobOut)
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    job = db.query(ImportJob).filter(ImportJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

@router.get("/jobs/{job_id}/items", response_model=List[ImportJobItemOut])
def get_job_items(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    items = db.query(ImportJobItem).filter(ImportJobItem.import_job_id == job_id).all()
    return items

@router.post("/jobs/{job_id}/cancel", response_model=ImportJobOut)
def cancel_job(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor_or_admin)
):
    job = db.query(ImportJob).filter(ImportJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job.status not in ["pending", "processing"]:
        raise HTTPException(status_code=400, detail="Cannot cancel completed/failed job")

    job.status = "cancelled"
    # Cancel pending items
    db.query(ImportJobItem).filter(
        ImportJobItem.import_job_id == job_id,
        ImportJobItem.status == "pending"
    ).update({"status": "cancelled", "enrichment_status": "cancelled"})
    
    db.commit()
    return job
