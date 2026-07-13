import pytest
from fastapi.testclient import TestClient
from app.models import User

def test_login_success(client: TestClient):
    response = client.post(
        "/api/auth/token",
        data={"username": "admin@test.com", "password": "securepassword123"}
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["role"] == "admin"

def test_login_failure(client: TestClient):
    response = client.post(
        "/api/auth/token",
        data={"username": "admin@test.com", "password": "wrongpassword"}
    )
    assert response.status_code == 401

def test_get_me(client: TestClient):
    # Log in
    login_resp = client.post(
        "/api/auth/token",
        data={"username": "admin@test.com", "password": "securepassword123"}
    )
    token = login_resp.json()["access_token"]
    
    response = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json()["email"] == "admin@test.com"

def test_admin_bootstrap_and_restricted_registration(client: TestClient, db):
    # Re-enable database lock for test
    from app.config import settings
    
    # Save original settings
    orig_allow = settings.ALLOW_INITIAL_ADMIN_BOOTSTRAP
    orig_token = settings.INITIAL_ADMIN_BOOTSTRAP_TOKEN
    
    try:
        # Clear database users
        db.query(User).delete()
        db.commit()
        
        # Scenario 1: Initial Bootstrap is disabled - registration fails
        settings.ALLOW_INITIAL_ADMIN_BOOTSTRAP = False
        resp = client.post(
            "/api/auth/register",
            json={"email": "first@test.com", "password": "securepassword123"}
        )
        assert resp.status_code == 403
        assert "Initial admin bootstrap is disabled" in resp.json()["detail"]
        
        # Scenario 2: Initial Bootstrap enabled but missing token - registration fails
        settings.ALLOW_INITIAL_ADMIN_BOOTSTRAP = True
        settings.INITIAL_ADMIN_BOOTSTRAP_TOKEN = "secret_bootstrap"
        resp = client.post(
            "/api/auth/register",
            json={"email": "first@test.com", "password": "securepassword123"}
        )
        assert resp.status_code == 401
        
        # Scenario 3: Initial Bootstrap enabled with correct token - registration succeeds as admin
        resp = client.post(
            "/api/auth/register",
            json={"email": "first@test.com", "password": "securepassword123"},
            headers={"X-Bootstrap-Token": "secret_bootstrap"}
        )
        assert resp.status_code == 201
        assert resp.json()["role"] == "admin"
        
        # Scenario 4: Subsequent public registration fails without auth
        resp = client.post(
            "/api/auth/register",
            json={"email": "second@test.com", "password": "securepassword123"}
        )
        assert resp.status_code == 401
        
        # Login to get admin token
        login_resp = client.post(
            "/api/auth/token",
            data={"username": "first@test.com", "password": "securepassword123"}
        )
        admin_token = login_resp.json()["access_token"]
        
        # Scenario 5: Admin creates a viewer user - succeeds
        resp = client.post(
            "/api/auth/register",
            json={"email": "viewer2@test.com", "password": "securepassword123"},
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert resp.status_code == 201
        assert resp.json()["role"] == "viewer"
        
        # Login to get viewer token
        login_resp = client.post(
            "/api/auth/token",
            data={"username": "viewer2@test.com", "password": "securepassword123"}
        )
        viewer_token = login_resp.json()["access_token"]
        
        # Scenario 6: Viewer tries to create another user - fails
        resp = client.post(
            "/api/auth/register",
            json={"email": "third@test.com", "password": "securepassword123"},
            headers={"Authorization": f"Bearer {viewer_token}"}
        )
        assert resp.status_code == 403
        
    finally:
        # Restore original settings
        settings.ALLOW_INITIAL_ADMIN_BOOTSTRAP = orig_allow
        settings.INITIAL_ADMIN_BOOTSTRAP_TOKEN = orig_token
        # Re-seed default users
        from app.auth import get_password_hash
        db.query(User).delete()
        admin = User(
            email="admin@test.com",
            hashed_password=get_password_hash("securepassword123"),
            role="admin"
        )
        viewer = User(
            email="viewer@test.com",
            hashed_password=get_password_hash("securepassword123"),
            role="viewer"
        )
        db.add(admin)
        db.add(viewer)
        db.commit()
