import pytest
import uuid
import hashlib
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.models import User, UserInvitation, AuditLog
from app.auth import get_password_hash
from app.config import settings

@pytest.fixture
def test_users(db: Session):
    admin = db.query(User).filter(User.email == "admin@test.com").first()
    editor = db.query(User).filter(User.email == "editor@test.com").first()
    viewer = db.query(User).filter(User.email == "viewer@test.com").first()
    
    # Reset to pristine state to avoid cross-test pollution
    admin.role = "admin"
    admin.is_active = True
    editor.role = "editor"
    editor.is_active = True
    viewer.role = "viewer"
    viewer.is_active = True
    
    # Delete test invitations
    db.query(UserInvitation).delete()
    # Delete temporary test users created during acceptance tests
    db.query(User).filter(User.email.notin_(["admin@test.com", "editor@test.com", "viewer@test.com"])).delete()
    
    db.commit()
    return {"admin": admin, "editor": editor, "viewer": viewer}

@pytest.fixture
def auth_headers(client: TestClient):
    # Get tokens
    def _headers(email: str):
        resp = client.post(
            f"{settings.API_V1_STR}/auth/token",
            data={"username": email, "password": "securepassword123"}
        )
        assert resp.status_code == 200
        token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}
    return _headers

def test_admin_creates_viewer_and_editor_invitations(client: TestClient, test_users, auth_headers, db: Session):
    headers = auth_headers("admin@test.com")
    
    with patch("app.services.email.SMTPEmailService.send_invitation") as mock_send:
        resp = client.post(
            f"{settings.API_V1_STR}/admin/invitations",
            json={"email": "invited_viewer@test.com", "role": "viewer"},
            headers=headers
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["email"] == "invited_viewer@test.com"
        assert data["role"] == "viewer"
        assert data["status"] == "pending"
        
        # Verify token is stored only as a hash
        invitation = db.query(UserInvitation).filter(UserInvitation.id == data["id"]).first()
        assert invitation.token_hash != ""
        
        # Verify call arguments
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        raw_token = kwargs.get("raw_token") or args[2]
        assert raw_token != ""
        assert hashlib.sha256(raw_token.encode('utf-8')).hexdigest() == invitation.token_hash

def test_editor_and_viewer_cannot_invite(client: TestClient, test_users, auth_headers):
    # Editor
    headers = auth_headers("editor@test.com")
    resp = client.post(
        f"{settings.API_V1_STR}/admin/invitations",
        json={"email": "new_guest@test.com", "role": "viewer"},
        headers=headers
    )
    assert resp.status_code == 403

    # Viewer
    headers = auth_headers("viewer@test.com")
    resp = client.post(
        f"{settings.API_V1_STR}/admin/invitations",
        json={"email": "new_guest2@test.com", "role": "viewer"},
        headers=headers
    )
    assert resp.status_code == 403

def test_invitation_validation_endpoint(client: TestClient, test_users, auth_headers, db: Session):
    admin_headers = auth_headers("admin@test.com")
    
    # Create invitation
    with patch("app.services.email.SMTPEmailService.send_invitation") as mock_send:
        client.post(
            f"{settings.API_V1_STR}/admin/invitations",
            json={"email": "validate_guest@test.com", "role": "editor"},
            headers=admin_headers
        )
        raw_token = mock_send.call_args[1].get("raw_token") or mock_send.call_args[0][2]
        
    # Validate valid token
    resp = client.post(
        f"{settings.API_V1_STR}/auth/invitations/validate",
        json={"token": raw_token}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert data["email"] == "validate_guest@test.com"
    assert data["role"] == "editor"
    
    # Validate invalid token returns generic error
    resp = client.post(
        f"{settings.API_V1_STR}/auth/invitations/validate",
        json={"token": "wrong_token_here"}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid or expired invitation link"

def test_invitation_acceptance_flows(client: TestClient, test_users, auth_headers, db: Session):
    admin_headers = auth_headers("admin@test.com")
    
    # 1. Create invitation
    with patch("app.services.email.SMTPEmailService.send_invitation") as mock_send:
        client.post(
            f"{settings.API_V1_STR}/admin/invitations",
            json={"email": "accept_guest@test.com", "role": "editor"},
            headers=admin_headers
        )
        raw_token = mock_send.call_args[1].get("raw_token") or mock_send.call_args[0][2]
        
    # 2. Accept
    resp = client.post(
        f"{settings.API_V1_STR}/auth/invitations/accept",
        json={"token": raw_token, "password": "newpassword123", "password_confirm": "newpassword123"}
    )
    assert resp.status_code == 200
    
    # 3. Check created user
    user = db.query(User).filter(User.email == "accept_guest@test.com").first()
    assert user is not None
    assert user.role == "editor"
    assert user.is_active is True
    
    # 4. Expired token rejection
    with patch("app.services.email.SMTPEmailService.send_invitation") as mock_send:
        client.post(
            f"{settings.API_V1_STR}/admin/invitations",
            json={"email": "expired_guest@test.com", "role": "editor"},
            headers=admin_headers
        )
        raw_token_expired = mock_send.call_args[1].get("raw_token") or mock_send.call_args[0][2]
    # Artificially expire
    inv = db.query(UserInvitation).filter(UserInvitation.email == "expired_guest@test.com").first()
    inv.expires_at = datetime.utcnow() - timedelta(hours=1)
    db.add(inv)
    db.commit()
    
    resp = client.post(
        f"{settings.API_V1_STR}/auth/invitations/accept",
        json={"token": raw_token_expired, "password": "newpassword123", "password_confirm": "newpassword123"}
    )
    assert resp.status_code == 409
    
    # 5. Revoked token rejection
    with patch("app.services.email.SMTPEmailService.send_invitation") as mock_send:
        client.post(
            f"{settings.API_V1_STR}/admin/invitations",
            json={"email": "revoked_guest@test.com", "role": "editor"},
            headers=admin_headers
        )
        rev_id = db.query(UserInvitation).filter(UserInvitation.email == "revoked_guest@test.com").first().id
        raw_token_revoked = mock_send.call_args[1].get("raw_token") or mock_send.call_args[0][2]
        
    # Revoke it
    client.post(f"{settings.API_V1_STR}/admin/invitations/{rev_id}/revoke", headers=admin_headers)
    
    resp = client.post(
        f"{settings.API_V1_STR}/auth/invitations/accept",
        json={"token": raw_token_revoked, "password": "newpassword123", "password_confirm": "newpassword123"}
    )
    assert resp.status_code == 409
    
    # 6. Reused token rejection
    resp = client.post(
        f"{settings.API_V1_STR}/auth/invitations/accept",
        json={"token": raw_token, "password": "newpassword123", "password_confirm": "newpassword123"}
    )
    assert resp.status_code == 409

def test_mixed_case_duplicate_email_and_pending_constraints(client: TestClient, test_users, auth_headers, db: Session):
    admin_headers = auth_headers("admin@test.com")
    
    # Prove User@Example.com and user@example.com are treated as same identity
    with patch("app.services.email.SMTPEmailService.send_invitation"):
        resp = client.post(
            f"{settings.API_V1_STR}/admin/invitations",
            json={"email": "user@example.com", "role": "viewer"},
            headers=admin_headers
        )
        assert resp.status_code == 201
        
        # Second invitation with mixed case should trigger a resend constraint / pending block
        resp = client.post(
            f"{settings.API_V1_STR}/admin/invitations",
            json={"email": "User@Example.com", "role": "editor"},
            headers=admin_headers
        )
        assert resp.status_code == 400
        assert "pending invitation already exists" in resp.json()["detail"]

def test_resend_invalidates_previous_token(client: TestClient, test_users, auth_headers, db: Session):
    admin_headers = auth_headers("admin@test.com")
    
    with patch("app.services.email.SMTPEmailService.send_invitation") as mock_send:
        client.post(
            f"{settings.API_V1_STR}/admin/invitations",
            json={"email": "resend_test@test.com", "role": "viewer"},
            headers=admin_headers
        )
        raw_token_first = mock_send.call_args[1].get("raw_token") or mock_send.call_args[0][2]
        
    inv = db.query(UserInvitation).filter(UserInvitation.email == "resend_test@test.com").first()
    
    with patch("app.services.email.SMTPEmailService.send_invitation") as mock_send_resend:
        resp = client.post(
            f"{settings.API_V1_STR}/admin/invitations/{inv.id}/resend",
            headers=admin_headers
        )
        assert resp.status_code == 200
        raw_token_second = mock_send_resend.call_args[1].get("raw_token") or mock_send_resend.call_args[0][2]
        
    # Verify first token fails validation / acceptance
    resp = client.post(
        f"{settings.API_V1_STR}/auth/invitations/validate",
        json={"token": raw_token_first}
    )
    assert resp.status_code == 400
    
    # Verify second token is valid
    resp = client.post(
        f"{settings.API_V1_STR}/auth/invitations/validate",
        json={"token": raw_token_second}
    )
    assert resp.status_code == 200

def test_role_change_rules_and_final_admin_lockout(client: TestClient, test_users, auth_headers, db: Session):
    admin_headers = auth_headers("admin@test.com")
    editor_headers = auth_headers("editor@test.com")
    
    # Viewers and Editors cannot change roles
    resp = client.patch(
        f"{settings.API_V1_STR}/admin/users/{test_users['viewer'].id}/role",
        json={"role": "editor"},
        headers=editor_headers
    )
    assert resp.status_code == 403
    
    # Admin can change role
    resp = client.patch(
        f"{settings.API_V1_STR}/admin/users/{test_users['viewer'].id}/role",
        json={"role": "editor"},
        headers=admin_headers
    )
    assert resp.status_code == 200
    
    # Downgrading final admin fails
    resp = client.patch(
        f"{settings.API_V1_STR}/admin/users/{test_users['admin'].id}/role",
        json={"role": "editor"},
        headers=admin_headers
    )
    assert resp.status_code == 400
    assert "Cannot downgrade the final active administrator" in resp.json()["detail"]

def test_user_disabling_and_enabling_active_jwt_lockdown(client: TestClient, test_users, auth_headers, db: Session):
    admin_headers = auth_headers("admin@test.com")
    
    # 1. Login to get a JWT
    resp = client.post(
        f"{settings.API_V1_STR}/auth/token",
        data={"username": "viewer@test.com", "password": "securepassword123"}
    )
    token = resp.json()["access_token"]
    viewer_jwt_headers = {"Authorization": f"Bearer {token}"}
    
    # Verify JWT works
    resp = client.get(f"{settings.API_V1_STR}/auth/me", headers=viewer_jwt_headers)
    assert resp.status_code == 200
    
    # 2. Admin disables user
    client.post(f"{settings.API_V1_STR}/admin/users/{test_users['viewer'].id}/disable", headers=admin_headers)
    
    # Verify JWT now fails instantly
    resp = client.get(f"{settings.API_V1_STR}/auth/me", headers=viewer_jwt_headers)
    assert resp.status_code == 401
    
    # Verify logging in fails
    resp = client.post(
        f"{settings.API_V1_STR}/auth/token",
        data={"username": "viewer@test.com", "password": "securepassword123"}
    )
    assert resp.status_code == 401
    
    # Disabling final admin fails
    resp = client.post(f"{settings.API_V1_STR}/admin/users/{test_users['admin'].id}/disable", headers=admin_headers)
    assert resp.status_code == 400
    assert "Cannot disable the final active administrator" in resp.json()["detail"]

def test_atomic_audit_failure_rolls_back_changes(client: TestClient, test_users, auth_headers, db: Session):
    admin_headers = auth_headers("admin@test.com")
    
    # Mock log_audit_event to fail
    with patch("app.routes.admin_users.log_audit_event", side_effect=Exception("Database Audit Save Failure")):
        resp = client.post(
            f"{settings.API_V1_STR}/admin/users/{test_users['viewer'].id}/disable",
            headers=admin_headers
        )
        assert resp.status_code == 500
        
    # Verify user was NOT disabled due to atomic rollback
    db.expire_all()
    user = db.query(User).filter(User.id == test_users['viewer'].id).first()
    assert user.is_active is True

def test_email_failure_records_delivery_failure(client: TestClient, test_users, auth_headers, db: Session):
    admin_headers = auth_headers("admin@test.com")
    
    with patch("app.services.email.SMTPEmailService.send_invitation", side_effect=Exception("SMTP Relay Timeout")):
        resp = client.post(
            f"{settings.API_V1_STR}/admin/invitations",
            json={"email": "email_fail@test.com", "role": "viewer"},
            headers=admin_headers
        )
        assert resp.status_code == 201
        
        # Verify invitation status remains pending but email delivery fields record the failure
        inv = db.query(UserInvitation).filter(UserInvitation.email == "email_fail@test.com").first()
        assert inv.status == "pending"
        assert inv.email_delivery_status == "failed"
        assert "SMTP Relay Timeout" in inv.email_delivery_error

def test_list_users_endpoints(client: TestClient, test_users, auth_headers):
    admin_headers = auth_headers("admin@test.com")
    editor_headers = auth_headers("editor@test.com")
    
    # 1. Access checks
    resp = client.get(f"{settings.API_V1_STR}/admin/users", headers=editor_headers)
    assert resp.status_code == 403
    
    # 2. Get list without filters
    resp = client.get(f"{settings.API_V1_STR}/admin/users", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 3
    emails = [u["email"] for u in data["users"]]
    assert "admin@test.com" in emails
    
    # 3. Filter by role
    resp = client.get(f"{settings.API_V1_STR}/admin/users?role=admin", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert all(u["role"] == "admin" for u in data["users"])
    
    # 4. Filter by status
    resp = client.get(f"{settings.API_V1_STR}/admin/users?status_filter=active", headers=admin_headers)
    assert resp.status_code == 200
    assert all(u["is_active"] is True for u in resp.json()["users"])
    
    # 5. Search filter
    resp = client.get(f"{settings.API_V1_STR}/admin/users?search=admin", headers=admin_headers)
    assert resp.status_code == 200
    assert len(resp.json()["users"]) >= 1
    assert "admin@test.com" in [u["email"] for u in resp.json()["users"]]

def test_list_invitations_endpoint(client: TestClient, test_users, auth_headers, db: Session):
    admin_headers = auth_headers("admin@test.com")
    editor_headers = auth_headers("editor@test.com")
    
    # 1. Access checks
    resp = client.get(f"{settings.API_V1_STR}/admin/invitations", headers=editor_headers)
    assert resp.status_code == 403
    
    # 2. Create invitation to populate the list
    with patch("app.services.email.SMTPEmailService.send_invitation"):
        client.post(
            f"{settings.API_V1_STR}/admin/invitations",
            json={"email": "list_inv_guest@test.com", "role": "editor"},
            headers=admin_headers
        )
        
    # 3. Get list
    resp = client.get(f"{settings.API_V1_STR}/admin/invitations", headers=admin_headers)
    assert resp.status_code == 200
    invs = resp.json()["invitations"]
    assert len(invs) >= 1
    assert "list_inv_guest@test.com" in [i["email"] for i in invs]

def test_email_service_direct_call(db: Session):
    from app.services.email import SMTPEmailService
    service = SMTPEmailService()
    
    with patch("smtplib.SMTP") as mock_smtp:
        mock_server = MagicMock()
        mock_smtp.return_value = mock_server
        
        # Test success path
        with patch("app.config.settings.ENVIRONMENT", "development"):
            service.send_invitation(
                to_email="test_recipient@test.com",
                role="editor",
                raw_token="token123",
                expires_at=datetime.utcnow() + timedelta(days=1),
                inviter_email="admin@test.com"
            )
            
        mock_smtp.assert_called_once_with(settings.SMTP_HOST, settings.SMTP_PORT, timeout=5)
        mock_server.sendmail.assert_called_once()
        mock_server.quit.assert_called_once()

def test_admin_routes_edge_cases(client: TestClient, test_users, auth_headers):
    admin_headers = auth_headers("admin@test.com")
    fake_id = uuid.uuid4()
    
    # 1. Resend non-existent invitation -> 404
    resp = client.post(f"{settings.API_V1_STR}/admin/invitations/{fake_id}/resend", headers=admin_headers)
    assert resp.status_code == 404
    
    # 2. Revoke non-existent invitation -> 404
    resp = client.post(f"{settings.API_V1_STR}/admin/invitations/{fake_id}/revoke", headers=admin_headers)
    assert resp.status_code == 404
    
    # 3. Disable non-existent user -> 404
    resp = client.post(f"{settings.API_V1_STR}/admin/users/{fake_id}/disable", headers=admin_headers)
    assert resp.status_code == 404
    
    # 4. Enable non-existent user -> 404
    resp = client.post(f"{settings.API_V1_STR}/admin/users/{fake_id}/enable", headers=admin_headers)
    assert resp.status_code == 404
    
    # 5. Role change on non-existent user -> 404
    resp = client.patch(f"{settings.API_V1_STR}/admin/users/{fake_id}/role", json={"role": "editor"}, headers=admin_headers)
    assert resp.status_code == 404
    
    # 6. Disable already disabled user
    viewer_id = test_users["viewer"].id
    resp = client.post(f"{settings.API_V1_STR}/admin/users/{viewer_id}/disable", headers=admin_headers)
    assert resp.status_code == 200
    resp = client.post(f"{settings.API_V1_STR}/admin/users/{viewer_id}/disable", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["message"] == "User is already disabled"
    
    # 7. Enable already active user
    resp = client.post(f"{settings.API_V1_STR}/admin/users/{viewer_id}/enable", headers=admin_headers)
    assert resp.status_code == 200
    resp = client.post(f"{settings.API_V1_STR}/admin/users/{viewer_id}/enable", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["message"] == "User is already active"
