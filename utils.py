"""Utility helpers for auth, email, and captcha verification."""

from __future__ import annotations

import hashlib
import logging
import smtplib
from email.message import EmailMessage
from typing import Optional

import requests
from itsdangerous import URLSafeTimedSerializer

from config import settings

log = logging.getLogger("PrinterMonitor")


def make_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.SECRET_KEY, salt="password-reset")


def generate_reset_token(user_id: int, email: str) -> str:
    return make_serializer().dumps({"user_id": user_id, "email": email})


def verify_reset_token(token: str, max_age_seconds: int = 3600) -> Optional[dict]:
    try:
        return make_serializer().loads(token, max_age=max_age_seconds)
    except Exception:
        return None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_recaptcha(token: str, remote_ip: str = None) -> bool:
    """
    تأیید توکن reCAPTCHA.

    رفتار:
    - اگر RECAPTCHA_SECRET_KEY در production تنظیم نشده باشد → خطا (راه نمی‌اندازیم
      چون captcha نباید بی‌سروصدا غیرفعال شود).
    - اگر در development تنظیم نشده باشد → True (با هشدار) — برای راحتی توسعه.
    - اگر تنظیم شده باشد → تأیید واقعی با Google API.
    """
    if not settings.RECAPTCHA_SECRET_KEY:
        if settings.ENVIRONMENT == "production":
            # در production هرگز bypass نکنیم؛ این یک حفره امنیتی است.
            log.error(
                "❌ reCAPTCHA verification skipped in PRODUCTION because "
                "RECAPTCHA_SECRET_KEY is not set! Treating as failed."
            )
            return False
        # development: بدون captcha (با هشدار یکباره)
        if not getattr(verify_recaptcha, "_warned", False):
            log.warning(
                "⚠  reCAPTCHA is DISABLED (no RECAPTCHA_SECRET_KEY). "
                "Set the env variable to enable. Acceptable for development only."
            )
            verify_recaptcha._warned = True
        return True

    if not token:
        return False
    try:
        payload = {
            "secret": settings.RECAPTCHA_SECRET_KEY,
            "response": token,
        }
        if remote_ip:
            payload["remoteip"] = remote_ip
        response = requests.post(
            "https://www.google.com/recaptcha/api/siteverify",
            data=payload,
            timeout=10,
        )
        data = response.json()
        success = bool(data.get("success"))
        if not success:
            log.warning("reCAPTCHA verification failed: %s", data.get("error-codes"))
        return success
    except Exception as exc:
        log.exception("reCAPTCHA verification error: %s", exc)
        # در production fail-closed: اگر سرویس Google در دسترس نباشد، کاربر رد می‌شود
        return settings.ENVIRONMENT != "production"


def send_email(subject: str, recipient: str, text_body: str, html_body: str = None) -> bool:
    if not settings.MAIL_SERVER or not recipient:
        log.warning("Email not sent because MAIL_SERVER or recipient is missing")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.MAIL_USERNAME or settings.MAIL_SERVER
    msg["To"] = recipient
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    try:
        if settings.MAIL_USE_TLS:
            with smtplib.SMTP(settings.MAIL_SERVER, settings.MAIL_PORT, timeout=20) as smtp:
                smtp.starttls()
                if settings.MAIL_USERNAME:
                    smtp.login(settings.MAIL_USERNAME, settings.MAIL_PASSWORD)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(settings.MAIL_SERVER, settings.MAIL_PORT, timeout=20) as smtp:
                if settings.MAIL_USERNAME:
                    smtp.login(settings.MAIL_USERNAME, settings.MAIL_PASSWORD)
                smtp.send_message(msg)
        return True
    except Exception as exc:
        log.exception("Failed to send email to %s: %s", recipient, exc)
        return False
