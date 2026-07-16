from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from app.config import settings
from app.services.email import ResendEmailService, SMTPEmailService, get_email_service


def test_resend_is_preferred_when_api_key_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")
    assert isinstance(get_email_service(), ResendEmailService)


def test_smtp_remains_the_fallback(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", None)
    assert isinstance(get_email_service(), SMTPEmailService)


def test_resend_sends_invitation_over_https(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(settings, "RESEND_FROM", "Beauty PIM <onboarding@resend.dev>")
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://pim.example")
    response = Mock()
    response.raise_for_status.return_value = None

    with patch("app.services.email.requests.post", return_value=response) as post:
        ResendEmailService().send_invitation(
            to_email="invitee@example.com",
            role="editor",
            raw_token="secret-token",
            expires_at=datetime.utcnow() + timedelta(hours=72),
            inviter_email="admin@example.com",
        )

    kwargs = post.call_args.kwargs
    assert kwargs["timeout"] == 10
    assert kwargs["headers"]["Authorization"] == "Bearer re_test"
    assert kwargs["json"]["to"] == ["invitee@example.com"]
    assert "secret-token" in kwargs["json"]["text"]


def test_resend_error_does_not_expose_token(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")
    response = Mock(status_code=403)
    error = __import__("requests").HTTPError(response=response)

    with patch("app.services.email.requests.post", side_effect=error):
        with pytest.raises(RuntimeError) as exc:
            ResendEmailService().send_invitation(
                to_email="invitee@example.com",
                role="viewer",
                raw_token="must-not-leak",
                expires_at=datetime.utcnow() + timedelta(hours=72),
                inviter_email="admin@example.com",
            )

    assert "must-not-leak" not in str(exc.value)
