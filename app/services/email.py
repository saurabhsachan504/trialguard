"""Outbound email. The console backend just logs, which is fine for dev."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.config import settings

logger = logging.getLogger("trialguard.email")


def send_email(to: str, subject: str, body: str) -> None:
    if settings.EMAIL_BACKEND == "console" or not settings.SMTP_HOST:
        logger.info("EMAIL to=%s subject=%s\n%s", to, subject, body)
        return

    msg = EmailMessage()
    msg["From"] = settings.EMAIL_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as smtp:
            if settings.SMTP_STARTTLS:
                smtp.starttls()
            if settings.SMTP_USER:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(msg)
    except Exception:  # pragma: no cover - network
        logger.exception("Failed to send email to %s", to)


def send_verification_email(to: str, token: str) -> None:
    link = f"{settings.APP_BASE_URL}{settings.API_PREFIX}/auth/verify-email?token={token}"
    send_email(
        to,
        "Verify your email",
        f"Welcome! Confirm your address to activate your free trials:\n\n{link}\n\n"
        f"This link expires in {settings.EMAIL_VERIFY_TTL_HOURS} hours.",
    )


def send_password_reset_email(to: str, token: str) -> None:
    link = f"{settings.APP_BASE_URL}/reset-password?token={token}"
    send_email(
        to,
        "Reset your password",
        f"Use this link to choose a new password:\n\n{link}\n\n"
        f"It expires in {settings.PASSWORD_RESET_TTL_MINUTES} minutes. "
        "If you did not request this, you can ignore this email.",
    )
