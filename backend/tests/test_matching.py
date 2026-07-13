import pytest
from sqlalchemy.orm import Session
import uuid
from app.services.deduplication import jaro_winkler_similarity, evaluate_match, merge_canonical_products
from app.models import CanonicalProduct, ProductVariant, Brand, User

def test_jaro_winkler_similarity():
    assert jaro_winkler_similarity("martha", "marhta") > 0.90
    assert jaro_winkler_similarity("dwayne", "duane") > 0.80
    assert jaro_winkler_similarity("cerave hydrating cleanser", "cerave hydration cleanser") > 0.90

def test_evaluate_match_new_product(db: Session):
    status, score, matched_id, var_id = evaluate_match(
        db=db,
        raw_name="Water Drench Cream",
        raw_brand="Peter Thomas Roth"
    )
    assert status == "new_product"
    assert matched_id is None

def test_evaluate_match_exact_ean(db: Session):
    # Seed brand
    brand = Brand(id=uuid.uuid4(), name="Cerave", normalized_name="cerave")
    db.add(brand)
    db.flush()

    # Seed canonical product & variant
    prod = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Hydrating Cleanser", normalized_name="hydratingcleanser")
    db.add(prod)
    db.flush()

    variant = ProductVariant(id=uuid.uuid4(), canonical_product_id=prod.id, size="236ml", gtin="3337875597198")
    db.add(variant)
    db.commit()

    status, score, matched_id, var_id = evaluate_match(
        db=db,
        raw_name="Hydrating Cleanser Lotion",
        raw_brand="Cerave",
        raw_gtin="3337875597198"
    )
    assert status == "exact_match"
    assert matched_id == prod.id
    assert var_id == variant.id

def test_merge_products(db: Session):
    brand = Brand(id=uuid.uuid4(), name="Clinique", normalized_name="clinique")
    db.add(brand)
    db.flush()

    prod1 = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Moisture Surge 72h", normalized_name="moisturesurge72h")
    prod2 = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Moisture Surge 100h", normalized_name="moisturesurge100h")
    db.add(prod1)
    db.add(prod2)
    db.flush()

    user = db.query(User).filter(User.role == "admin").first()

    # Merge prod2 into prod1
    merge_canonical_products(
        db=db,
        source_id=prod2.id,
        target_id=prod1.id,
        merged_by_id=user.id,
        reason="Consolidating reformulations"
    )

    db.refresh(prod2)
    assert prod2.review_status == "merged"
    assert prod2.is_deleted == True
