import pytest
from fastapi.testclient import TestClient

def test_login_success(client: TestClient):
    response = client.post(
        "/api/auth/token",
        data={"username": "admin@test.com", "password": "password123"}
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
        data={"username": "admin@test.com", "password": "password123"}
    )
    token = login_resp.json()["access_token"]
    
    response = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json()["email"] == "admin@test.com"
