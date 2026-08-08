import uuid

from app.models import Brand, CanonicalProduct, Category, FieldValue
from app.routes.products import _product_expected_format
from app.services.product_identity import product_version_compatible, product_version_label


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
    assert "Eau de Toilette" in expected
