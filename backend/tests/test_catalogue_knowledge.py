import uuid

from app.models import Brand, CanonicalProduct, FieldValue
from app.services.catalogue_knowledge import build_catalogue_knowledge_context


def test_catalogue_context_contains_current_source_evidence(db):
    brand = Brand(id=uuid.uuid4(), name="Evidence Brand", normalized_name="evidencebrand")
    db.add(brand)
    product = CanonicalProduct(
        id=uuid.uuid4(),
        brand_id=brand.id,
        product_name="Evidence Cream",
        normalized_name="evidencecream",
    )
    db.add(product)
    db.flush()
    db.add(FieldValue(
        id=uuid.uuid4(),
        canonical_product_id=product.id,
        field_name="benefits",
        value=["hydrating"],
        source_type="source_data",
        source_reference="https://retail-data.invalid/p/123",
        confidence_score=1,
        review_status="confirmed",
        is_current=True,
    ))
    db.flush()

    context = build_catalogue_knowledge_context(db, product.id)
    assert context["accepted_or_current_fields"][0]["value"] == ["hydrating"]
    assert context["accepted_or_current_fields"][0]["source_reference"].endswith("/123")
    assert "Exact-product" in context["usage_policy"]
