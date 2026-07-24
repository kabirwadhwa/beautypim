import hashlib
import uuid
from unittest.mock import Mock

from app.config import settings
from app.routes.catalog_assistant import interpret_question
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


def test_catalogue_assistant_matches_legacy_source_category(client, db, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    brand = Brand(
        id=uuid.uuid4(),
        name="Legacy Beauty",
        normalized_name="legacybeauty",
    )
    db.add(brand)
    db.flush()
    product = CanonicalProduct(
        id=uuid.uuid4(),
        product_name="Archive Gel Cleanser",
        normalized_name="archivegelcleanser",
        brand_id=brand.id,
        category_id=None,
        review_status="imported",
    )
    db.add(product)
    job = ImportJob(
        id=uuid.uuid4(),
        filename="legacy.csv",
        file_hash=hashlib.sha256(b"legacy").hexdigest(),
        status="completed",
        total_rows=1,
        processed_rows=1,
        column_mapping={"category": "department", "description": "copy"},
    )
    db.add(job)
    db.flush()
    db.add(SourceListing(
        id=uuid.uuid4(),
        import_job_id=job.id,
        canonical_product_id=product.id,
        raw_data={
            "department": "Skincare",
            "copy": "A gentle archive cleanser.",
        },
        source_hash=hashlib.sha256(b"legacy-row").hexdigest(),
    ))
    db.commit()

    response = client.post(
        "/api/assistant/chat",
        headers=auth_headers(client),
        json={"message": "Show me skincare products", "history": []},
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["products"]] == [str(product.id)]
    assert response.json()["products"][0]["description"] == "A gentle archive cleanser."


def test_ai_interpretation_cannot_weaken_explicit_product_type_or_exclusion(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-key")
    ai_payload = {
        "query_terms": [],
        "brand_names": [],
        "category_terms": ["body care"],
        "product_types": [],
        "ingredients_include": [],
        "ingredients_exclude": [],
        "concerns": [],
        "claims": [],
        "review_statuses": [],
        "explanation": "Broad body care search",
        "limit": 20,
    }
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "choices": [{"message": {"content": __import__("json").dumps(ai_payload)}}]
    }
    monkeypatch.setattr(
        "app.routes.catalog_assistant.requests.post",
        Mock(return_value=response),
    )

    filters, provider = interpret_question(
        "Find vegan body oils without retinol",
        [],
        [],
        ["Body Care > Body Oil"],
    )
    assert provider.startswith("OpenAI")
    assert "body oil" in filters["product_types"]
    assert "vegan" in filters["claims"]
    assert "retinol" in filters["ingredients_exclude"]


def test_named_product_question_returns_a_grounded_explanation_not_the_whole_catalogue(
    client, db, monkeypatch
):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    lip_oil = seed_product(
        db,
        name="Chromatic Dew Lip Oil",
        brand_name="Arc & Ember",
        category_path="Makeup > Lip Oil",
        product_type="lip oil",
        description="A glossy tinted lip oil with jojoba and raspberry seed oils.",
        ingredients="Jojoba Oil, Raspberry Seed Oil, Tocopherol",
        vegan=True,
    )
    seed_product(
        db,
        name="Solar Fig Body Nectar",
        brand_name="Golden Syntax",
        category_path="Body Care > Body Oil",
        product_type="body oil",
        description="A shimmering dry-touch body oil.",
        ingredients="Squalane, Sunflower Seed Oil",
    )

    response = client.post(
        "/api/assistant/chat",
        headers=auth_headers(client),
        json={
            "message": "Tell me more about Chromatic Dew Lip Oil, what is it?",
            "history": [],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["interpreted_filters"]["intent"] == "product_detail"
    assert payload["total_matches"] == 1
    assert [item["id"] for item in payload["products"]] == [str(lip_oil.id)]
    assert "glossy tinted lip oil" in payload["answer"].lower()
    assert "jojoba oil" in payload["answer"].lower()
    assert "i found 2" not in payload["answer"].lower()


def test_follow_up_can_resolve_the_product_from_conversation_history(client, db, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    product = seed_product(
        db,
        name="Chromatic Dew Lip Oil",
        brand_name="Arc & Ember",
        category_path="Makeup > Lip Oil",
        product_type="lip oil",
        description="A glossy tinted lip oil.",
        ingredients="Jojoba Oil, Tocopherol",
    )

    response = client.post(
        "/api/assistant/chat",
        headers=auth_headers(client),
        json={
            "message": "What are its ingredients?",
            "history": [
                {
                    "role": "assistant",
                    "content": "Products shown: Chromatic Dew Lip Oil by Arc & Ember",
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["interpreted_filters"]["intent"] == "product_detail"
    assert [item["id"] for item in payload["products"]] == [str(product.id)]
    assert "jojoba oil" in payload["answer"].lower()


def test_catalogue_summary_answers_counts_brands_and_categories(client, db, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    seed_product(
        db,
        name="Cloud Serum",
        brand_name="Northlight Lab",
        category_path="Skincare > Serum",
        product_type="serum",
        description="A hydrating serum.",
        ingredients="Aqua, Glycerin",
    )
    seed_product(
        db,
        name="Meadow Oil",
        brand_name="Aster Grove",
        category_path="Body Care > Body Oil",
        product_type="body oil",
        description="A body oil.",
        ingredients="Squalane",
    )

    response = client.post(
        "/api/assistant/chat",
        headers=auth_headers(client),
        json={
            "message": "How many products, brands and categories are in the catalogue?",
            "history": [],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["interpreted_filters"]["intent"] == "catalogue_summary"
    assert payload["total_matches"] == 2
    assert "2 matching products" in payload["answer"]
    assert "2 brands" in payload["answer"]
