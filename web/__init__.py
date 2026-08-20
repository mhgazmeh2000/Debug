"""
ساخت Flask app و ثبت همه blueprintها
"""

import os
from flask import Flask, jsonify, redirect, request, url_for
from flask_login import current_user
from web.routes.dashboard  import bp as bp_dashboard
from web.routes.printers   import bp as bp_printers
from web.routes.logs       import bp as bp_logs
from web.routes.export_bp  import bp as bp_export
from web.routes.scan       import bp as bp_scan
from web.routes.discover   import bp as bp_discover
from web.routes.stats      import bp as bp_stats
from web.routes.validation import bp as bp_validation
from web.routes.system     import bp as bp_system
from web.routes.users      import bp as bp_users
from web.routes.security   import bp as bp_security_audit
from web.routes.groups     import bp as bp_groups
from web.routes.import_db import bp as bp_import_db
from web.routes.yield_status import bp as bp_yield_status
from web.auth import auth_bp, init_auth, user_can_access_module
from web.security import init_security, csrf
from config import settings

# مسیر مطلق پوشه web/ (همین فایل در web/ قرار دارد)
_WEB_DIR = os.path.dirname(os.path.abspath(__file__))


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=os.path.join(_WEB_DIR, "templates"),
        static_folder=os.path.join(_WEB_DIR, "static"),
    )

    app.config.update(
        SECRET_KEY=settings.SECRET_KEY,
        MAIL_SERVER=settings.MAIL_SERVER,
        MAIL_PORT=settings.MAIL_PORT,
        MAIL_USE_TLS=settings.MAIL_USE_TLS,
        MAIL_USERNAME=settings.MAIL_USERNAME,
        MAIL_PASSWORD=settings.MAIL_PASSWORD,
        GOOGLE_CLIENT_ID=settings.GOOGLE_CLIENT_ID,
        GOOGLE_CLIENT_SECRET=settings.GOOGLE_CLIENT_SECRET,
        RECAPTCHA_SITE_KEY=settings.RECAPTCHA_SITE_KEY,
        RECAPTCHA_SECRET_KEY=settings.RECAPTCHA_SECRET_KEY,
        ASSET_VERSION=os.getenv("ASSET_VERSION", "20260615-sensor-2"),
    )

    init_auth(app)
    init_security(app)

    for blueprint in (
        auth_bp,
        bp_dashboard, bp_printers, bp_logs, bp_export,
        bp_scan, bp_discover, bp_stats, bp_validation, bp_system, bp_users,
        bp_security_audit, bp_import_db, bp_yield_status, bp_groups,
    ):
        app.register_blueprint(blueprint)

    # ─── معافیت endpoint های JSON API از CSRF ──────────────────────
    # برای endpointهای JSON که از session احراز هویت می‌کنند، CSRF protection
    # کنترل می‌شود اما باید مرورگر/کلاینت header X-CSRFToken بفرستد.
    # برای حالا، endpoint های GET (که side-effect ندارند) و کل bp_export
    # که فقط GET دارد، نیازی به CSRF ندارند.
    # Flask-WTF خودکار GET/HEAD/OPTIONS را معاف می‌کند.

    @app.before_request
    def protect_routes():
        endpoint = request.endpoint or ""
        public_endpoints = {
            "static",
            "auth.login",
            "auth.register",
            "auth.forgot_password",
            "auth.reset_password",
            "auth.google_login",
            "auth.google_callback",
            # NOTE: "system.api_status" عمداً از لیست عمومی حذف شد —
            # بدون login نباید آمار polling و وضعیت سرویس دیده شود.
        }
        if endpoint.startswith("static"):
            return None
        if not current_user.is_authenticated:
            if endpoint in public_endpoints:
                return None
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "unauthorized", "login_url": url_for("auth.login", next=request.url)}), 401
            return redirect(url_for("auth.login", next=request.url))

        if not getattr(current_user, "is_verified", False):
            if request.path in ("/", "/auth/logout") or endpoint == "auth.logout":
                return None
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "pending_verification"}), 403
            return redirect(url_for("dashboard.index"))

        endpoint_modules = {
            "printers.api_printers": "printers",
            "printers.api_printer": "printers",
            "printers.debug_printer": "printers",
            "printers.api_add": "printers",
            "printers.api_bulk_add": "printers",
            "printers.api_remove": "printers",
            "printers.api_auto_add_printer": "printers",
            "printers.rename_printer": "printers",
            "logs.api_printer_log": "logs",
            "logs.api_all_logs": "logs",
            "logs.api_clear_logs": "logs",
            "logs.api_manual_event": "logs",
            "export.export_excel": "excel",
            "export.export_logs": "excel",
            "users.users_page": "users",
            "users.api_office_options": "users",
            "users.api_users": "users",
            "users.api_user_role": "users",
            "users.api_user_verify": "users",
            "users.api_delete_user": "users",
            "users.api_user_add": "users",
            "users.api_user_access": "users",
        }
        module_name = endpoint_modules.get(endpoint)
        if module_name and not user_can_access_module(current_user, module_name):
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "forbidden"}), 403
            return redirect(url_for("dashboard.index"))

        role = getattr(current_user, "role", "viewer")
        if role == "admin":
            return None

        viewer_allowed = {
            "dashboard.index",
            "system.api_status",
            "groups.api_groups",
            "printers.api_printers",
            "printers.api_printer",
            "logs.api_printer_log",
            "logs.api_all_logs",
            "stats.api_daily_stats",
            "auth.logout",
        }
        manager_allowed = viewer_allowed | {
            "export.export_excel",
            "users.users_page",
            "users.api_office_options",
            "users.api_users",
            "users.api_user_add",
            "users.api_user_role",
            "users.api_user_verify",
            "users.api_user_access",
            "users.api_delete_user",
        }

        if role == "manager":
            if endpoint == "export.export_logs":
                if request.args.get("format", "csv").lower() == "excel":
                    return None
            elif endpoint in manager_allowed:
                return None

            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "forbidden"}), 403
            return redirect(url_for("dashboard.index"))

        if endpoint in viewer_allowed:
            return None
        if request.path.startswith("/api/") or request.is_json:
            return jsonify({"error": "forbidden"}), 403
        return redirect(url_for("dashboard.index"))

    # ─── CORS (only if explicitly configured via env) ────────────
    # By default no CORS headers are set (same-origin only).
    # Configure via CORS_ALLOWED_ORIGINS env variable.
    if settings.CORS_ALLOWED_ORIGINS:
        @app.after_request
        def cors(r):
            origin = request.headers.get("Origin", "")
            allowed = settings.CORS_ALLOWED_ORIGINS
            # دامنه درخواست‌کننده باید در whitelist باشد (یا '*' برای dev)
            if "*" in allowed:
                r.headers['Access-Control-Allow-Origin'] = '*'
            elif origin in allowed:
                r.headers['Access-Control-Allow-Origin'] = origin
                # Vary: Origin مهم است تا cacheها origin مختلف را جدا نگه دارند
                r.headers['Vary'] = 'Origin'
            else:
                # origin مجاز نیست → CORS header نگذاریم
                return r
            r.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-CSRFToken'
            r.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
            r.headers['Access-Control-Allow-Credentials'] = 'true'
            r.headers['Access-Control-Max-Age'] = '3600'
            return r

    @app.context_processor
    def inject_settings():
        # Build a list of office group defs for templates/JS, using subnets from settings
        name_map = {
            'imamat': 'دفتر امامت',
            'soroush': 'دفتر سروش',
            'falestin': 'دفتر فلسطین',
            'elahiye': 'دفتر الهیه',
            'other': 'سایر',
        }
        icon_map = {
            'other': '🖨'
        }
        color_map = {
            'imamat': 'cyan', 'soroush': 'green', 'falestin': 'yellow', 'elahiye': 'magenta', 'other': 'orange'
        }
        office_groups = []
        for oid, subnet in settings.OFFICE_SUBNETS.items():
            office_groups.append({
                'id': oid,
                'name': name_map.get(oid, oid),
                'subnet': subnet,
                'icon': icon_map.get(oid, '🏢'),
                'color': color_map.get(oid, 'cyan'),
            })

        return dict(poll_interval=settings.POLL_INTERVAL, OFFICE_GROUPS=office_groups)

    return app