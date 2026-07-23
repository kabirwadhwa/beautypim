import hashlib
import uuid

from app.config import settings
from app.models import (
    Brand,
    CanonicalProduct,
    Category,
    FieldValue,
    Formulation,
    ImportJob,
    SourceListing,
)


def auth_headers(client):
    response = client.post(
        "/api/auth/token",
        data={"username": "viewer@test.com", "password": "securepassword123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def seed_product(
    db,
    *,
    name,
    brand_name,
    category_path,
    product_type,
    description,
    ingredients,
    hydration=False,
    vegan=False,
):
    brand = Brand(
        id=uuid.uuid4(),
        name=brand_name,
        normalized_name=brand_name.lower().replace(" ", ""),
    )
    root_name = category_path.split(" > ")[0]
    root = db.query(Category).filter(Category.path == root_name).first()
    if not root:
        root = Category(id=uuid.uuid4(), name=root_name, path=root_name, level=0)
        db.add(root)
        db.flush()
    category = db.query(Category).filter(Category.path == category_path).first()
    if not category:
        category = Category(
            id=uuid.uuid4(),
            name=category_path.split(" > ")[-1],
            path=category_path,
            level=1,
            parent_id=root.id,
        )
        db.add(category)
        db.flush()
    db.add(brand)
    db.flush()
    product = CanonicalProduct(
        id=uuid.uuid4(),
        product_name=name,
        normalized_name=name.lower().replace(" ", ""),
        brand_id=brand.id,
        category_id=category.id,
        review_status="approved",
    )
    db.add(product)
    db.flush()
    job = ImportJob(
        id=uuid.uuid4(),
        filename=f"{name}.csv",
        file_hash=hashlib.sha256(name.encode()).hexdigest(),
        status="completed",
        total_rows=1,
        processed_rows=1,
        column_mapping={"description": "description"},
    )
    db.add(job)
    db.flush()
    db.add(SourceListing(
        id=uuid.uuid4(),
        import_job_id=job.id,
        canonical_product_id=product.id,
        raw_data={"description": description},
        source_hash=hashlib.sha256(description.encode()).hexdigest(),
    ))
    db.add(Formulation(
        id=uuid.uuid4(),
        canonical_product_id=product.id,
        raw_inci_text=ingredients,
        content_hash=hashlib.sha256(ingredients.encode()).hexdigest(),
    ))
    for field_name, value, status in (
        ("product_type", product_type, "inferred"),
        ("hydration", hydration, "explicit" if hydration else "not_targeted"),
        ("vegan", "yes" if vegan else "unknown", "explicit_brand_claim" if vegan else "unknown"),
    ):
        db.add(FieldValue(
            id=uuid.uuid4(),
            canonical_product_id=product.id,
            field_name=field_name,
            value=value,
            source_type="ai_inference",
            review_status="inferred",
            semantic_status=status,
            is_current=True,
        ))
    db.commit()
    return product


def test_catalogue_assistant_returns_only_grounded_matches(client, db, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    serum = seed_product(
        db,
        name="Cloudberry Water Serum",
        brand_name="Northlight Lab",
        category_path="Skincare > Serum",
        product_type="serum",
        description="A lightweight hydrating facial serum.",
        ingredients="Aqua, Glycerin, Cloudberry Extract",
        hydration=True,
        vegan=True,
    )
    seed_product(
        db,
        name="Ember Body Oil",
        brand_name="Copper Ritual",
        category_path="Body Care > Body Oil",
        product_type="body oil",
        description="A nourishing body oil.",
        ingredients="Squalane, Retinol, Tocopherol",
        hydration=False,
    )

    response = client.post(
        "/api/assistant/chat",
        headers=auth_headers(client),
        json={"message": "Show me skincare products", "history": []},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total_matches"] == 1
    assert payload["products"][0]["id"] == str(serum.id)
    assert payload["products"][0]["description"] == "A lightweight hydrating facial serum."
    assert payload["products"][0]["category"] == "Skincare > Serum"


def test_catalogue_assistant_applies_exclusions_and_attribute_filters(client, db, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    safe_oil = seed_product(
        db,
        name="Meadow Body Oil",
        brand_name="Aster Grove",
        category_path="Body Care > Body Oil",
        product_type="body oil",
        description="A vegan botanical body oil.",
        ingredients="Squalane, Jojoba Oil, Tocopherol",
        vegan=True,
    )
    seed_product(
        db,
        name="Renewal Body Oil",
        brand_name="Night Theory",
        category_path="Body Care > Body Oil",
        product_type="body oil",
        description="A night body oil.",
        ingredients="Squalane, Retinol",
        vegan=True,
    )

    response = client.post(
        "/api/assistant/chat",
        headers=auth_headers(client),
        json={"message": "Find vegan body oils without retinol", "history": []},
    )
    assert response.status_code == 200
    products = response.json()["products"]
    assert [product["id"] for product in products] == [str(safe_oil.id)]
    assert "vegan" in response.json()["interpreted_filters"]["claims"]
    assert "retinol" in response.json()["interpreted_filters"]["ingredients_exclude"]


def test_catalogue_assistant_requires_authentication(client):
    response = client.post(
        "/api/assistant/chat",
        json={"message": "Show me skincare products", "history": []},
    )
    assert response.status_code == 401


def test_catalogue_assistant_never_returns_nonexistent_products(client, db, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    response = client.post(
        "/api/assistant/chat",
        headers=auth_headers(client),
        json={"message": "Show me Quantum Moonbeam Foundation", "history": []},
    )
    assert response.status_code == 200
    assert response.json()["products"] == []
    assert response.json()["total_matches"] == 0
