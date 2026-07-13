import pytest
from fastapi.testclient import TestClient
from app.config import settings
from app.limiter import limiter

def test_rate_limiting_login(client: TestClient):
    # Reset limiter history for test isolation
    limiter.history.clear()
    # Set limit very low for testing
    orig_limit = settings.RATE_LIMIT_LOGIN
    settings.RATE_LIMIT_LOGIN = "2/minute"
    
    try:
        # Request 1: OK
        resp = client.post("/api/auth/token", data={"username": "admin@test.com", "password": "securepassword123"})
        assert resp.status_code == 200
        
        # Request 2: OK
        resp = client.post("/api/auth/token", data={"username": "admin@test.com", "password": "securepassword123"})
        assert resp.status_code == 200
        
        # Request 3: Exceeded!
        resp = client.post("/api/auth/token", data={"username": "admin@test.com", "password": "securepassword123"})
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        assert "Rate limit exceeded" in resp.json()["detail"]
        
    finally:
        settings.RATE_LIMIT_LOGIN = orig_limit
