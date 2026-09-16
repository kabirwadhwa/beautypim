import uuid
from unittest.mock import patch

from app.auth import get_password_hash
from app.config import Settings
from app.main import api_documentation_urls
from app.models import Brand, CanonicalProduct, FieldValue, ProductVariant, User


def _external(db, client):
    email = f"external-{uuid.uuid4().hex}@example.com"
    db.add(User(email=email, hashed_password=get_password_hash("ExternalPass123!"), role="external_viewer"))
    db.commit()
    login = client.post("/api/auth/token", data={"username": email, "password": "ExternalPass123!"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}, email


def _product(db):
    brand = Brand(id=uuid.uuid4(), name="Public Brand", normalized_name=f"public{uuid.uuid4().hex}")
    product = CanonicalProduct(
        id=uuid.uuid4(), brand=brand, product_name="Public Cream",
        normalized_name=f"publiccream{uuid.uuid4().hex}", review_status="approved",
    )
    db.add_all([brand, product])
    db.flush()
    variant = ProductVariant(id=uuid.uuid4(), canonical_product_id=product.id, gtin="7612345678901", size="50", unit="ml")
    db.add(variant)
    db.flush()
    db.add(FieldValue(
        id=uuid.uuid4(), canonical_product_id=product.id, field_name="description",
        value="Client-safe description", source_type="source_data", source_reference="user:private-admin-id",
        evidence=[{"source_header": "Product Description", "supporting_text": "safe"}],
        is_current=True, review_status="confirmed", reviewer_id=uuid.uuid4(),
    ))
    db.commit()
    return product, variant


def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key.lower()
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def test_external_viewer_can_read_products_but_receives_sanitized_contract(client, db):
    headers, own_email = _external(db, client)
    product, variant = _product(db)
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email"] == own_email
    assert set(me.json()) == {"id", "email", "display_name", "role"}

    listing = client.get(f"/api/products?search={variant.gtin}", headers=headers)
    assert listing.status_code == 200 and len(listing.json()) == 1
    detail = client.get(f"/api/products/{product.id}?variant={variant.id}", headers=headers)
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["gtin"] == variant.gtin
    assert payload["description"] == "Client-safe description"
    forbidden = {
        "created_by_id", "reviewer_id", "requested_by_id", "invited_by_id",
        "source_reference", "raw_response", "raw_payload", "prompt", "model_config",
        "token_count", "cost", "traceback", "internal_path", "enrichment_metadata",
        "corpus_evidence",
    }
    assert forbidden.isdisjoint(set(_walk_keys(payload)))
    assert "private-admin-id" not in str(payload)
    pdf = client.get(f"/api/products/{product.id}/pdf?variant={variant.id}", headers=headers)
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert b"private-admin" not in pdf.content.lower()

    hidden = CanonicalProduct(
        id=uuid.uuid4(), brand_id=product.brand_id, product_name="Unapproved Internal Draft",
        normalized_name=f"draft{uuid.uuid4().hex}", review_status="imported",
    )
    db.add(hidden); db.commit()
    assert client.get(f"/api/products/{hidden.id}", headers=headers).status_code == 404
    assert "Unapproved Internal Draft" not in client.get("/api/products", headers=headers).text


def test_external_viewer_direct_internal_and_mutation_requests_are_forbidden(client, db):
    headers, _ = _external(db, client)
    product, variant = _product(db)
    requests = [
        ("get", "/api/admin/users", None),
        ("get", "/api/admin/invitations", None),
        ("get", "/api/feeds/jobs", None),
        ("get", f"/api/feeds/jobs/{uuid.uuid4()}", None),
        ("get", f"/api/feeds/jobs/{uuid.uuid4()}/items", None),
        ("get", "/api/crawl-jobs", None),
        ("get", "/api/knowledge-corpus/metrics", None),
        ("get", "/api/products/metrics", None),
        ("get", "/api/products/identity-review-queue", None),
        ("get", f"/api/products/{product.id}/improvement", None),
        ("get", f"/api/products/{product.id}/research-status", None),
        ("get", f"/api/products/{product.id}/research-results", None),
        ("put", f"/api/products/{product.id}", {"field_name": "description", "value": "attack"}),
        ("put", f"/api/products/{product.id}/image", {"image_url": "https://example.com/a.jpg"}),
        ("put", f"/api/products/{product.id}/tags", {"tags": ["attack"]}),
        ("post", f"/api/products/{product.id}/improve", {"mode": "missing_only"}),
        ("post", f"/api/products/{product.id}/approve", None),
        ("put", f"/api/products/{product.id}/classification", {"category": "Admin", "subcategory": "Attack"}),
        ("post", "/api/products/bulk/actions", {"product_variant_ids": [str(variant.id)], "action": "approve"}),
        ("post", "/api/auth/register", {"email": "escalate@example.invalid", "password": "ExternalPass123!"}),
        ("post", "/api/catalog-assistant/chat", {"message": "show internals"}),
        ("patch", f"/api/admin/users/{uuid.uuid4()}/role", {"role": "admin"}),
        ("post", "/api/crawl-jobs", {"starting_urls": ["https://example.com"]}),
        ("post", "/api/feeds/process", {"filename": "attack.xlsx", "file_hash": "x", "column_mapping": {}}),
    ]
    for method, url, body in requests:
        response = getattr(client, method)(url, headers=headers, json=body) if body is not None else getattr(client, method)(url, headers=headers)
        assert response.status_code in {403, 404, 405, 422}, (method, url, response.status_code, response.text)
    upload = client.post(
        "/api/feeds/upload", headers=headers,
        files={"file": ("attack.csv", b"EAN,Brand\n1,Attack", "text/csv")},
    )
    assert upload.status_code == 403


def test_external_export_is_business_only_and_sanitized(client, db):
    headers, _ = _external(db, client)
    _product(db)
    business = client.get("/api/exports/download?mode=business&format=json", headers=headers)
    assert business.status_code == 200
    assert "source_reference" not in business.text and "created_by_id" not in business.text
    assert client.get("/api/exports/download?mode=audit&format=json", headers=headers).status_code == 403
    assert client.post("/api/exports/run", headers=headers, json={
        "export_mode": "business", "file_format": "json", "webhook_url": "https://example.com/hook"
    }).status_code == 403


def test_production_docs_and_cors_configuration_are_locked_down():
    assert api_documentation_urls("production") == {"docs_url": None, "redoc_url": None, "openapi_url": None}
    with __import__("pytest").raises(ValueError):
        Settings(ENVIRONMENT="production", SECRET_KEY="x" * 40, CORS_ALLOWED_ORIGINS="*")
    configured = Settings(
        ENVIRONMENT="production", SECRET_KEY="x" * 40,
        CORS_ALLOWED_ORIGINS="https://catalog.example.com",
    )
    assert configured.CORS_ALLOWED_ORIGINS == "https://catalog.example.com"


def test_external_invitation_is_anonymous_and_accepts_exact_role(client, db):
    admin = db.query(User).filter(User.email == "admin@test.com").first()
    admin.display_name = "Private Developer"
    db.commit()
    login = client.post("/api/auth/token", data={"username": admin.email, "password": "securepassword123"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)

    with patch("app.services.email.SMTPEmailService.send_invitation", side_effect=capture):
        response = client.post("/api/admin/invitations", headers=headers, json={
            "email": f"external-invitee-{uuid.uuid4().hex}@example.com", "role": "external_viewer",
        })
    assert response.status_code == 201
    assert "inviter_email" not in captured
    assert admin.email not in str(captured) and "Private Developer" not in str(captured)
    assert response.json()["role"] == "external_viewer"
    validation = client.post("/api/auth/invitations/validate", json={"token": captured["raw_token"]})
    assert validation.status_code == 200
    assert validation.json()["role"] == "external_viewer"
    assert admin.email not in validation.text and "Private Developer" not in validation.text
    accepted = client.post("/api/auth/invitations/accept", json={
        "token": captured["raw_token"], "password": "AcceptedPass123!", "password_confirm": "AcceptedPass123!",
    })
    assert accepted.status_code == 200
    invited = db.query(User).filter(User.email == captured["to_email"]).first()
    assert invited and invited.role == "external_viewer"
    assert client.post("/api/auth/invitations/accept", json={
        "token": captured["raw_token"], "password": "AcceptedPass123!", "password_confirm": "AcceptedPass123!",
    }).status_code == 409
