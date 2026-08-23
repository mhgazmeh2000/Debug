"""Authentication blueprint for login, registration, reset, and Google OAuth."""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import wraps
import json
import re

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_login import LoginManager

from config import settings
from models import User
from utils import generate_reset_token, hash_token, send_email, verify_recaptcha, verify_reset_token
import logging
from web.security import limiter
log = logging.getLogger("PrinterMonitor")

from core.security_audit import (
    log_security_event, SecurityEvent, Severity,
    count_recent_failed_logins,
    MAX_FAILED_ATTEMPTS_PER_USER, MAX_FAILED_ATTEMPTS_PER_IP,
    FAILED_ATTEMPT_WINDOW_MINUTES,
)


def _audit_context():
    """خواندن اطلاعات context برای audit log."""
    return {
        "ip_address": request.remote_addr,
        "user_agent": (request.headers.get("User-Agent") or "")[:200],
        "endpoint": request.endpoint,
    }

auth_bp = Blueprint("auth", __name__)
login_manager = LoginManager()
oauth = OAuth()


def _api_unauthorized():
    login_url = url_for("auth.login", next=request.url)
    if request.path.startswith("/api/") or request.is_json or "application/json" in request.headers.get("Accept", ""):
        return jsonify({"error": "unauthorized", "login_url": login_url}), 401
    return redirect(login_url)


def init_auth(app):
    app.config.setdefault("SECRET_KEY", settings.SECRET_KEY)
    app.config.setdefault("MAIL_SERVER", settings.MAIL_SERVER)
    app.config.setdefault("MAIL_PORT", settings.MAIL_PORT)
    app.config.setdefault("MAIL_USE_TLS", settings.MAIL_USE_TLS)
    app.config.setdefault("MAIL_USERNAME", settings.MAIL_USERNAME)
    app.config.setdefault("MAIL_PASSWORD", settings.MAIL_PASSWORD)
    app.config.setdefault("MAIL_USE_OAUTH", settings.MAIL_USE_OAUTH)
    app.config.setdefault("MAIL_OAUTH_CLIENT_ID", settings.MAIL_OAUTH_CLIENT_ID)
    app.config.setdefault("MAIL_OAUTH_CLIENT_SECRET", settings.MAIL_OAUTH_CLIENT_SECRET)
    app.config.setdefault("MAIL_OAUTH_REFRESH_TOKEN", settings.MAIL_OAUTH_REFRESH_TOKEN)
    app.config.setdefault("GOOGLE_CLIENT_ID", settings.GOOGLE_CLIENT_ID)
    app.config.setdefault("GOOGLE_CLIENT_SECRET", settings.GOOGLE_CLIENT_SECRET)
    app.config.setdefault("RECAPTCHA_SITE_KEY", settings.RECAPTCHA_SITE_KEY)
    app.config.setdefault("RECAPTCHA_SECRET_KEY", settings.RECAPTCHA_SECRET_KEY)

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "برای ادامه وارد شوید"
    login_manager.login_message_category = "warning"

    @login_manager.unauthorized_handler
    def unauthorized():
        return _api_unauthorized()

    oauth.init_app(app)
    if app.config.get("GOOGLE_CLIENT_ID") and app.config.get("GOOGLE_CLIENT_SECRET"):
        oauth.register(
            name="google",
            client_id=app.config["GOOGLE_CLIENT_ID"],
            client_secret=app.config["GOOGLE_CLIENT_SECRET"],
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )


@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)


def _is_safe_next_url(target: str) -> bool:
    """
    بررسی امن بودن URL مقصد برای جلوگیری از Open Redirect attack.
    فقط مسیرهای نسبی داخلی مجاز هستند (مثل /dashboard، /printers).
    مسیرهای زیر رد می‌شوند:
      - URL مطلق (http://...، https://...)
      - URLهای protocol-relative (//evil.com)
      - URLهای با scheme (javascript:، data:)
      - مقادیر خالی یا None
    """
    if not target:
        return False
    from urllib.parse import urlparse
    parsed = urlparse(target)
    # نه scheme داشته باشد، نه netloc؛ و باید با '/' شروع شود اما نه '//'
    return (
        not parsed.scheme
        and not parsed.netloc
        and target.startswith("/")
        and not target.startswith("//")
    )


def _next_url(default_endpoint: str = "dashboard.index") -> str:
    """دریافت امن URL بعد از login/register.
    اگر پارامتر `next` معتبر بود از آن استفاده می‌کند، در غیر این صورت
    به endpoint پیش‌فرض هدایت می‌کند.
    """
    target = request.values.get("next", "")
    if _is_safe_next_url(target):
        return target
    return url_for(default_endpoint)


def has_role(user, *roles):
    return bool(user and getattr(user, "role", None) in roles)


def _clean_list(value, *, lower: bool = True):
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return [value.strip().lower() if lower else value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    items = []
    for item in value:
        text = str(item).strip()
        if lower:
            text = text.lower()
        if text and text not in items:
            items.append(text)
    return items


def office_for_ip(ip: str) -> str:
    ip = (ip or "").strip()
    if not ip:
        return "other"
    for office_id, subnet in settings.OFFICE_SUBNETS.items():
        if subnet and ip.startswith(f"{subnet}."):
            return office_id
    return "other"


def user_allowed_offices(user):
    return _clean_list(getattr(user, "allowed_offices", []), lower=True)


def user_allowed_modules(user):
    return _clean_list(getattr(user, "allowed_modules", []), lower=True)


def user_can_access_module(user, module_name: str) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "role", None) == "admin":
        return True
    module_name = str(module_name or "").strip().lower()
    allowed = user_allowed_modules(user)
    return not allowed or module_name in allowed


def user_can_access_office(user, ip: str) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "role", None) == "admin":
        return True
    allowed = user_allowed_offices(user)
    if not allowed:
        return True
    from core import store
    with store.printers_lock:
        configured = next((p for p in store.PRINTERS if p.get("ip") == ip), None)
    if configured and str(configured.get("group") or "").strip().lower() in allowed:
        return True
    office_id = office_for_ip(ip)
    return office_id in allowed


def allowed_printer_ips(user):
    if not user or not getattr(user, "is_authenticated", False):
        return []
    if getattr(user, "role", None) == "admin":
        return []
    allowed = set(user_allowed_offices(user))
    if not allowed:
        return []
    from core import store

    with store.printers_lock:
        printers = list(store.PRINTERS)
    allowed_ips = []
    for printer in printers:
        printer_ip = (printer.get("ip") or "").strip()
        if not printer_ip:
            continue
        group_id = str(printer.get("group") or "").strip().lower()
        office_id = office_for_ip(printer_ip)
        if group_id in allowed or office_id in allowed:
            allowed_ips.append(printer_ip)
    return allowed_ips


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapper(*args, **kwargs):
            if not has_role(current_user, *roles):
                abort(403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def admin_required(view):
    return role_required("admin")(view)


def users_access_required(view):
    """Allow admins and users-management managers to use the users module."""
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if getattr(current_user, "role", None) == "admin":
            return view(*args, **kwargs)
        if getattr(current_user, "role", None) == "manager" and user_can_access_module(current_user, "users"):
            return view(*args, **kwargs)
        abort(403)
    return wrapper


def _unique_username(base_name: str) -> str:
    cleaned = "".join(ch for ch in base_name.lower() if ch.isalnum()) or "user"
    candidate = cleaned
    suffix = 1
    while User.find_by_identifier(candidate):
        candidate = f"{cleaned}{suffix}"
        suffix += 1
    return candidate


USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 30 per 5 minutes", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(_next_url())

    error = None
    if request.method == "POST":
        ctx = _audit_context()
        identifier = (request.form.get("identifier") or "").strip()

        # بررسی brute-force بر اساس IP/کاربر
        ip_attempts = count_recent_failed_logins(
            ip_address=ctx["ip_address"], minutes=FAILED_ATTEMPT_WINDOW_MINUTES,
        )
        user_attempts = count_recent_failed_logins(
            user_identifier=identifier, minutes=FAILED_ATTEMPT_WINDOW_MINUTES,
        ) if identifier else 0

        if ip_attempts >= MAX_FAILED_ATTEMPTS_PER_IP or user_attempts >= MAX_FAILED_ATTEMPTS_PER_USER:
            log_security_event(
                SecurityEvent.ACCOUNT_LOCKED, severity=Severity.CRITICAL,
                user_identifier=identifier, success=False,
                details=f"Too many failed attempts: ip={ip_attempts}, user={user_attempts}",
                **ctx,
            )
            error = (
                "تعداد تلاش‌های ناموفق از حد مجاز گذشته است. "
                f"لطفاً پس از {FAILED_ATTEMPT_WINDOW_MINUTES} دقیقه دوباره تلاش کنید."
            )
        elif not verify_recaptcha(request.form.get("g-recaptcha-response", ""), ctx["ip_address"]):
            log_security_event(
                SecurityEvent.SUSPICIOUS_ACTIVITY, severity=Severity.WARNING,
                user_identifier=identifier, success=False,
                details="reCAPTCHA verification failed", **ctx,
            )
            error = "کپچا معتبر نیست"
        else:
            password = request.form.get("password") or ""
            user = User.find_by_identifier(identifier)
            if user and user.is_active and user.verify_password(password):
                login_user(user, remember=bool(request.form.get("remember")))
                user.touch_login()
                log_security_event(
                    SecurityEvent.SUCCESSFUL_LOGIN, severity=Severity.INFO,
                    user_identifier=user.username, user_id=user.id,
                    success=True, **ctx,
                )
                flash("با موفقیت وارد شدید", "success")
                return redirect(_next_url())
            # تلاش ناموفق
            log_security_event(
                SecurityEvent.FAILED_LOGIN, severity=Severity.WARNING,
                user_identifier=identifier,
                user_id=user.id if user else None,
                success=False,
                details=(
                    "User not found" if not user
                    else "Inactive user" if not user.is_active
                    else "Invalid password"
                ),
                **ctx,
            )
            error = "نام کاربری/ایمیل یا رمز عبور اشتباه است"

    return render_template(
        "login.html",
        error=error,
        google_enabled=bool(current_app.config.get("GOOGLE_CLIENT_ID") and current_app.config.get("GOOGLE_CLIENT_SECRET")),
        recaptcha_site_key=current_app.config.get("RECAPTCHA_SITE_KEY", ""),
        load_dashboard_scripts=False,
    )


@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute; 10 per hour", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(_next_url())

    error = None
    if request.method == "POST":
        if not verify_recaptcha(request.form.get("g-recaptcha-response", ""), request.remote_addr):
            error = "کپچا معتبر نیست"
        else:
            username = (request.form.get("username") or "").strip()
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""
            confirm = request.form.get("confirm_password") or ""

            if not username or not email or not password:
                error = "همه فیلدها الزامی هستند"
            elif not USERNAME_RE.fullmatch(username):
                error = "نام کاربری فقط باید با حروف انگلیسی، عدد، نقطه، خط تیره یا زیرخط باشد"
            elif password != confirm:
                error = "رمز عبور و تکرار آن یکسان نیست"
            elif User.find_by_identifier(username):
                error = "نام کاربری قبلاً استفاده شده است"
            elif User.find_by_email(email):
                error = "ایمیل قبلاً ثبت شده است"
            else:
                # ✅ خودکارسازی: اولین کاربر = ادمین تأیید‌شده
                is_first_user = User.total_count() == 0
                user_role = "admin" if is_first_user else "viewer"
                user_verified = is_first_user
                user = User.create(username=username, email=email, password=password, role=user_role, is_verified=user_verified)
                if user:
                    login_user(user)
                    user.touch_login()
                    log_security_event(
                        SecurityEvent.ACCOUNT_CREATED, severity=Severity.INFO,
                        user_identifier=user.username, user_id=user.id,
                        success=True, **_audit_context(),
                    )
                    if user.is_verified:
                        flash("ثبت‌نام انجام شد و حساب شما فعال است.", "success")
                    else:
                        flash("ثبت‌نام انجام شد. حساب شما در انتظار تأیید است.", "warning")
                    return redirect(_next_url())
                error = "امکان ایجاد حساب وجود نداشت"

    return render_template(
        "register.html",
        error=error,
        recaptcha_site_key=current_app.config.get("RECAPTCHA_SITE_KEY", ""),
        load_dashboard_scripts=False,
    )


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("3 per minute; 5 per hour", methods=["POST"])
def forgot_password():
    message = None
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        user = User.find_by_email(email)
        ctx = _audit_context()
        # همیشه log می‌کنیم (موفق یا نه) — برای detection حملات email enumeration
        log_security_event(
            SecurityEvent.PASSWORD_RESET_REQUESTED,
            severity=Severity.INFO,
            user_identifier=email,
            user_id=user.id if user else None,
            success=bool(user),
            details=("Email matched" if user else "No matching email"),
            **ctx,
        )
        if user:
            token = generate_reset_token(user.id, user.email)
            token_hash = hash_token(token)
            expires_at = (datetime.now() + timedelta(hours=1)).isoformat()
            user.set_reset_token(token_hash, expires_at)
            reset_link = url_for("auth.reset_password", token=token, _external=True)
            text_body = (
                f"برای بازنشانی رمز عبور روی لینک زیر کلیک کنید:\n{reset_link}\n\n"
                "این لینک فقط یکبار و تا ۱ ساعت معتبر است."
            )
            html_body = f"<p>برای بازنشانی رمز عبور روی لینک زیر کلیک کنید:</p><p><a href='{reset_link}'>{reset_link}</a></p><p>این لینک فقط یکبار و تا ۱ ساعت معتبر است.</p>"
            email_sent = send_email("بازنشانی رمز عبور", user.email, text_body, html_body)
            if email_sent:
                message = "لینک بازنشانی رمز عبور با موفقیت به ایمیل شما ارسال شد."
            else:
                error = "ارسال ایمیل با خطا مواجه شد. لطفاً بعدا دوباره تلاشش کنید."
        else:
            error = "ایمیل وارد شده در سیستم موجود نیست."

    return render_template(
        "forgot_password.html",
        message=message,
        error=error,
        load_dashboard_scripts=False,
    )


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def reset_password(token):
    payload = verify_reset_token(token)
    token_hash = hash_token(token)
    user = User.find_by_reset_token_hash(token_hash)
    error = None

    if not payload or not user:
        error = "لینک بازنشانی نامعتبر یا منقضی شده است"
    else:
        expires_at = user.reset_token_expires
        if expires_at:
            try:
                if datetime.fromisoformat(expires_at) < datetime.now():
                    error = "لینک بازنشانی منقضی شده است"
            except Exception:
                error = "لینک بازنشانی نامعتبر است"

    if request.method == "POST" and not error:
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""
        if not password:
            error = "رمز عبور الزامی است"
        elif password != confirm:
            error = "رمز عبور و تکرار آن یکسان نیست"
        else:
            if user.set_password(password):
                user.clear_reset_token()
                log_security_event(
                    SecurityEvent.PASSWORD_RESET_COMPLETED, severity=Severity.WARNING,
                    user_identifier=user.username, user_id=user.id,
                    success=True, **_audit_context(),
                )
                flash("رمز عبور با موفقیت تغییر کرد", "success")
                return redirect(url_for("auth.login"))
            error = "امکان تغییر رمز وجود نداشت"

    return render_template(
        "reset_password.html",
        token=token,
        error=error,
        load_dashboard_scripts=False,
    )


@auth_bp.route("/auth/google")
def google_login():
    if not hasattr(oauth, "google"):
        flash("ورود با گوگل فعال نشده است", "error")
        return redirect(url_for("auth.login"))
    # nonce برای جلوگیری از replay attack (OIDC requirement)
    import secrets
    from flask import session
    nonce = secrets.token_urlsafe(32)
    session['oauth_nonce'] = nonce
    redirect_uri = settings.GOOGLE_REDIRECT_URI or url_for("auth.google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri, nonce=nonce)


@auth_bp.route("/auth/google/callback")
def google_callback():
    if not hasattr(oauth, "google"):
        flash("ورود با گوگل فعال نشده است", "error")
        return redirect(url_for("auth.login"))
    ctx = _audit_context()
    try:
        # توکن را مستقیماً از Google دریافت کن (بدون JWK verification)
        import requests as _requests
        token_resp = _requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": request.args.get("code", ""),
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI or url_for("auth.google_callback", _external=True),
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise ValueError("No access_token in token response")

        # اطلاعات کاربر را از userinfo endpoint بگیر (بدون نیاز به JWK)
        userinfo_resp = _requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        userinfo_resp.raise_for_status()
        profile = userinfo_resp.json()
        if not profile.get("email"):
            raise ValueError("No email in Google userinfo response")
    except Exception as e:
        log.exception("OAuth callback error: %s", e)
        log_security_event(
            SecurityEvent.OAUTH_FAILURE, severity=Severity.WARNING,
            success=False, details=str(e), **ctx,
        )
        flash("خطا در ورود با گوگل: " + str(e), "error")
        return redirect(url_for("auth.login"))

    email = (profile.get("email") or "").strip().lower()
    google_id = str(profile.get("sub") or profile.get("id") or "").strip()
    name = (profile.get("name") or profile.get("given_name") or email.split("@")[0] or "user").strip()

    if not email:
        log_security_event(
            SecurityEvent.OAUTH_FAILURE, severity=Severity.WARNING,
            success=False, details="No email in Google profile", **ctx,
        )
        flash("اطلاعات ایمیل از گوگل دریافت نشد", "error")
        return redirect(url_for("auth.login"))

    user = User.find_by_google_id(google_id) if google_id else None
    if not user:
        user = User.find_by_email(email)
    if not user:
        user = User.create(username=_unique_username(name), email=email, google_id=google_id, is_verified=False)
    elif google_id and not user.google_id:
        user.set_google_id(google_id)

    if user:
        login_user(user)
        user.touch_login()
        log_security_event(
            SecurityEvent.OAUTH_LOGIN, severity=Severity.INFO,
            user_identifier=user.username, user_id=user.id,
            success=True, details="Google OAuth", **ctx,
        )
        if user.is_verified:
            flash("با حساب گوگل وارد شدید", "success")
        else:
            flash("حساب شما با گوگل وارد شد اما هنوز در انتظار تأیید ادمین است", "warning")
        return redirect(_next_url())

    flash("ورود گوگلی انجام نشد", "error")
    return redirect(url_for("auth.login"))




@auth_bp.route("/logout")
@login_required
def logout():
    user_id = current_user.id
    username = current_user.username
    logout_user()
    log_security_event(
        SecurityEvent.LOGOUT, severity=Severity.INFO,
        user_identifier=username, user_id=user_id, success=True,
        **_audit_context(),
    )
    flash("خروج با موفقیت انجام شد", "success")
    return redirect(url_for("auth.login"))
