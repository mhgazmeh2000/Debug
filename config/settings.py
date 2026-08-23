# config/settings.py

"""
تنظیمات سراسری برنامه
"""

import json
import os

from dotenv import load_dotenv
load_dotenv()

# ─── فایل‌ها و مسیرها ───────────────────────────────────────────
PRINTERS_FILE        = "printers.json"
DB_PATH              = "logs.db.active"
OID_PROFILES_FILE    = "oid_profiles.json"
VALIDATION_LOG_FILE  = "oid_validation_errors.txt"

# ─── پرینترهای پیش‌فرض ─────────────────────────────────────────
# برای جلوگیری از commit شدن IPهای واقعی، لیست پیش‌فرض فقط از ENV خوانده می‌شود.
# مثال:
#   export DEFAULT_PRINTERS_JSON='[{"ip":"192.168.1.10","name":"Printer #1","community":"public"}]'
try:
    DEFAULT_PRINTERS = json.loads(os.getenv("DEFAULT_PRINTERS_JSON", "[]") or "[]")
    if not isinstance(DEFAULT_PRINTERS, list):
        DEFAULT_PRINTERS = []
except Exception:
    DEFAULT_PRINTERS = []

# ─── SNMP ───────────────────────────────────────────────────────
SNMP_PORT     = 161

# ─── Polling ────────────────────────────────────────────────────
# 🔥 تغییر: از 30 ثانیه به 60 ثانیه (1 دقیقه)
POLL_INTERVAL = 60   # ثانیه (1 دقیقه)

# ─── Flask ──────────────────────────────────────────────────────
FLASK_PORT = 5050

# ─── Environment & SECRET_KEY ───────────────────────────────────
# ENVIRONMENT می‌تواند "development" یا "production" باشد.
# در production، SECRET_KEY حتماً باید از env تنظیم شود وگرنه برنامه راه نمی‌افتد.
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
_INSECURE_DEFAULT_KEY = "change-this-secret-key-in-production"
SECRET_KEY = os.getenv("SECRET_KEY", "")

if not SECRET_KEY:
    if ENVIRONMENT == "production":
        raise RuntimeError(
            "❌ SECRET_KEY environment variable is REQUIRED in production!\n"
            "   Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\"\n"
            "   Then: export SECRET_KEY=<the-generated-key>"
        )
    # حالت development: کلید موقت با هشدار
    import warnings
    SECRET_KEY = _INSECURE_DEFAULT_KEY
    warnings.warn(
        "⚠  SECRET_KEY is using the INSECURE default value. "
        "Set the SECRET_KEY environment variable for security. "
        "This is acceptable for development only.",
        RuntimeWarning,
        stacklevel=2,
    )
elif SECRET_KEY == _INSECURE_DEFAULT_KEY and ENVIRONMENT == "production":
    raise RuntimeError(
        "❌ SECRET_KEY is set to the default INSECURE value in production!\n"
        "   Generate a new one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )

MAIL_SERVER = os.getenv("MAIL_SERVER", "")
MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "1") == "1"
MAIL_USERNAME = os.getenv("MAIL_USERNAME", "")
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "")
# ─── SMTP OAuth 2.0 (Gmail XOAUTH2) ──────────────────────────
# فعال‌سازی: MAIL_USE_OAUTH=1 تا به جای App Password از OAuth 2.0 استفاده شود
MAIL_USE_OAUTH = os.getenv("MAIL_USE_OAUTH", "0") == "1"
MAIL_OAUTH_CLIENT_ID = os.getenv("MAIL_OAUTH_CLIENT_ID", "")
MAIL_OAUTH_CLIENT_SECRET = os.getenv("MAIL_OAUTH_CLIENT_SECRET", "")
MAIL_OAUTH_REFRESH_TOKEN = os.getenv("MAIL_OAUTH_REFRESH_TOKEN", "")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "")
RECAPTCHA_SITE_KEY = os.getenv("RECAPTCHA_SITE_KEY", "")
RECAPTCHA_SECRET_KEY = os.getenv("RECAPTCHA_SECRET_KEY", "")

# ─── CORS ───────────────────────────────────────────────────────
# لیست origin هایی که اجازه دسترسی به API دارند (با کاما جدا کنید).
# مقدار خالی = هیچ CORS header ای ست نمی‌شود (same-origin only).
# مقدار "*" = همه origin ها (فقط برای development - خطرناک در production!)
# مثال: CORS_ALLOWED_ORIGINS="https://app.example.com,https://admin.example.com"
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]
if "*" in CORS_ALLOWED_ORIGINS and ENVIRONMENT == "production":
    raise RuntimeError(
        "❌ CORS_ALLOWED_ORIGINS='*' is NOT allowed in production! "
        "Specify exact origins like: https://app.example.com"
    )

# ─── دفاتر و subnetهای مجاز ─────────────────────────────────────
# اگر ENV تنظیم نشده باشد، fallback به subnetهای legacy انجام می‌شود تا
# محدودسازی دسترسی کاربران و فیلتر دفاتر از کار نیفتد.
_LEGACY_OFFICE_SUBNETS = {
    "imamat": "172.16.25",
    "soroush": "172.16.24",
    "falestin": "172.16.0",
    "elahiye": "172.16.32",
}
OFFICE_SUBNETS = {
    "imamat": os.getenv("OFFICE_SUBNET_IMAMAT") or _LEGACY_OFFICE_SUBNETS["imamat"],
    "soroush": os.getenv("OFFICE_SUBNET_SOROUSH") or _LEGACY_OFFICE_SUBNETS["soroush"],
    "falestin": os.getenv("OFFICE_SUBNET_FALESTIN") or _LEGACY_OFFICE_SUBNETS["falestin"],
    "elahiye": os.getenv("OFFICE_SUBNET_ELAHIYE") or _LEGACY_OFFICE_SUBNETS["elahiye"],
    "other": None,
}

# ─── پشتیبانی از فایل تنظیمات محلی office_subnets.json ─────────────
# اگر فایل وجود داشته باشد، مقادیر آن جایگزین مقدارهای ENV/پیش‌فرض می‌شود.
try:
    _settings_dir = os.path.dirname(os.path.abspath(__file__))
    _subnets_file = os.path.join(_settings_dir, 'office_subnets.json')
    if os.path.exists(_subnets_file):
        with open(_subnets_file, 'r', encoding='utf-8') as _f:
            _file_data = json.load(_f) or {}
            if isinstance(_file_data, dict):
                # If file exists, it is the source of truth (no merge with legacy defaults)
                OFFICE_SUBNETS = {str(k).strip(): v for k, v in _file_data.items() if str(k).strip()}
except Exception:
    # اگر فایل خراب باشد، از مقادیر ENV/پیش‌فرض استفاده می‌کنیم
    pass

# ─── Thresholds for toner alerts (percent)
TONER_ALERT_THRESHOLDS = {
    "critical": 5,   # زیر ۵٪ بحرانی
    "warning": 15,   # زیر ۱۵٪ هشدار
    "info": 30,      # زیر ۳۰٪ اطلاع‌رسانی (اختیاری)
}