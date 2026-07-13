import pytest
from app.services.webhooks import is_safe_url, dispatch_webhook_safe
from app.config import settings

def test_ssrf_url_validation():
    # Save original settings
    orig_env = settings.ENVIRONMENT
    orig_domains = settings.WEBHOOK_ALLOWED_DOMAINS
    
    try:
        # 1. Dev mode: HTTP is allowed, but private IPs are blocked
        settings.ENVIRONMENT = "development"
        settings.WEBHOOK_ALLOWED_DOMAINS = None
        
        is_safe, reason = is_safe_url("http://example.com/webhook")
        assert is_safe is True
        
        is_safe, reason = is_safe_url("http://127.0.0.1/webhook")
        assert is_safe is False
        assert "Disallowed IP address" in reason
        
        is_safe, reason = is_safe_url("http://localhost/webhook")
        assert is_safe is False
        
        is_safe, reason = is_safe_url("http://169.254.169.254/metadata")
        assert is_safe is False
        
        # 2. Prod mode: HTTP is blocked, HTTPS is required
        settings.ENVIRONMENT = "production"
        is_safe, reason = is_safe_url("http://example.com/webhook")
        assert is_safe is False
        assert "HTTPS is required in production" in reason
        
        is_safe, reason = is_safe_url("https://example.com/webhook")
        assert is_safe is True
        
        # 3. Domain allowlist check
        settings.WEBHOOK_ALLOWED_DOMAINS = "ceartas.io,google.com"
        is_safe, reason = is_safe_url("https://google.com/webhook")
        assert is_safe is True
        
        is_safe, reason = is_safe_url("https://www.google.com/webhook")
        assert is_safe is True
        
        is_safe, reason = is_safe_url("https://attacker.com/webhook")
        assert is_safe is False
        assert "not in the allowlist" in reason

    finally:
        settings.ENVIRONMENT = orig_env
        settings.WEBHOOK_ALLOWED_DOMAINS = orig_domains

def test_dispatch_webhook_invalid_ssrf():
    with pytest.raises(ValueError):
        dispatch_webhook_safe("http://127.0.0.1/webhook", {})

def test_webhook_edge_cases():
    # Unsupported schemes
    is_safe, reason = is_safe_url("ftp://example.com")
    assert is_safe is False
    assert "Unsupported scheme" in reason

    # Missing hostname
    is_safe, reason = is_safe_url("https:///path")
    assert is_safe is False
    
    # Invalid URL formatting
    is_safe, reason = is_safe_url("http://[invalid-ip]/")
    assert is_safe is False

    # is_safe_ip invalid format
    from app.services.webhooks import is_safe_ip
    assert is_safe_ip("999.999.999.999") is False
    assert is_safe_ip("not-an-ip") is False

