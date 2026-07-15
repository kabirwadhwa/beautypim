import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Optional
from app.config import settings

logger = logging.getLogger("app.email")

class BaseEmailService:
    def send_invitation(
        self,
        to_email: str,
        role: str,
        raw_token: str,
        expires_at: datetime,
        inviter_email: str
    ) -> None:
        raise NotImplementedError()

class SMTPEmailService(BaseEmailService):
    def send_invitation(
        self,
        to_email: str,
        role: str,
        raw_token: str,
        expires_at: datetime,
        inviter_email: str
    ) -> None:
        # Build the secure accept invitation link
        # The raw token only appears here.
        accept_link = f"{settings.FRONTEND_URL}/accept-invite?token={raw_token}"
        expires_str = expires_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        
        # Write token to temporary file for E2E tests in dev environment
        if settings.ENVIRONMENT != "production":
            try:
                # Write to the root workspace directory
                with open("../test_invitation_token.txt", "w") as f:
                    f.write(raw_token)
            except Exception:
                pass
        
        subject = f"You have been invited to join Beauty PIM"
        body = (
            f"Hello,\n\n"
            f"You have been invited to join Beauty PIM by {inviter_email} with the role of '{role}'.\n\n"
            f"To accept this invitation and set up your password, please click the link below:\n"
            f"{accept_link}\n\n"
            f"This invitation will expire on {expires_str}.\n\n"
            f"If you did not expect this invitation, please ignore this email.\n"
        )
        
        msg = MIMEMultipart()
        msg['From'] = settings.EMAIL_FROM
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        
        try:
            # We connect to SMTP host and port
            # Default is localhost:1025 for Mailpit in dev, or real SMTP relay in prod
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=5)
            if settings.SMTP_TLS:
                server.starttls()
            if settings.SMTP_USER and settings.SMTP_PASSWORD:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.sendmail(settings.EMAIL_FROM, [to_email], msg.as_string())
            server.quit()
            logger.info("Invitation email sent successfully.")
        except Exception as e:
            # IMPORTANT: Do not log raw_token, accept_link or token_hash in case of failure!
            logger.error("Failed to deliver invitation email due to connection failure.")
            # Raise the exception so that the caller can handle/record it
            raise e

# Helper to get active email service
def get_email_service() -> BaseEmailService:
    return SMTPEmailService()
