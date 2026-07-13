import pandas as pd
import json
import hashlib
import io
import uuid
from typing import Dict, Any, List, Tuple
from sqlalchemy.orm import Session
from app.models import ImportJob, ImportJobItem, SourceListing
from app.config import settings

def compute_file_hash(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()

def detect_delimiter(file_bytes: bytes) -> str:
    # Read first line to detect separator
    line = file_bytes.split(b'\n')[0].decode('utf-8', errors='ignore')
    if ';' in line:
        return ';'
    return ','

def read_preview(file_bytes: bytes, file_type: str) -> Tuple[List[str], List[Dict[str, Any]], int]:
    """Reads a preview of the file (first 5 rows) and returns header columns, sample rows, and total row count.
    """
    total_rows = 0
    headers = []
    preview_rows = []

    if file_type == "csv":
        delim = detect_delimiter(file_bytes)
        df = pd.read_csv(io.BytesIO(file_bytes), sep=delim, nrows=5)
        headers = df.columns.tolist()
        preview_rows = df.fillna("").to_dict(orient="records")
        # Count total rows
        df_full = pd.read_csv(io.BytesIO(file_bytes), sep=delim, usecols=[0])
        total_rows = len(df_full)

    elif file_type == "xlsx":
        df = pd.read_excel(io.BytesIO(file_bytes), nrows=5)
        headers = df.columns.tolist()
        preview_rows = df.fillna("").to_dict(orient="records")
        df_full = pd.read_excel(io.BytesIO(file_bytes), usecols=[0])
        total_rows = len(df_full)

    elif file_type == "json":
        data = json.loads(file_bytes.decode('utf-8'))
        if isinstance(data, list):
            total_rows = len(data)
            sample = data[:5]
            if sample:
                headers = list(sample[0].keys())
                preview_rows = sample
        elif isinstance(data, dict) and "products" in data:
            products = data["products"]
            total_rows = len(products)
            sample = products[:5]
            if sample:
                headers = list(sample[0].keys())
                preview_rows = sample
        else:
            raise ValueError("Unsupported JSON layout. Must be a list of products or contain a 'products' array.")

    # Convert UUIDs or objects to strings for serialization
    for row in preview_rows:
        for k, v in row.items():
            if not isinstance(v, (str, int, float, bool, type(None))):
                row[k] = str(v)

    return headers, preview_rows, total_rows

def ingest_file_to_source_listings(
    db: Session,
    file_bytes: bytes,
    file_type: str,
    job_id: uuid.UUID,
    column_mapping: Dict[str, str]
) -> int:
    """Parses the uploaded file and stores each row as a SourceListing.
    Creates corresponding ImportJobItem tasks.
    """
    if file_type == "csv":
        delim = detect_delimiter(file_bytes)
        df = pd.read_csv(io.BytesIO(file_bytes), sep=delim)
    elif file_type == "xlsx":
        df = pd.read_excel(io.BytesIO(file_bytes))
    elif file_type == "json":
        data = json.loads(file_bytes.decode('utf-8'))
        if isinstance(data, list):
            df = pd.DataFrame(data)
        else:
            df = pd.DataFrame(data.get("products", []))
    else:
        raise ValueError("Unsupported file type")

    df = df.fillna("")
    records = df.to_dict(orient="records")

    def clean_row_for_serialization(row_dict: Dict[str, Any]) -> Dict[str, Any]:
        import numpy as np
        cleaned = {}
        for k, v in row_dict.items():
            if pd.isna(v):
                cleaned[k] = None
            elif isinstance(v, np.integer):
                cleaned[k] = int(v)
            elif isinstance(v, np.floating):
                cleaned[k] = float(v)
            elif isinstance(v, np.ndarray):
                cleaned[k] = v.tolist()
            else:
                cleaned[k] = v
        return cleaned

    row_count = 0
    for idx, row in enumerate(records):
        row = clean_row_for_serialization(row)
        source_row_num = idx + 1
        
        # Serialize raw row content for unique tracking hash
        raw_row_str = json.dumps(row, sort_keys=True)
        # Unique local constraint: import_job_id + source_row_number
        # Content Hash is used for comparison
        source_hash = hashlib.sha256(f"{job_id}-{source_row_num}-{raw_row_str}".encode('utf-8')).hexdigest()
        
        # Extract retailer and url if provided in mapping
        url_col = column_mapping.get("product_url")
        retailer_col = column_mapping.get("retailer")
        
        source_url = str(row[url_col]) if url_col and url_col in row else None
        retailer = str(row[retailer_col]) if retailer_col and retailer_col in row else "unknown"

        # Create SourceListing
        listing = SourceListing(
            id=uuid.uuid4(),
            import_job_id=job_id,
            raw_data=row,
            source_hash=source_hash,
            source_url=source_url,
            retailer=retailer
        )
        db.add(listing)
        db.flush()

        # Create Job Item tracker
        item = ImportJobItem(
            id=uuid.uuid4(),
            import_job_id=job_id,
            source_row_number=source_row_num,
            source_listing_id=listing.id,
            status="pending",
            match_status="not_evaluated",
            enrichment_status="not_requested"
        )
        db.add(item)
        row_count += 1

    db.commit()
    return row_count

def suggest_mapping(headers: List[str]) -> Dict[str, str]:
    """Provides heuristics to suggest mappings for raw column headers to canonical fields.
    """
    suggestions = {}
    mapping_keywords = {
        "product_name": ["name", "title", "product_name", "product_title", "label"],
        "brand": ["brand", "marque", "manufacturer", "vendor"],
        "ean": ["ean", "upc", "gtin", "barcode", "code-barre"],
        "description": ["description", "desc", "details", "info"],
        "ingredients": ["ingredients", "inci", "composition", "ingredients_list"],
        "category": ["category", "type", "rayon", "classification"],
        "price": ["price", "prix", "cost", "value"],
        "size": ["size", "volume", "capacity", "continence", "format"],
        "product_url": ["url", "link", "product_url", "href"],
        "image_url": ["image", "img", "picture", "image_url", "photo"]
    }

    for canonical, keywords in mapping_keywords.items():
        for header in headers:
            clean_header = header.lower().strip().replace("_", "").replace(" ", "")
            for keyword in keywords:
                if keyword in clean_header:
                    suggestions[canonical] = header
                    break
            if canonical in suggestions:
                break

    return suggestions
