import uuid

from app.models import Brand, CanonicalProduct, Category, FieldValue
from app.routes.products import _product_expected_format
from app.services.product_identity import (
    product_is_fragrance, product_version_compatible, product_version_label,
    research_identity_compatible,
    trusted_product_version,
)


def test_product_version_label_prefers_specific_fragrance_concentrations():
    assert product_version_label("Sauvage Eau de Toilette") == "eau_de_toilette"
    assert product_version_label("Sauvage EDP") == "eau_de_parfum"
    assert product_version_label("Sauvage Parfum") == "parfum"


def test_product_version_guard_rejects_cross_edition_evidence():
    assert product_version_compatible("Eau de Toilette", "Sauvage Eau de Toilette 100ml")
    assert not product_version_compatible("Eau de Toilette", "Sauvage Parfum 100ml")
    assert not product_version_compatible("Eau de Parfum", "Sauvage Elixir")


def test_product_version_guard_allows_missing_edition_signal():
    assert product_version_compatible("Eau de Toilette", "Sauvage fragrance collection")
    assert product_version_compatible("Serum", "Niacinamide serum")


def test_exact_distinctive_product_name_can_correct_a_feed_version_label():
    assert research_identity_compatible(
        "Eau de Toilette", "Armani Stronger With You Amber Eau de Parfum",
        "Stronger With You Amber",
    )
    assert not research_identity_compatible(
        "Eau de Toilette", "Dior Sauvage Parfum", "Sauvage",
    )


def test_expected_format_reads_category_without_product_relationship(db):
    brand = Brand(id=uuid.uuid4(), name="Dior", normalized_name="dior")
    category = Category(id=uuid.uuid4(), name="Eau de Toilette", level=1, path="Perfume > Eau de Toilette")
    product = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, category_id=category.id,
        product_name="Dior Sauvage", normalized_name="dior sauvage", review_status="imported",
    )
    db.add_all([brand, category, product]); db.flush()
    db.add(FieldValue(
        id=uuid.uuid4(), canonical_product_id=product.id, field_name="product_type",
        value="Perfume", source_type="source_data", confidence_score=1,
        review_status="confirmed", is_current=True,
    ))
    db.flush()

    expected = _product_expected_format(db, product)

    assert "Perfume" in expected
    assert "Eau de Toilette" not in expected


def test_ai_inferred_fragrance_concentration_is_not_trusted_identity(db):
    brand = Brand(id=uuid.uuid4(), name="Dior", normalized_name="dior")
    category = Category(id=uuid.uuid4(), name="Fragrance", level=1, path="Perfume > Fragrance")
    product = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, category_id=category.id,
        product_name="Dior Sauvage", normalized_name="dior sauvage", review_status="imported",
    )
    db.add_all([brand, category, product]); db.flush()
    db.add(FieldValue(
        id=uuid.uuid4(), canonical_product_id=product.id, field_name="product_type",
        value="Eau de Parfum", source_type="ai_inference", confidence_score=.8,
        review_status="inferred", is_current=True,
    ))
    db.flush()

    assert product_is_fragrance(db, product)
    assert trusted_product_version(db, product) is None
    assert "Eau de Parfum" not in _product_expected_format(db, product)
