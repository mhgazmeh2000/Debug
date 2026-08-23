import os
import logging
import secrets
import re
import json
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, render_template, request, url_for
from flask_login import current_user
from config import settings
from core.groups import load_groups

from models import User
from web.auth import users_access_required
from utils import generate_reset_token, hash_token, send_email
from core.security_audit import log_security_event, SecurityEvent, Severity

bp = Blueprint("users", __name__)
log = logging.getLogger("PrinterMonitor")

def _allowed_offices() -> set:
    office_ids = set((getattr(settings, "OFFICE_SUBNETS", {}) or {}).keys())
    office_ids.update(group["id"] for group in load_groups() if group.get("id"))
    return office_ids


def _office_options() -> list:
    name_map = {
        "imamat": "دفتر امامت",
        "soroush": "دفتر سروش",
        "falestin": "دفتر فلسطین",
        "elahiye": "دفتر الهیه",
        "other": "سایر",
    }
    options = [
        {"id": office_id, "name": name_map.get(office_id, office_id), "type": "office"}
        for office_id in (getattr(settings, "OFFICE_SUBNETS", {}) or {})
    ]
    options.extend(
        {"id": group["id"], "name": group["name"], "type": "custom"}
        for group in load_groups()
        if group.get("id") and group.get("name")
    )
    return options
ALLOWED_MODULES = {"printers", "logs", "excel", "users"}


def _audit_user_action(event_type, target, details, success=True):
    log_security_event(
        event_type,
        severity=Severity.INFO if success else Severity.WARNING,
        user_identifier=getattr(current_user, "username", None),
        user_id=getattr(current_user, "id", None),
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent"),
        endpoint=request.endpoint,
        success=success,
        details=json.dumps({
            "target_user_id": getattr(target, "id", None),
            "target_username": getattr(target, "username", None),
            **details,
        }, ensure_ascii=False),
    )

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


def _validate_username(username: str):
    if not username:
        return "username الزامی است"
    if not USERNAME_RE.fullmatch(username):
        return "نام کاربری فقط باید با حروف انگلیسی، عدد، نقطه، خط تیره یا زیرخط باشد"
    return None


def _normalize_selection(values, allowed_values):
    normalized = []
    for value in values or []:
        text = str(value).strip().lower()
        if text in allowed_values and text not in normalized:
            normalized.append(text)
    return normalized


def _serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "is_verified": bool(user.is_verified),
        "is_active": bool(user.is_active),
        "allowed_offices": list(getattr(user, "allowed_offices", []) or []),
        "allowed_modules": list(getattr(user, "allowed_modules", []) or []),
        "last_login_at": user.last_login_at,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


@bp.route("/users")
@users_access_required
def users_page():
    return render_template("users.html", load_dashboard_scripts=False, office_options=_office_options())


@bp.route("/api/users/office-options", methods=["GET"])
@users_access_required
def api_office_options():
    return jsonify({"offices": _office_options()})


@bp.route("/api/users", methods=["GET"])
@users_access_required
def api_users():
    users = [u for u in User.all() if u]
    return jsonify({"users": [_serialize_user(u) for u in users], "total": len(users)})


@bp.route("/api/users", methods=["POST"])
@users_access_required
def api_user_add():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip().lower()
    role = (data.get("role") or "viewer").strip().lower()
    allowed_offices = data.get("allowed_offices") or []
    allowed_modules = data.get("allowed_modules") or []
    allowed_offices = _normalize_selection(allowed_offices, _allowed_offices())
    allowed_modules = _normalize_selection(allowed_modules, ALLOWED_MODULES)
    if role == "admin":
        allowed_offices = []
        allowed_modules = []

    username_error = _validate_username(username)
    if username_error:
        return jsonify({"error": username_error}), 400
    if not email:
        return jsonify({"error": "email الزامی است"}), 400
    if role not in ("admin", "manager", "viewer"):
        return jsonify({"error": "role نامعتبر است"}), 400
    if User.find_by_identifier(username):
        return jsonify({"error": "نام کاربری تکراری است"}), 400
    if User.find_by_email(email):
        return jsonify({"error": "ایمیل تکراری است"}), 400

    temp_password = secrets.token_urlsafe(12)
    user = User.create(
        username=username,
        email=email,
        password=temp_password,
        role=role,
        is_verified=True,
        allowed_offices=allowed_offices,
        allowed_modules=allowed_modules,
    )
    if not user:
        return jsonify({"error": "unable to create user"}), 500

    token = generate_reset_token(user.id, user.email)
    token_hash = hash_token(token)
    expires_at = (datetime.now() + timedelta(hours=1)).isoformat()
    user.set_reset_token(token_hash, expires_at)
    reset_link = url_for("auth.reset_password", token=token, _external=True)
    text_body = (
        f"یک حساب کاربری برای شما ایجاد شده است.\n"
        f"نام کاربری: {username}\n"
        f"برای تنظیم رمز عبور روی لینک زیر کلیک کنید:\n{reset_link}\n\n"
        "این لینک فقط یک‌بار و تا ۱ ساعت معتبر است."
    )
    html_body = (
        f"<p>یک حساب کاربری برای شما ایجاد شده است.</p>"
        f"<p>نام کاربری: <strong>{username}</strong></p>"
        f"<p><a href='{reset_link}'>تنظیم رمز عبور</a></p>"
        f"<p>این لینک فقط یک‌بار و تا ۱ ساعت معتبر است.</p>"
    )
    email_sent = send_email("تنظیم رمز عبور اولیه", user.email, text_body, html_body)
    _audit_user_action(SecurityEvent.USER_CREATED, user, {
        "role": role,
        "allowed_offices": allowed_offices,
        "allowed_modules": allowed_modules,
        "email_sent": email_sent,
    })
    payload = _serialize_user(user)
    payload["email_sent"] = email_sent
    return jsonify({"status": "ok", "user": payload})


@bp.route("/api/users/<int:user_id>/reset-password", methods=["POST"])
@users_access_required
def api_user_reset_password(user_id):
    """ارسال ایمیل بازنشانی رمز عبور برای یک کاربر مشخص."""
    target = User.get(user_id)
    if not target:
        return jsonify({"error": "user not found"}), 404

    token = generate_reset_token(target.id, target.email)
    token_hash = hash_token(token)
    expires_at = (datetime.now() + timedelta(hours=1)).isoformat()
    target.set_reset_token(token_hash, expires_at)
    reset_link = url_for("auth.reset_password", token=token, _external=True)
    text_body = (
        f"برای بازنشانی رمز عبور روی لینک زیر کلیک کنید:\n{reset_link}\n\n"
        "این لینک فقط یک‌بار و تا ۱ ساعت معتبر است."
    )
    html_body = (
        f"<p>برای بازنشانی رمز عبور روی لینک زیر کلیک کنید:</p>"
        f"<p><a href='{reset_link}'>{reset_link}</a></p>"
        f"<p>این لینک فقط یک‌بار و تا ۱ ساعت معتبر است.</p>"
    )
    email_sent = send_email("بازنشانی رمز عبور", target.email, text_body, html_body)
    _audit_user_action(SecurityEvent.PASSWORD_RESET_REQUESTED, target, {
        "email_sent": email_sent,
    })
    return jsonify({"status": "ok", "email_sent": email_sent})


@bp.route("/api/users/<int:user_id>/role", methods=["POST"])
@users_access_required
def api_user_role(user_id):
    data = request.get_json() or {}
    role = (data.get("role") or "").strip().lower()
    if role not in ("admin", "manager", "viewer"):
        return jsonify({"error": "role نامعتبر است"}), 400

    target = User.get(user_id)
    if not target:
        return jsonify({"error": "user not found"}), 404
    if int(target.id) == int(current_user.id):
        return jsonify({"error": "نمی‌توانید نقش کاربری خودتان را تغییر دهید"}), 403

    old_role = target.role
    if target.role == "admin" and role != "admin":
        admins = [u for u in User.all() if u and u.role == "admin"]
        if len(admins) <= 1:
            return jsonify({"error": "حداقل یک admin باید باقی بماند"}), 400

    if target.set_role(role):
        if role == "admin":
            target.set_verified(True)
            target.set_access(allowed_offices=[], allowed_modules=[])
        _audit_user_action(SecurityEvent.USER_ROLE_CHANGED, target, {"old_role": old_role, "new_role": role})
        return jsonify({"status": "ok", "user": _serialize_user(target)})
    return jsonify({"error": "unable to update role"}), 500


@bp.route("/api/users/<int:user_id>/verify", methods=["POST"])
@users_access_required
def api_user_verify(user_id):
    data = request.get_json(silent=True) or {}
    target = User.get(user_id)
    if not target:
        return jsonify({"error": "user not found"}), 404

    verified = data.get("is_verified")
    if verified is None:
        verified = not target.is_verified
    verified = bool(verified)

    if target.role == "admin" and not verified:
        admins = [u for u in User.all() if u and u.role == "admin"]
        if len(admins) <= 1:
            return jsonify({"error": "حداقل یک admin باید تأیید شده بماند"}), 400

    if target.set_verified(verified):
        _audit_user_action(SecurityEvent.USER_VERIFICATION_CHANGED, target, {"is_verified": verified})
        return jsonify({"status": "ok", "user": _serialize_user(target)})
    return jsonify({"error": "unable to update verification"}), 500


@bp.route("/api/users/<int:user_id>/access", methods=["POST"])
@users_access_required
def api_user_access(user_id):
    data = request.get_json(silent=True) or {}
    target = User.get(user_id)
    if not target:
        return jsonify({"error": "user not found"}), 404
    if int(target.id) == int(current_user.id):
        return jsonify({"error": "نمی‌توانید دسترسی کاربری خودتان را تغییر دهید"}), 403

    allowed_offices = data.get("allowed_offices") or []
    allowed_modules = data.get("allowed_modules") or []
    if target.role == "admin":
        target.set_access(allowed_offices=[], allowed_modules=[])
        _audit_user_action(SecurityEvent.USER_ACCESS_CHANGED, target, {"full_access": True})
        return jsonify({"status": "ok", "user": _serialize_user(target), "full_access": True})
    allowed_offices = _normalize_selection(allowed_offices, _allowed_offices())
    allowed_modules = _normalize_selection(allowed_modules, ALLOWED_MODULES)
    if target.set_access(allowed_offices=allowed_offices, allowed_modules=allowed_modules):
        _audit_user_action(SecurityEvent.USER_ACCESS_CHANGED, target, {
            "allowed_offices": allowed_offices,
            "allowed_modules": allowed_modules,
        })
        return jsonify({"status": "ok", "user": _serialize_user(target)})
    return jsonify({"error": "unable to update access"}), 500


@bp.route("/api/users/<int:user_id>", methods=["DELETE"])
@users_access_required
def api_delete_user(user_id):
    target = User.get(user_id)
    if not target:
        return jsonify({"error": "user not found"}), 404

    if int(target.id) == int(current_user.id):
        return jsonify({"error": "cannot delete current user"}), 400

    if target.role == "admin":
        admins = [u for u in User.all() if u and u.role == "admin"]
        if len(admins) <= 1:
            return jsonify({"error": "حداقل یک admin باید باقی بماند"}), 400

    if target.delete():
        _audit_user_action(SecurityEvent.USER_DELETED, target, {"deleted_user_id": user_id})
        return jsonify({"status": "deleted", "user_id": user_id})
    return jsonify({"error": "unable to delete user"}), 500


@bp.route("/api/users/<int:user_id>/sensor-alert", methods=["GET"])
@users_access_required
def api_user_sensor_alert_get(user_id):
    """دریافت تنظیمات ایمیل هشدار سنسور برای یک کاربر."""
    target = User.get(user_id)
    if not target:
        return jsonify({"error": "user not found"}), 404
    return jsonify({
        "sensor_alert_enabled": target.sensor_alert_enabled,
        "sensor_temp_warning": target.sensor_temp_warning,
        "sensor_temp_critical": target.sensor_temp_critical,
        "sensor_hum_warning_high": target.sensor_hum_warning_high,
        "sensor_hum_critical_high": target.sensor_hum_critical_high,
        "sensor_hum_warning_low": target.sensor_hum_warning_low,
        "sensor_alert_cooldown_minutes": target.sensor_alert_cooldown_minutes,
        "sensor_alert_start_hour": target.sensor_alert_start_hour,
        "sensor_alert_end_hour": target.sensor_alert_end_hour,
    })


@bp.route("/api/users/<int:user_id>/sensor-alert", methods=["POST"])
@users_access_required
def api_user_sensor_alert_set(user_id):
    """ذخیره تنظیمات ایمیل هشدار سنسور برای یک کاربر."""
    target = User.get(user_id)
    if not target:
        return jsonify({"error": "user not found"}), 404

    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("sensor_alert_enabled", False))
    temp_warning = float(data.get("sensor_temp_warning", 30))
    temp_critical = float(data.get("sensor_temp_critical", 35))
    hum_warning_high = float(data.get("sensor_hum_warning_high", 70))
    hum_critical_high = float(data.get("sensor_hum_critical_high", 80))
    hum_warning_low = float(data.get("sensor_hum_warning_low", 20))
    cooldown_minutes = int(data.get("sensor_alert_cooldown_minutes", 30))
    start_hour = int(data.get("sensor_alert_start_hour", 0))
    end_hour = int(data.get("sensor_alert_end_hour", 24))

    # Validate ranges
    if not (0 <= start_hour <= 24 and 0 <= end_hour <= 24):
        return jsonify({"error": "ساعت شروع/پایان باید بین 0 تا 24 باشد"}), 400
    if cooldown_minutes < 0:
        return jsonify({"error": "فاصله ارسال نمی‌تواند منفی باشد"}), 400

    if target.set_sensor_alert(
        enabled=enabled,
        temp_warning=temp_warning,
        temp_critical=temp_critical,
        hum_warning_high=hum_warning_high,
        hum_critical_high=hum_critical_high,
        hum_warning_low=hum_warning_low,
        cooldown_minutes=cooldown_minutes,
        start_hour=start_hour,
        end_hour=end_hour,
    ):
        _audit_user_action(SecurityEvent.USER_ACCESS_CHANGED, target, {
            "sensor_alert_enabled": enabled,
        })
        return jsonify({"status": "ok", "user": _serialize_user(target)})
    return jsonify({"error": "unable to update sensor alert settings"}), 500

@bp.route("/api/sensor-alert", methods=["GET"])
@users_access_required
def api_sensor_alert_get():
    from core.sensor_alert import load_config
    return jsonify(load_config())


@bp.route("/api/sensor-alert", methods=["POST"])
@users_access_required
def api_sensor_alert_set():
    from core.sensor_alert import save_config, load_config
    data = request.get_json(silent=True) or {}
    config = load_config()
    config["enabled"] = bool(data.get("enabled", config.get("enabled", False)))
    config["recipient_emails"] = data.get("recipient_emails", config.get("recipient_emails", []))
    config["user_ids"] = [int(x) for x in (data.get("user_ids", config.get("user_ids", [])))]
    config["temp_warning"] = float(data.get("temp_warning", config.get("temp_warning", 30)))
    config["temp_critical"] = float(data.get("temp_critical", config.get("temp_critical", 35)))
    config["hum_warning_high"] = float(data.get("hum_warning_high", config.get("hum_warning_high", 70)))
    config["hum_critical_high"] = float(data.get("hum_critical_high", config.get("hum_critical_high", 80)))
    config["hum_warning_low"] = float(data.get("hum_warning_low", config.get("hum_warning_low", 20)))
    config["cooldown_minutes"] = int(data.get("cooldown_minutes", config.get("cooldown_minutes", 30)))
    config["start_hour"] = int(data.get("start_hour", config.get("start_hour", 0)))
    config["end_hour"] = int(data.get("end_hour", config.get("end_hour", 24)))
    save_config(config)
    return jsonify({"status": "ok", "config": config})

# ─── System Settings (Google, SMTP) ──────────────────────────
_SYSTEM_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'system_config.json')

# Fields that can be edited from the admin panel
_SYSTEM_FIELDS = [
    'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET',
    'MAIL_SERVER', 'MAIL_PORT', 'MAIL_USE_TLS',
    'MAIL_USERNAME', 'MAIL_PASSWORD',
    'RECAPTCHA_SITE_KEY', 'RECAPTCHA_SECRET_KEY'
]

def _load_system_config():
    import json
    try:
        if os.path.exists(_SYSTEM_CONFIG_FILE):
            with open(_SYSTEM_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}

def _save_system_config(cfg):
    import json
    with open(_SYSTEM_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


@bp.route("/api/system-settings", methods=["GET"])
@users_access_required
def api_system_settings_get():
    cfg = _load_system_config()
    # Return only the fields we allow editing (mask secrets partially)
    result = {}
    for key in _SYSTEM_FIELDS:
        val = cfg.get(key, '')
        if key in ('GOOGLE_CLIENT_SECRET', 'MAIL_PASSWORD', 'RECAPTCHA_SECRET_KEY') and val:
            # Mask secret: show first 4 + last 4 chars
            if len(val) > 8:
                result[key] = val[:4] + '•' * (len(val) - 8) + val[-4:]
            else:
                result[key] = '•' * len(val)
        else:
            result[key] = val
    return jsonify({"status": "ok", "settings": result, "fields": _SYSTEM_FIELDS})


@bp.route("/api/system-settings", methods=["POST"])
@users_access_required
def api_system_settings_set():
    import json
    from flask import current_app
    data = request.get_json(silent=True) or {}
    cfg = _load_system_config()

    # Update only provided fields (secrets that are masked get special handling)
    for key in _SYSTEM_FIELDS:
        if key in data:
            val = data[key]
            # If value contains masking chars, it means user didn't change it
            if key in ('GOOGLE_CLIENT_SECRET', 'MAIL_PASSWORD', 'RECAPTCHA_SECRET_KEY'):
                if val and '•' in val:
                    continue  # Keep old value
            if key == 'MAIL_PORT':
                try:
                    val = int(val)
                except (ValueError, TypeError):
                    val = 587
            if key == 'MAIL_USE_TLS':
                val = bool(val) if not isinstance(val, bool) else val
            cfg[key] = val

    _save_system_config(cfg)

    # Audit
    try:
        _audit_user_action(
            current_user.id,
            "system_settings_changed",
            {"changed_keys": [k for k in data.keys() if k in _SYSTEM_FIELDS]}
        )
    except Exception:
        pass

    # Reload settings in the running app
    try:
        from config import settings
        for key in _SYSTEM_FIELDS:
            if hasattr(settings, key) and key in cfg:
                setattr(settings, key, cfg[key])
    except Exception:
        pass

    return jsonify({"status": "ok", "message": "تنظیمات با موفقیت ذخیره شد. نیاز به ریستارت سرور برای اعمال کامل تغییرات است."})


@bp.route("/api/system-settings/test-email", methods=["POST"])
@users_access_required
def api_system_settings_test_email():
    """Send a test email to verify SMTP settings."""
    from config import settings
    import smtplib
    from email.mime.text import MIMEText

    data = request.get_json(silent=True) or {}
    to_email = data.get("to_email", settings.MAIL_USERNAME)

    if not settings.MAIL_SERVER or not settings.MAIL_USERNAME:
        return jsonify({"error": "SMTP server or username not configured"}), 400

    try:
        msg = MIMEText("این یک ایمیل تست از سیستم مانیتورینگ پرینتر است.", "plain", "utf-8")
        msg["Subject"] = "تست ایمیل - مانیتورینگ پرینتر"
        msg["From"] = settings.MAIL_USERNAME
        msg["To"] = to_email

        with smtplib.SMTP(settings.MAIL_SERVER, settings.MAIL_PORT) as server:
            server.starttls()
            server.login(settings.MAIL_USERNAME, settings.MAIL_PASSWORD)
            server.send_message(msg)

        return jsonify({"status": "ok", "message": f"ایمیل تست به {to_email} ارسال شد."})
    except Exception as e:
        return jsonify({"error": f"خطا در ارسال ایمیل: {str(e)}"}), 500
