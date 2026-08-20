import logging
from flask import Blueprint, jsonify, request
from core import store
from config.settings import POLL_INTERVAL, FLASK_PORT

log = logging.getLogger("PrinterMonitor")

bp = Blueprint("system", __name__)


@bp.route('/api/status')
def api_status():
    # 🔒 امنیت: host_ip و dashboard_url حذف شدند — نشت IP داخلی سرور
    # به کلاینت (و در گذشته به کاربران مهمان) لازم نیست.
    # فرانت داشبورد از همین origin استفاده می‌کند و به host_ip نیاز ندارد.
    from flask_wtf.csrf import generate_csrf
    return jsonify({
        "status":        "running",
        "poll_interval": POLL_INTERVAL,
        # توکن CSRF تازه: داشبورد صفحه‌ای long-lived است و توکن متا پس از
        # WTF_CSRF_TIME_LIMIT منقضی می‌شود (اعمال 400 روی همه POSTها).
        # فرانت با هر پاسخ status توکن را سایلنت به‌روز می‌کند.
        # امن برای افشا در این endpoint: same-origin + لاگین‌شده + بدون CORS.
        "csrf_token":    generate_csrf(),
        **store.poll_stats,
    })


@bp.route('/api/poll/now', methods=['POST'])
def api_poll_now():
    import threading
    from core.poller import poll_all, _polling_lock

    if _polling_lock.locked():
        log.warning('Manual pull request rejected: poll_all is already running')
        return jsonify({"status": "busy", "message": "Pull already in progress", "running": True})

    log.info('Manual pull requested via API')
    try:
        threading.Thread(target=poll_all, daemon=True).start()
        return jsonify({"status": "started"})
    except Exception:
        log.exception('Failed to start manual pull')
        # 🔒 امنیت: جزئیات خطای داخلی (str(e)) به کلاینت برنگردد
        return jsonify({"status": "error", "error": "internal server error"}), 500
