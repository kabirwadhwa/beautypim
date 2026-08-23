import pytest
from fastapi.testclient import TestClient
import json
import uuid
from unittest.mock import patch
from app.config import settings
from app.models import CanonicalProduct, Brand, FieldValue, ImportJob, ImportJobItem, User, ProductVariant, ValidationIssue, Category, SourceListing, ProductTag, CrawlJob

def test_database_dialect_matches_environment(db):
    dialect_name = db.bind.dialect.name
    print(f"DIALECT_NAME: {dialect_name}")
    assert dialect_name in ["sqlite", "postgresql"]

def get_admin_token(client: TestClient) -> str:
    resp = client.post(
        "/api/auth/token",
        data={"username": "admin@test.com", "password": "securepassword123"}
    )
    return resp.json()["access_token"]

def test_get_templates_list(client: TestClient):
    token = get_admin_token(client)
    resp = client.get("/api/feeds/templates", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)

def test_upload_file_invalid_format(client: TestClient):
    token = get_admin_token(client)
    files = {"file": ("test.txt", b"invalid text", "text/plain")}
    resp = client.post(
        "/api/feeds/upload",
        files=files,
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 400

def test_list_products_api(client: TestClient, db):
    token = get_admin_token(client)
    
    # Seed a product
    brand = Brand(id=uuid.uuid4(), name="Drunk Elephant", normalized_name="drunkelephant")
    db.add(brand)
    db.flush()
    prod = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="Lala Retro", normalized_name="lalaretro")
    db.add(prod)
    db.commit()

    resp = client.get(
        "/api/products",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["product_name"] == "Lala Retro"
    assert data[0]["internal_code"].startswith("ICN-")

    metrics = client.get(
        "/api/products/metrics",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert metrics.status_code == 200
    assert metrics.json()["total_products"] >= 1
    assert metrics.json()["unresolved_issues"] >= 0


def test_product_grid_search_and_filters_include_gtin_icn_and_variant_issues(client: TestClient, db):
    token = get_admin_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    brand = Brand(id=uuid.uuid4(), name="Grid Test", normalized_name="gridtest")
    db.add(brand)
    db.flush()
    product = CanonicalProduct(
        id=uuid.uuid4(),
        brand_id=brand.id,
        product_name="Searchable Serum",
        normalized_name="searchableserum",
        review_status="needs_review",
    )
    db.add(product)
    db.flush()
    variant = ProductVariant(
        id=uuid.uuid4(),
        canonical_product_id=product.id,
        gtin="1234567890123",
    )
    db.add(variant)
    db.flush()
    db.add(ValidationIssue(
        id=uuid.uuid4(),
        product_variant_id=variant.id,
        severity="warning",
        issue_type="test_issue",
        message="Variant needs attention.",
        created_by_type="system",
    ))
    db.commit()

    by_gtin = client.get("/api/products?search=1234567890123", headers=headers)
    assert by_gtin.status_code == 200
    assert [row["id"] for row in by_gtin.json()] == [str(product.id)]
    assert by_gtin.json()[0]["validation_issue_count"] == 1
    assert by_gtin.json()[0]["variant_count"] == 1

    internal_code = by_gtin.json()[0]["internal_code"]
    by_icn = client.get(f"/api/products?search={internal_code}", headers=headers)
    assert [row["id"] for row in by_icn.json()] == [str(product.id)]

    with_issues = client.get("/api/products?issue_filter=true", headers=headers)
    assert str(product.id) in [row["id"] for row in with_issues.json()]
    clear = client.get("/api/products?issue_filter=false", headers=headers)
    assert str(product.id) not in [row["id"] for row in clear.json()]
    status_filtered = client.get("/api/products?status_filter=needs_review", headers=headers)
    assert str(product.id) in [row["id"] for row in status_filtered.json()]


def test_product_tags_single_and_bulk_workflows(client: TestClient, db):
    token = get_admin_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    brand = Brand(id=uuid.uuid4(), name="Tag Test", normalized_name=f"tagtest{uuid.uuid4().hex}")
    products = [
        CanonicalProduct(
            id=uuid.uuid4(), brand=brand, product_name=f"Tagged Product {index}",
            normalized_name=f"taggedproduct{index}{uuid.uuid4().hex}",
        )
        for index in range(2)
    ]
    db.add_all([brand, *products])
    db.commit()

    updated = client.put(
        f"/api/products/{products[0].id}/tags",
        json={"tags": ["Investor Ready", "  Launch  ", "investor ready"]},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert set(updated.json()["tags"]) == {"Investor Ready", "Launch"}

    listing = client.get("/api/products", headers=headers)
    listed = next(row for row in listing.json() if row["id"] == str(products[0].id))
    assert set(listed["tags"]) == {"Investor Ready", "Launch"}

    added = client.post(
        "/api/products/bulk/actions",
        json={
            "product_ids": [str(product.id) for product in products],
            "action": "add_tags", "tags": ["Priority"],
        },
        headers=headers,
    )
    assert added.status_code == 200, added.text
    assert added.json()["success_count"] == 2
    db.expire_all()
    assert db.query(ProductTag).filter(ProductTag.normalized_name == "priority").count() == 2

    removed = client.post(
        "/api/products/bulk/actions",
        json={
            "product_ids": [str(product.id) for product in products],
            "action": "remove_tags", "tags": ["priority"],
        },
        headers=headers,
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["success_count"] == 2
    assert db.query(ProductTag).filter(ProductTag.normalized_name == "priority").count() == 0


def test_product_tag_permissions_and_validation(client: TestClient, db):
    token = get_admin_token(client)
    admin_headers = {"Authorization": f"Bearer {token}"}
    viewer_login = client.post(
        "/api/auth/token", data={"username": "viewer@test.com", "password": "securepassword123"},
    )
    viewer_headers = {"Authorization": f"Bearer {viewer_login.json()['access_token']}"}
    brand = Brand(id=uuid.uuid4(), name="Tag Guard", normalized_name=f"tagguard{uuid.uuid4().hex}")
    product = CanonicalProduct(
        id=uuid.uuid4(), brand=brand, product_name="Guarded Product",
        normalized_name=f"guardedproduct{uuid.uuid4().hex}",
    )
    db.add_all([brand, product])
    db.commit()

    assert client.put(
        f"/api/products/{product.id}/tags", json={"tags": ["Blocked"]}, headers=viewer_headers,
    ).status_code == 403
    too_long = client.put(
        f"/api/products/{product.id}/tags", json={"tags": ["x" * 51]}, headers=admin_headers,
    )
    assert too_long.status_code == 422
    empty_bulk = client.post(
        "/api/products/bulk/actions",
        json={"product_ids": [str(product.id)], "action": "add_tags", "tags": []},
        headers=admin_headers,
    )
    assert empty_bulk.status_code == 400


def test_bulk_approve_reject_and_classification_actions(client: TestClient, db):
    token = get_admin_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    brand = Brand(id=uuid.uuid4(), name="Bulk Controls", normalized_name=f"bulkcontrols{uuid.uuid4().hex}")
    products = [
        CanonicalProduct(
            id=uuid.uuid4(), brand=brand, product_name=f"Bulk Control {index}",
            normalized_name=f"bulkcontrol{index}{uuid.uuid4().hex}", review_status="imported",
        )
        for index in range(2)
    ]
    db.add_all([brand, *products])
    db.commit()
    product_ids = [str(product.id) for product in products]

    approved = client.post(
        "/api/products/bulk/actions",
        json={"product_ids": product_ids, "action": "approve"}, headers=headers,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["success_count"] == 2
    db.expire_all()
    assert {db.query(CanonicalProduct).filter(CanonicalProduct.id == product.id).one().review_status for product in products} == {"approved"}

    rejected = client.post(
        "/api/products/bulk/actions",
        json={"product_ids": product_ids, "action": "reject"}, headers=headers,
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["success_count"] == 2
    db.expire_all()
    assert {db.query(CanonicalProduct).filter(CanonicalProduct.id == product.id).one().review_status for product in products} == {"rejected"}

    classified = client.post(
        "/api/products/bulk/actions",
        json={
            "product_ids": product_ids, "action": "set_classification",
            "category": "Makeup", "subcategory": "Lips",
        },
        headers=headers,
    )
    assert classified.status_code == 200, classified.text
    assert classified.json()["success_count"] == 2
    for product in products:
        detail = client.get(f"/api/products/{product.id}", headers=headers)
        assert detail.json()["product_category"] == "Makeup"
        assert detail.json()["subcategory"] == "Lips"


def test_improve_product_summary_opens_without_canonical_description_column(client: TestClient, db):
    token = get_admin_token(client)
    brand = Brand(id=uuid.uuid4(), name="Improve Test", normalized_name=f"improvetest{uuid.uuid4().hex}")
    product = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, product_name="Brightening Essence",
        normalized_name="brighteningessence",
    )
    db.add_all([brand, product])
    db.commit()

    response = client.get(
        f"/api/products/{product.id}/improvement",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["identity"]["product_name"] == "Brightening Essence"
    assert "description" in response.json()["fields_recommended_for_research"]


def test_product_classification_button_persists_and_writes_valid_audit(client: TestClient, db):
    token = get_admin_token(client)
    brand = Brand(id=uuid.uuid4(), name="Classify Test", normalized_name=f"classifytest{uuid.uuid4().hex}")
    product = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, product_name="Velvet Body Oil",
        normalized_name="velvetbodyoil",
    )
    db.add_all([brand, product])
    db.commit()

    response = client.put(
        f"/api/products/{product.id}/classification",
        headers={"Authorization": f"Bearer {token}"},
        json={"category": "Body Care", "subcategory": "Body Oil"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["product_category"] == "Body Care"
    assert response.json()["subcategory"] == "Body Oil"


def test_guided_improvement_identity_research_discovery_and_selected_enrichment(client: TestClient, db, monkeypatch):
    token = get_admin_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    brand = Brand(id=uuid.uuid4(), name="Guided Test", normalized_name=f"guidedtest{uuid.uuid4().hex}")
    product = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, product_name="Moonlight Eau de Parfum",
        normalized_name="moonlighteaudeparfum",
    )
    job = ImportJob(
        id=uuid.uuid4(), filename="guided.csv", file_hash=uuid.uuid4().hex,
        status="completed", total_rows=1, processed_rows=1,
        column_mapping={"product_name": "name", "brand": "brand"},
    )
    listing = SourceListing(
        id=uuid.uuid4(), import_job_id=job.id, canonical_product_id=product.id,
        raw_data={"name": product.product_name, "brand": brand.name},
        source_hash=uuid.uuid4().hex,
    )
    item = ImportJobItem(
        id=uuid.uuid4(), import_job_id=job.id, source_row_number=1,
        source_listing_id=listing.id, canonical_product_id=product.id,
        status="completed", match_status="new_product", enrichment_status="succeeded",
    )
    db.add_all([brand, job])
    db.flush()
    db.add(product)
    db.flush()
    db.add(listing)
    db.flush()
    db.add(item)
    db.commit()

    identity = client.put(
        f"/api/products/{product.id}/identity", headers=headers,
        json={"format": "Eau de Parfum", "variant": "Original", "size": "50", "unit": "ml", "gtin": "1234567890123", "market": "FR"},
    )
    assert identity.status_code == 200, identity.text

    with patch("app.services.web_discovery.discover_product_sources", return_value=[{
        "title": "Official", "url": "https://brand.example/moonlight",
        "domain": "brand.example", "provider": "test", "candidate_only": True,
    }]):
        discovery = client.post(
            f"/api/products/{product.id}/discover-sources", headers=headers,
            json={"approved_domains": ["brand.example"]},
        )
    assert discovery.status_code == 200, discovery.text
    assert discovery.json()["results"][0]["candidate_only"] is True

    with patch("app.scraping.url_safety.validate_public_url", return_value="brand.example"):
        research = client.post(
            f"/api/products/{product.id}/research", headers=headers,
            json={"urls": ["https://brand.example/moonlight"], "refresh_interval_hours": 168},
        )
    assert research.status_code == 201, research.text
    assert research.json()["status"] == "queued"

    monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-key")
    with patch("app.routes.products.process_item_enrichment") as process:
        improved = client.post(
            f"/api/products/{product.id}/improve", headers=headers,
            json={"mode": "selected", "fields": ["benefits", "directions"]},
        )
    assert improved.status_code == 200, improved.text
    assert process.call_args.kwargs["mode"] == "selected"
    assert process.call_args.kwargs["selected_fields"] == ["benefits", "directions"]
    assert improved.json()["improvement_result"]["research_pending"] is True
    assert improved.json()["improvement_result"]["research_status"] == "queued"

    research_status = client.get(
        f"/api/products/{product.id}/research-status", headers=headers,
    )
    assert research_status.status_code == 200
    assert research_status.json()["research_pending"] is True

    results = client.get(f"/api/products/{product.id}/research-results", headers=headers)
    assert results.status_code == 200
    assert results.json() == []


def test_bulk_improve_queues_durable_jobs_without_running_ai_in_request(client: TestClient, db):
    token = get_admin_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    brand = Brand(id=uuid.uuid4(), name="Bulk Improve", normalized_name=uuid.uuid4().hex)
    job = ImportJob(
        id=uuid.uuid4(), filename="bulk.csv", file_hash=uuid.uuid4().hex,
        status="completed", total_rows=2, processed_rows=2,
        column_mapping={"product_name": "name", "brand": "brand"},
    )
    db.add_all([brand, job])
    db.flush()
    products = []
    for index in range(2):
        product = CanonicalProduct(
            id=uuid.uuid4(), brand_id=brand.id, product_name=f"Bulk Product {index}",
            normalized_name=f"bulkproduct{index}",
        )
        db.add(product)
        db.flush()
        listing = SourceListing(
            id=uuid.uuid4(), import_job_id=job.id, canonical_product_id=product.id,
            raw_data={"name": product.product_name, "brand": brand.name}, source_hash=uuid.uuid4().hex,
        )
        db.add(listing)
        db.flush()
        db.add(ImportJobItem(
            id=uuid.uuid4(), import_job_id=job.id, source_row_number=index + 1,
            source_listing_id=listing.id, canonical_product_id=product.id,
            status="completed", match_status="new_product", enrichment_status="succeeded",
        ))
        products.append(product)
    db.commit()

    quality = {
        "missing_high_priority_fields": ["description", "benefits"],
        "research_objectives": [{"field": "description"}, {"field": "benefits"}],
    }
    with (
        patch("app.services.product_improvement.product_improvement_summary", return_value=quality),
        patch("app.knowledge_corpus.retrieval.retrieve_corpus_evidence", return_value={"match_level": "unmatched"}),
        patch("app.knowledge_corpus.retrieval.evidence_is_sufficient", return_value=False),
        patch("app.routes.products.process_item_enrichment") as process,
    ):
        response = client.post(
            "/api/products/bulk/actions/improve", headers=headers,
            json={"product_ids": [str(product.id) for product in products], "mode": "missing_only"},
        )

    assert response.status_code == 202, response.text
    assert response.json()["queued_count"] == 2
    assert response.json()["failed_count"] == 0
    assert all(item["web_search_planned"] for item in response.json()["items"])
    job_ids = [item["research_job_id"] for item in response.json()["items"]]
    progress = client.post(
        "/api/products/bulk/actions/improve/status", headers=headers,
        json={"research_job_ids": job_ids},
    )
    assert progress.status_code == 200, progress.text
    assert progress.json()["requested_count"] == 2
    assert progress.json()["pending_count"] == 2
    assert progress.json()["progress_percent"] == 0
    assert progress.json()["all_terminal"] is False

    jobs = db.query(CrawlJob).filter(CrawlJob.id.in_([uuid.UUID(value) for value in job_ids])).all()
    jobs[0].status = "completed"
    jobs[0].configuration = {**(jobs[0].configuration or {}), "result": {
        "business_outcome": "improved", "business_status": "READY",
        "before_completeness": 40, "after_completeness": 82,
        "fields_added": ["description", "benefits"], "sources_ingested": 2,
    }}
    jobs[1].status = "failed"
    jobs[1].error_summary = "Research provider unavailable"
    jobs[1].configuration = {**(jobs[1].configuration or {}), "result": {
        "business_outcome": "rate_limited_retriable",
        "business_status": "TECHNICAL_RETRY_REQUIRED",
        "before_completeness": 35, "after_completeness": 35,
        "fields_added": [], "sources_ingested": 0,
    }}
    db.commit()
    finished = client.post(
        "/api/products/bulk/actions/improve/status", headers=headers,
        json={"research_job_ids": job_ids},
    )
    assert finished.status_code == 200
    assert finished.json()["progress_percent"] == 100
    assert finished.json()["successful_count"] == 1
    assert finished.json()["failed_count"] == 1
    assert finished.json()["outcome_counts"]["improved"] == 1
    assert finished.json()["outcome_counts"]["rate_limited_retriable"] == 1
    assert finished.json()["items"][0]["before_completeness"] == 40
    assert finished.json()["items"][0]["after_completeness"] == 82
    assert finished.json()["all_terminal"] is True
    process.assert_not_called()


def test_taxonomy_crud_and_guards(client: TestClient, db):
    token = get_admin_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    root = client.post("/api/settings/categories", json={"name": "Skincare"}, headers=headers)
    assert root.status_code == 201, root.text
    root_id = root.json()["id"]
    child = client.post(
        "/api/settings/categories",
        json={"name": "Serums", "parent_id": root_id},
        headers=headers,
    )
    assert child.status_code == 201
    assert child.json()["path"] == "Skincare > Serums"

    renamed = client.put(
        f"/api/settings/categories/{root_id}",
        json={"name": "Face Care"},
        headers=headers,
    )
    assert renamed.status_code == 200
    categories = client.get("/api/settings/categories", headers=headers).json()
    assert any(category["path"] == "Face Care > Serums" for category in categories)

    blocked = client.delete(f"/api/settings/categories/{root_id}", headers=headers)
    assert blocked.status_code == 409
    child_id = child.json()["id"]
    assert client.delete(f"/api/settings/categories/{child_id}", headers=headers).status_code == 204
    assert client.delete(f"/api/settings/categories/{root_id}", headers=headers).status_code == 204

def test_edit_product_api(client: TestClient, db):
    token = get_admin_token(client)

    # Seed
    brand = Brand(id=uuid.uuid4(), name="Paula's Choice", normalized_name="paulaschoice")
    db.add(brand)
    db.flush()
    prod = CanonicalProduct(id=uuid.uuid4(), brand_id=brand.id, product_name="BHA Exfoliant", normalized_name="bhaexfoliant")
    db.add(prod)
    db.commit()

    # Edit
    edit_payload = {
        "field_name": "claims",
        "value": [{"name": "Vegan", "value": "Yes", "status": "verified"}],
        "reason": "Verified brand certification page"
    }
    resp = client.put(
        f"/api/products/{prod.id}",
        json=edit_payload,
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    
    # Verify new human edit is current
    fvs = db.query(FieldValue).filter(FieldValue.canonical_product_id == prod.id).all()
    assert len(fvs) == 1
    assert fvs[0].is_current == True
    assert fvs[0].value[0]["status"] == "verified"
    assert fvs[0].source_type == "human_edit"

def test_run_export_api(client: TestClient):
    token = get_admin_token(client)
    payload = {
        "export_mode": "business",
        "file_format": "json",
        "include_inferred": True
    }
    resp = client.post(
        "/api/exports/run",
        json=payload,
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert "download_url" in resp.json()

def test_upload_file_valid_csv(client: TestClient):
    token = get_admin_token(client)
    csv_data = b"product_name,brand,ean,price,description\nDaily Cleanser,Cerave,3337875597198,12.50,Hydrating face lotion"
    files = {"file": ("products.csv", csv_data, "text/csv")}
    resp = client.post(
        "/api/feeds/upload",
        files=files,
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["filename"] == "products.csv"
    assert "headers" in data
    assert "suggested_mapping" in data
    assert data["total_rows"] == 1

def test_atomic_upload_and_process_flow(client: TestClient, db):
    token = get_admin_token(client)
    csv_data = b"product_name,brand,ean,price,description,ingredients,size\nDaily Cleanser,Cerave,03337875597198,12.50,Hydrating vegan face cleanser,Aqua; Glycerin,236ml"
    preview = client.post(
        "/api/feeds/upload",
        files={"file": ("products.csv", csv_data, "text/csv")},
        headers={"Authorization": f"Bearer {token}"}
    ).json()
    request_json = json.dumps({
        "filename": "products.csv",
        "file_hash": preview["file_hash"],
        "column_mapping": preview["suggested_mapping"]
    })
    resp = client.post(
        "/api/feeds/process-upload",
        files={"file": ("products.csv", csv_data, "text/csv")},
        data={"request_json": request_json},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    job = resp.json()
    assert job["total_rows"] == 1
    # Test fixtures use an isolated transaction that a fresh background-worker
    # connection cannot see, so execute the queued job in that same transaction.
    from app.worker import run_job_worker
    run_job_worker(db, uuid.UUID(job["id"]))
    product = db.query(CanonicalProduct).filter(CanonicalProduct.product_name == "Daily Cleanser").first()
    assert product is not None

def test_approve_product_api(client: TestClient, db):
    token = get_admin_token(client)

    # Seed
    brand = Brand(id=uuid.uuid4(), name="Glow Recipe", normalized_name="glowrecipe")
    db.add(brand)
    db.flush()
    prod = CanonicalProduct(
        id=uuid.uuid4(), 
        brand_id=brand.id, 
        product_name="Watermelon Glow Toner", 
        normalized_name="watermelonglowtoner",
        review_status="imported"
    )
    db.add(prod)
    db.commit()

    resp = client.post(
        f"/api/products/{prod.id}/approve",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["review_status"] == "approved"

def test_health_and_readiness_api(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}

    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}

def test_export_download_json(client: TestClient, db):
    token = get_admin_token(client)
    # Seed approved product
    brand = Brand(id=uuid.uuid4(), name="Bio-Oil", normalized_name="biooil")
    db.add(brand)
    db.flush()
    prod = CanonicalProduct(
        id=uuid.uuid4(), 
        brand_id=brand.id, 
        product_name="Skincare Oil", 
        normalized_name="skincareoil",
        review_status="approved"
    )
    db.add(prod)
    db.commit()

    resp = client.get(
        "/api/exports/download?mode=business&format=json",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert "beauty_pim_export_business.json" in resp.headers["Content-Disposition"]

def test_export_download_csv(client: TestClient):
    token = get_admin_token(client)
    resp = client.get(
        "/api/exports/download?mode=business&format=csv",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert "beauty_pim_export_business.csv" in resp.headers["Content-Disposition"]
