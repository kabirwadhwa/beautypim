import uuid
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.models import (
    CanonicalProduct, ProductVariant, Brand, Category, 
    ImportJobItem, SourceListing, CanonicalProductMerge, FieldValue
)

def jaro_distance(s1: str, s2: str) -> float:
    if s1 == s2:
        return 1.0
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0

    max_dist = max(len1, len2) // 2 - 1
    if max_dist < 0:
        max_dist = 0

    match1 = [False] * len1
    match2 = [False] * len2
    matches = 0
    transpositions = 0

    for i in range(len1):
        start = max(0, i - max_dist)
        end = min(len2, i + max_dist + 1)
        for j in range(start, end):
            if not match2[j] and s1[i] == s2[j]:
                match1[i] = True
                match2[j] = True
                matches += 1
                break

    if matches == 0:
        return 0.0

    k = 0
    for i in range(len1):
        if match1[i]:
            while not match2[k]:
                k += 1
            if s1[i] != s2[k]:
                transpositions += 1
            k += 1
    transpositions //= 2

    return (matches / len1 + matches / len2 + (matches - transpositions) / matches) / 3.0

def jaro_winkler_similarity(s1: str, s2: str) -> float:
    jaro_sim = jaro_distance(s1, s2)
    prefix_len = 0
    for i in range(min(len(s1), len(s2))):
        if s1[i] == s2[i]:
            prefix_len += 1
        else:
            break
        if prefix_len == 4:
            break
    return jaro_sim + prefix_len * 0.1 * (1.0 - jaro_sim)

def normalize_text(text: str) -> str:
    if not text:
        return ""
    return "".join(c.lower() for c in text if c.isalnum() or c.isspace()).strip()

def evaluate_match(
    db: Session,
    raw_name: str,
    raw_brand: str,
    raw_gtin: Optional[str] = None,
    raw_size: Optional[str] = None,
    raw_category: Optional[str] = None
) -> Tuple[str, float, Optional[uuid.UUID], Optional[uuid.UUID]]:
    """Evaluates raw listing fields against database to detect matches.
    Returns Tuple[match_status, score, matched_canonical_id, matched_variant_id]
    """
    norm_name = normalize_text(raw_name)
    norm_brand = normalize_text(raw_brand)

    # 1. Exact GTIN Match (Variant level)
    if raw_gtin:
        variant = db.query(ProductVariant).filter(
            ProductVariant.gtin == raw_gtin, 
            ProductVariant.is_deleted == False
        ).first()
        if variant:
            return "exact_match", 1.0, variant.canonical_product_id, variant.id

    # Find or Create Brand reference
    brand = db.query(Brand).filter(Brand.normalized_name == norm_brand).first()
    if not brand:
        # No matching brand exists, this must be a new product
        return "new_product", 0.0, None, None

    # Retrieve products belonging to this Brand
    products = db.query(CanonicalProduct).filter(
        CanonicalProduct.brand_id == brand.id,
        CanonicalProduct.is_deleted == False
    ).all()

    candidates: List[Tuple[CanonicalProduct, float]] = []
    for prod in products:
        score = jaro_winkler_similarity(norm_name, prod.normalized_name)
        candidates.append((prod, score))

    # Sort candidates by score descending
    candidates.sort(key=lambda x: x[1], reverse=True)

    if not candidates:
        return "new_product", 0.0, None, None

    best_prod, best_score = candidates[0]

    # Check for exact matches deterministically
    exact_variant = None
    if best_score > 0.98:
        # Check if variant size matches
        if raw_size:
            exact_variant = db.query(ProductVariant).filter(
                ProductVariant.canonical_product_id == best_prod.id,
                ProductVariant.size == raw_size,
                ProductVariant.is_deleted == False
            ).first()
        
        # Rule: Exact Brand, exact Name, size compatible -> deterministic_match
        return "deterministic_match", best_score, best_prod.id, (exact_variant.id if exact_variant else None)

    # Fuzzy matches checks
    if best_score >= 0.92:
        # If there is a runner-up close candidate, mark as ambiguous
        if len(candidates) > 1 and (best_score - candidates[1][1]) < 0.03:
            return "ambiguous", best_score, None, None
        return "candidate", best_score, best_prod.id, None

    if best_score >= 0.80:
        return "candidate", best_score, best_prod.id, None

    return "new_product", best_score, None, None

def merge_canonical_products(
    db: Session,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    merged_by_id: uuid.UUID,
    reason: str
) -> CanonicalProductMerge:
    """Merges source canonical product into target canonical product.
    Updates relationships and marks source as merged.
    """
    source_product = db.query(CanonicalProduct).filter(CanonicalProduct.id == source_id).first()
    target_product = db.query(CanonicalProduct).filter(CanonicalProduct.id == target_id).first()
    
    if not source_product or not target_product:
        raise ValueError("Source or Target product does not exist")

    # Update Source Listings
    db.query(SourceListing).filter(SourceListing.canonical_product_id == source_id).update(
        {"canonical_product_id": target_id}
    )
    
    # Update Product Variants
    db.query(ProductVariant).filter(ProductVariant.canonical_product_id == source_id).update(
        {"canonical_product_id": target_id}
    )
    
    # Update Field Values (move and set to non-current)
    db.query(FieldValue).filter(FieldValue.canonical_product_id == source_id).update(
        {"canonical_product_id": target_id, "is_current": False}
    )
    
    # Create Merge Tombstone Record
    merge_record = CanonicalProductMerge(
        id=uuid.uuid4(),
        source_product_id=source_id,
        target_product_id=target_id,
        merged_by_id=merged_by_id,
        reason=reason
    )
    db.add(merge_record)

    # Set source product status to merged
    source_product.review_status = "merged"
    source_product.is_deleted = True # soft delete source product
    
    db.commit()
    return merge_record
