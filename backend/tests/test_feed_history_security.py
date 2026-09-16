import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.auth import get_password_hash
from app.models import ImportJob, ImportJobItem, User


def _headers(client, email: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/token",
        data={"username": email, "password": "securepassword123"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _job(db, owner: User, filename: str, status: str = "completed", age_minutes: int = 0):
    timestamp = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    job = ImportJob(
        id=uuid.uuid4(), filename=filename, source_name=filename,
        file_hash=uuid.uuid4().hex, status=status, total_rows=1,
        processed_rows=1 if status == "completed" else 0,
        column_mapping={"product_name": "Product Name", "brand": "Brand"},
        created_by_id=owner.id, created_at=timestamp, updated_at=timestamp,
    )
    db.add(job)
    db.flush()
    item = ImportJobItem(
        id=uuid.uuid4(), import_job_id=job.id, source_row_number=1,
        status="completed" if status == "completed" else "pending",
        match_status="not_evaluated", duplicate_score=0,
        enrichment_status="not_requested",
    )
    db.add(item)
    db.commit()
    return job, item


def test_global_feed_history_is_admin_only(client, db):
    admin = db.query(User).filter(User.email == "admin@test.com").one()
    editor = db.query(User).filter(User.email == "editor@test.com").one()
    external = User(
        email=f"external-feed-{uuid.uuid4().hex}@example.com",
        hashed_password=get_password_hash("securepassword123"),
        role="external_viewer", is_active=True,
    )
    db.add(external); db.commit()
    job, _ = _job(db, admin, "private-history.xlsx")

    admin_headers = _headers(client, admin.email)
    assert client.get("/api/feeds/jobs", headers=admin_headers).status_code == 200
    assert client.get(f"/api/feeds/jobs/{job.id}", headers=admin_headers).status_code == 200
    assert client.get(f"/api/feeds/jobs/{job.id}/items", headers=admin_headers).status_code == 200

    for email in (editor.email, "viewer@test.com", external.email):
        headers = _headers(client, email)
        assert client.get("/api/feeds/jobs", headers=headers).status_code == 403
        assert client.get(f"/api/feeds/jobs/{job.id}", headers=headers).status_code == 403
        assert client.get(f"/api/feeds/jobs/{job.id}/items", headers=headers).status_code == 403


def test_editor_can_monitor_only_own_current_job_without_history_timestamps(client, db):
    editor = db.query(User).filter(User.email == "editor@test.com").one()
    admin = db.query(User).filter(User.email == "admin@test.com").one()
    active, _ = _job(db, editor, "current-editor.csv", status="processing")
    other, _ = _job(db, admin, "another-users-private.csv", status="processing")
    historical, _ = _job(db, editor, "old-editor-history.csv", status="completed", age_minutes=60)
    headers = _headers(client, editor.email)

    response = client.get(f"/api/feeds/active-jobs/{active.id}", headers=headers)
    assert response.status_code == 200
    assert set(response.json()) == {
        "id", "filename", "status", "total_rows", "processed_rows", "error_message",
    }
    items = client.get(f"/api/feeds/active-jobs/{active.id}/items", headers=headers)
    assert items.status_code == 200
    assert "created_at" not in items.text and "import_job_id" not in items.text
    assert client.get(f"/api/feeds/active-jobs/{other.id}", headers=headers).status_code == 404
    assert client.get(f"/api/feeds/active-jobs/{other.id}/items", headers=headers).status_code == 404
    assert client.get(f"/api/feeds/active-jobs/{historical.id}", headers=headers).status_code == 404


def test_editor_upload_process_and_owned_monitoring_remain_available(client, db):
    headers = _headers(client, "editor@test.com")
    content = b"Product Name,Brand,EAN\nSecure Feed Product,Secure Brand,1234567890123\n"
    preview = client.post(
        "/api/feeds/upload", headers=headers,
        files={"file": ("editor-current.csv", content, "text/csv")},
    )
    assert preview.status_code == 200
    configuration = {
        "filename": "editor-current.csv",
        "file_hash": preview.json()["file_hash"],
        "column_mapping": {"product_name": "Product Name", "brand": "Brand", "ean": "EAN"},
        "source_name": "Editor current upload",
    }
    with patch("app.routes.feeds.run_job_in_background", return_value=None):
        process = client.post(
            "/api/feeds/process-upload", headers=headers,
            files={"file": ("editor-current.csv", content, "text/csv")},
            data={"request_json": json.dumps(configuration)},
        )
    assert process.status_code == 200, process.text
    payload = process.json()
    assert "created_at" not in payload and "updated_at" not in payload and "file_hash" not in payload
    assert client.get(f"/api/feeds/active-jobs/{payload['id']}", headers=headers).status_code == 200
    assert client.get("/api/feeds/jobs", headers=headers).status_code == 403

