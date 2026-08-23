import uuid
from unittest.mock import patch

from app.models import AuditLog, Brand, CanonicalProduct, FieldValue, User, ValidationIssue
from app.services.identity_review import (
    does_this_product_require_identity_review, persist_review_state,
    synchronize_blocking_issue,
)
from app.worker import create_field_value_version


def _product(db, name="Internal Product"):
    brand = Brand(id=uuid.uuid4(), name="Source Brand", normalized_name=uuid.uuid4().hex)
    product = CanonicalProduct(
        id=uuid.uuid4(), brand_id=brand.id, product_name=name,
        normalized_name=name.lower().replace(" ", ""),
    )
    db.add_all([brand, product]); db.flush()
    return product


def _contract(db, product, *, status="unresolved", module="unknown", fingerprint="v1", conflicts=None):
    row = FieldValue(
        id=uuid.uuid4(), canonical_product_id=product.id, field_name="product_understanding",
        value={
            "identity_status": status, "category_module": module,
            "foundational_fingerprint": fingerprint, "match_type": "unmatched",
            "identity": {"consumer_brand": {"value": product.brand.name},
                         "product_family": {"value": product.product_name}},
            "taxonomy": {}, "conflicts": conflicts or [],
            "research_plan": {"identity_first": status != "resolved", "objectives": ["category"] if status != "resolved" else []},
            "source_interpretation": {"source_brand": product.brand.name},
        },
        source_type="deterministic_rule", source_reference="test", confidence_score=.5,
        review_status="inferred", is_current=True,
    )
    db.add(row); db.flush()
    return row


def test_exact_resolved_identity_does_not_require_review(db):
    product = _product(db)
    _contract(db, product, status="resolved", module="makeup")
    decision = does_this_product_require_identity_review(db, product, {
        "identity_resolution_required": False, "identity_status": "complete",
        "missing_identity_fields": [], "corpus_match_level": "exact_product",
    })
    assert decision["requires_review"] is False
    assert decision["review_status"] == "RESOLVED"


def test_ambiguous_identity_creates_blocking_review_issue(db):
    product = _product(db, "LAT KHAMRAH DUKH")
    _contract(db, product)
    summary = {"identity_resolution_required": True, "identity_status": "ambiguous",
               "missing_identity_fields": ["category", "product_type"]}
    decision = does_this_product_require_identity_review(db, product, summary)
    synchronize_blocking_issue(db, product, decision)
    db.flush()
    issue = db.query(ValidationIssue).filter(
        ValidationIssue.canonical_product_id == product.id,
        ValidationIssue.issue_type == "foundational_identity_unresolved",
    ).one()
    assert decision["review_status"] == "NEEDS_REVIEW"
    assert issue.severity == "blocking"
    assert "Product category" in issue.message


def test_skip_is_persisted_but_identity_remains_unresolved(db):
    product = _product(db)
    _contract(db, product, fingerprint="same")
    actor = db.query(User).filter(User.email == "admin@test.com").one()
    persist_review_state(
        db, product, status="SKIPPED", fingerprint="same", actor_id=actor.id,
        reason="deferred", resume_context={"mode": "missing_only"},
    )
    db.flush()
    decision = does_this_product_require_identity_review(db, product, {
        "identity_resolution_required": True, "identity_status": "incomplete",
        "missing_identity_fields": ["product_type"],
    })
    assert decision["requires_review"] is True
    assert decision["review_status"] == "SKIPPED"


def test_human_confirmed_resolved_identity_is_reviewed_and_protected(db):
    product = _product(db)
    _contract(db, product, status="resolved", module="fragrance")
    db.add(FieldValue(
        id=uuid.uuid4(), canonical_product_id=product.id, field_name="category",
        value="Fragrance", source_type="human_edit", source_reference="user:test",
        confidence_score=1, review_status="confirmed", is_current=True,
    )); db.flush()
    decision = does_this_product_require_identity_review(db, product, {
        "identity_resolution_required": False, "identity_status": "complete",
        "missing_identity_fields": [],
    })
    assert decision["review_status"] == "REVIEWED"
    assert decision["human_confirmed_fields"] == ["category"]


def test_high_severity_conflict_has_actionable_reason(db):
    product = _product(db)
    _contract(db, product, status="conflicting", module="makeup", conflicts=[{
        "field_name": "brand", "severity": "high", "type": "human_vs_automatic",
        "human_value": "Armani Beauty", "automatic_value": "L'Oreal", "resolution": "human_value_preserved",
    }])
    decision = does_this_product_require_identity_review(db, product, {
        "identity_resolution_required": True, "identity_status": "conflicting",
        "missing_identity_fields": [],
    })
    assert decision["review_status"] == "CONFLICT"
    assert "Armani Beauty" in decision["reasons"][0]
    assert "retained" in decision["reasons"][0]


def test_later_human_correction_supersedes_older_human_value(db):
    product = _product(db)
    create_field_value_version(db, product.id, None, "category", "Skincare", "human_edit",
                               "user:one", 1, "confirmed")
    db.flush()
    create_field_value_version(db, product.id, None, "category", "Makeup", "human_edit",
                               "user:two", 1, "confirmed")
    db.flush()
    current = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product.id, FieldValue.field_name == "category",
        FieldValue.is_current == True,
    ).one()
    assert current.value == "Makeup"
    assert current.source_type == "human_edit"


def _admin_headers(client):
    response = client.post("/api/auth/token", data={
        "username": "admin@test.com", "password": "securepassword123",
    })
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_identity_review_queue_is_server_filtered(client, db):
    product = _product(db, "M DG SHOWER GEL FLORAL")
    _contract(db, product)
    db.add(ValidationIssue(
        id=uuid.uuid4(), canonical_product_id=product.id, field_name="product_understanding",
        severity="blocking", issue_type="foundational_identity_unresolved",
        message="Identity confirmation required: Product type is ambiguous.", created_by_type="system",
    )); db.commit()
    response = client.get("/api/products/identity-review-queue", headers=_admin_headers(client))
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["product_id"] == str(product.id)
    assert "ambiguous" in response.json()["items"][0]["reason"]


def test_save_identity_writes_protected_human_evidence_and_audit(client, db):
    product = _product(db, "Ambiguous Balm")
    _contract(db, product, fingerprint="open-review")
    resolved_contract = {
        "identity_status": "resolved", "category_module": "skincare",
        "foundational_fingerprint": "resolved-review", "confidence": 1,
        "identity": {"consumer_brand": {"value": "Confirmed Brand"},
                     "product_family": {"value": "Ambiguous Balm"}},
        "taxonomy": {"category": {"value": "Skin Care"},
                     "product_type": {"value": "Balm"}},
        "research_plan": {"identity_first": False, "objectives": []}, "conflicts": [],
    }
    quality = {
        "identity_resolution_required": False, "identity_status": "complete",
        "missing_identity_fields": [], "research_objectives": [], "research_phase": "attribute_completion",
        "identity_review": {"requires_review": False},
    }
    decision = {
        "requires_review": False, "review_status": "REVIEWED", "reasons": [],
        "understanding_fingerprint": "resolved-review", "conflicts": [],
    }
    with (
        patch("app.routes.products._refresh_product_understanding", return_value=resolved_contract),
        patch("app.routes.products._refresh_identity_review_gate", return_value=(quality, decision)),
        patch("app.services.identity_review.current_understanding", side_effect=[
            {"foundational_fingerprint": "open-review"}, resolved_contract, resolved_contract,
        ]),
        patch("app.services.identity_review.does_this_product_require_identity_review", return_value=decision),
    ):
        response = client.post(
            f"/api/products/{product.id}/identity-review/confirm",
            headers={**_admin_headers(client), "Content-Type": "application/json"},
            json={"identity": {"brand": "Confirmed Brand", "product_type": "Balm"},
                  "action": "save_only", "understanding_fingerprint": "open-review",
                  "resume_context": {"mode": "missing_only"}},
        )
    assert response.status_code == 200, response.text
    assert response.json()["review_status"] == "REVIEWED"
    fields = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product.id, FieldValue.source_type == "human_edit",
        FieldValue.is_current == True,
    ).all()
    assert {row.field_name for row in fields} >= {"brand", "product_type", "identity_review_state"}
    assert db.query(AuditLog).filter(AuditLog.entity_id == product.id).count() >= 1


def test_stale_identity_review_is_rejected(client, db):
    product = _product(db)
    _contract(db, product, fingerprint="newer")
    response = client.post(
        f"/api/products/{product.id}/identity-review/confirm",
        headers={**_admin_headers(client), "Content-Type": "application/json"},
        json={"identity": {"brand": "Manual"}, "action": "save_only",
              "understanding_fingerprint": "older", "resume_context": {}},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_identity_review"
