"""
ماژول متمرکز برای پیکربندی‌های امنیتی Flask:
- Rate Limiting (Flask-Limiter)
- CSRF Protection (Flask-WTF)
- Security Headers (CSP, X-Frame-Options, ...)
"""

import logging
from flask import jsonify, request, current_app
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, CSRFError

from config import settings

log = logging.getLogger("PrinterMonitor")

# ─── Rate Limiter ────────────────────────────────────────────────
# پیش‌فرض: ۲۰۰ درخواست در ساعت برای همه endpointها (سراسری)
# برای endpointهای حساس مثل login/register/forgot-password محدودیت سخت‌گیرانه‌تری اعمال می‌شود
# (با @limiter.limit("...") decorator).
limiter = Limiter(
    key_func=get_remote_address,  # شناسایی کاربر بر اساس IP
    default_limits=["1000 per hour"],
    storage_uri="memory://",  # برای production توصیه می‌شود redis://localhost:6379
    strategy="fixed-window",
    headers_enabled=True,  # اضافه کردن X-RateLimit-* headers به پاسخ
)


# ─── CSRF Protection ─────────────────────────────────────────────
csrf = CSRFProtect()


# ─── Security Headers ────────────────────────────────────────────
def _security_headers(response):
    """اضافه کردن header های امنیتی به همه پاسخ‌ها."""
    # جلوگیری از clickjacking
    response.headers.setdefault("X-Frame-Options", "DENY")
    # جلوگیری از MIME-type sniffing
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # Referrer policy: فقط origin بفرستد (نه full URL)
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # Permissions policy: غیرفعال کردن feature های غیرضروری
    response.headers.setdefault(
        "Permissions-Policy",
        "geolocation=(), microphone=(), camera=(), payment=()",
    )
    # HSTS فقط در production و فقط روی HTTPS
    if settings.ENVIRONMENT == "production" and request.is_secure:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    # Content-Security-Policy:
    # نکته: چون از Google reCAPTCHA و Chart.js استفاده می‌شود، باید CDN ها را whitelist کنیم.
    # 'unsafe-inline' برای style ها در ابتدا لازم است (تا حذف inline styles شود)
    if "Content-Security-Policy" not in response.headers:
        csp_parts = [
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline' https://www.google.com https://www.gstatic.com",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: https:",
            "font-src 'self' data:",
            "connect-src 'self'",
            "frame-src https://www.google.com",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
        ]
        response.headers["Content-Security-Policy"] = "; ".join(csp_parts)
    return response


def init_security(app):
    """راه‌اندازی همه ابزارهای امنیتی روی Flask app."""
    # Rate limiter
    limiter.init_app(app)

    # CSRF protection
    # فعال‌سازی محافظت CSRF فقط روی فرم‌های HTML (POST/PUT/DELETE/PATCH)
    # برای endpointهای JSON API (که با X-CSRFToken header کار می‌کنند) باید جداگانه exempt کرد یا token استفاده کرد
    csrf.init_app(app)

    # تنظیم مدت اعتبار CSRF token (پیش‌فرض ۱۲ ساعت)
    # داشبورد یک صفحه long-lived است (ساعت‌ها باز می‌ماند)؛ با مقدار ۳۶۰۰ ثانیه
    # همه دکمه‌های POST بعد از یک ساعت با 400 می‌مردند. توکن تازه هم از
    # /api/status به‌صورت سایلنت تزریق می‌شود و apiFetch روی انقضا retry می‌کند.
    app.config.setdefault("WTF_CSRF_TIME_LIMIT", 43200)
    app.config.setdefault("WTF_CSRF_SSL_STRICT", settings.ENVIRONMENT == "production")

    # مدیریت خطای CSRF (پاسخ JSON برای API، redirect برای HTML)
    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        log.warning("CSRF error from %s: %s", request.remote_addr, e.description)
        if request.path.startswith("/api/") or request.is_json:
            return jsonify({"error": "csrf_token_invalid", "message": e.description}), 400
        # برای فرم‌های HTML: پیام واضح
        return jsonify({
            "error": "csrf_token_invalid",
            "message": "Session منقضی شده است. لطفاً صفحه را تازه کنید و دوباره تلاش کنید.",
        }), 400

    # Security Headers
    app.after_request(_security_headers)

    # مدیریت خطای rate limit
    @app.errorhandler(429)
    def ratelimit_handler(e):
        retry_after = getattr(e, "retry_after", None)
        log.warning(
            "Rate limit exceeded for %s on %s (retry_after=%s)",
            request.remote_addr, request.path, retry_after,
        )
        return jsonify({
            "error": "rate_limit_exceeded",
            "message": "تعداد درخواست‌های شما بیش از حد مجاز است. لطفاً کمی صبر کنید.",
            "retry_after": retry_after,
        }), 429

    log.info("✅ Security initialized: CSRF, Rate Limiting, Security Headers")
