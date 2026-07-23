import pandas as pd
import json
import hashlib
import io
import uuid
import csv
from typing import Dict, Any, List, Tuple
from sqlalchemy.orm import Session
from app.models import ImportJob, ImportJobItem, SourceListing
from app.config import settings

def compute_file_hash(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()

def detect_delimiter(file_bytes: bytes) -> str:
    """Detect delimiters from parsed record structure, not raw character counts.

    ``csv.Sniffer`` is easily misled by comma-heavy JSON embedded in a
    semicolon-delimited export. Comparing candidate delimiters against the
    header width and subsequent record consistency correctly handles those
    fields while still respecting quoted delimiters.
    """
    sample = file_bytes[:131072].decode("utf-8-sig", errors="replace")
    candidates = [",", ";", "\t", "|"]
    scored = []
    for candidate in candidates:
        try:
            reader = csv.reader(io.StringIO(sample), delimiter=candidate)
            parsed_rows = []
            for row in reader:
                if row and any(str(value).strip() for value in row):
                    parsed_rows.append(row)
                if len(parsed_rows) >= 25:
                    break
            if not parsed_rows:
                continue
            header_width = len(parsed_rows[0])
            data_widths = [len(row) for row in parsed_rows[1:]]
            consistent_rows = sum(width == header_width for width in data_widths)
            inconsistent_distance = sum(abs(width - header_width) for width in data_widths)
            scored.append((
                header_width > 1,
                consistent_rows,
                -inconsistent_distance,
                header_width,
                candidate,
            ))
        except (csv.Error, UnicodeError):
            continue

    if not scored:
        return ","
    return max(scored)[-1]

def _validate_dataframe(df: pd.DataFrame) -> None:
    if df.empty:
        raise ValueError("The uploaded file contains no product rows.")
    if not len(df.columns):
        raise ValueError("The uploaded file has no header row.")
    headers = [str(column).strip() for column in df.columns]
    if any(not header or header.lower().startswith("unnamed:") for header in headers):
        raise ValueError("Every column must have a non-empty header.")
    duplicates = sorted({header for header in headers if headers.count(header) > 1})
    if duplicates:
        raise ValueError(f"Duplicate column headers are not supported: {', '.join(duplicates)}")
    df.columns = headers

def _read_table(file_bytes: bytes, file_type: str) -> pd.DataFrame:
    if not file_bytes:
        raise ValueError("The uploaded file is empty.")
    if file_type == "csv":
        return pd.read_csv(
            io.BytesIO(file_bytes), sep=detect_delimiter(file_bytes), dtype=str,
            keep_default_na=False, encoding="utf-8-sig"
        )
    if file_type == "xlsx":
        return pd.read_excel(io.BytesIO(file_bytes), dtype=str, keep_default_na=False)
    if file_type == "json":
        data = json.loads(file_bytes.decode("utf-8-sig"))
        if isinstance(data, list):
            products = data
        elif isinstance(data, dict) and isinstance(data.get("products"), list):
            products = data["products"]
        else:
            raise ValueError("JSON must be an array of products or contain a 'products' array.")
        if any(not isinstance(product, dict) for product in products):
            raise ValueError("Every JSON product must be an object.")
        return pd.DataFrame(products)
    raise ValueError("Unsupported file type")

def read_preview(file_bytes: bytes, file_type: str) -> Tuple[List[str], List[Dict[str, Any]], int]:
    """Reads a preview of the file (first 5 rows) and returns header columns, sample rows, and total row count.
    """
    total_rows = 0
    headers = []
    preview_rows = []

    df = _read_table(file_bytes, file_type)
    _validate_dataframe(df)
    headers = df.columns.tolist()
    total_rows = len(df)
    preview_rows = df.head(5).fillna("").to_dict(orient="records")

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
    df = _read_table(file_bytes, file_type)
    _validate_dataframe(df)

    missing_columns = sorted({column for column in column_mapping.values() if column and column not in df.columns})
    if missing_columns:
        raise ValueError(f"Mapped columns are missing from the file: {', '.join(missing_columns)}")
    for required in ("product_name", "brand"):
        source_column = column_mapping.get(required)
        if not source_column:
            raise ValueError(f"A mapping for '{required}' is required.")
        if not df[source_column].astype(str).str.strip().any():
            raise ValueError(f"The mapped '{required}' column contains no values.")

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
        "image_url": ["image", "img", "picture", "image_url", "photo"],
        "retailer": ["retailer", "retailer_name", "store", "merchant", "source"]
    }

    def normalize_header(value: str) -> str:
        return "".join(character for character in value.lower() if character.isalnum())

    claimed_headers = set()
    for canonical, keywords in mapping_keywords.items():
        normalized_keywords = [normalize_header(keyword) for keyword in keywords]
        # Exact normalized matches are reliable and avoid mistakes such as
        # mapping "manufacturer" to product_name merely because it contains "name".
        for header in headers:
            if header in claimed_headers:
                continue
            if normalize_header(str(header)) in normalized_keywords:
                suggestions[canonical] = header
                claimed_headers.add(header)
                break
        if canonical not in suggestions:
            for header in headers:
                if header in claimed_headers:
                    continue
                normalized_header = normalize_header(str(header))
                if any(
                    len(keyword) >= 3 and
                    (normalized_header.startswith(keyword) or normalized_header.endswith(keyword))
                    for keyword in normalized_keywords
                ):
                    suggestions[canonical] = header
                    claimed_headers.add(header)
                    break

    return suggestions
