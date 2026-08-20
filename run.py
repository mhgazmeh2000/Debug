#!/usr/bin/env python3
"""
نقطه ورود برنامه Multi Printer Monitoring
اجرا: python run.py   (از داخل پوشه pm2/)
"""

import os
import sys
import socket
import logging
import threading
import signal
import time
import platform

# اطمینان از اینکه ریشه پروژه در sys.path است
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config import settings as _settings

# Allow overriding Flask port via environment variable `FLASK_PORT`.
# Keep default in config/settings.py but let start scripts set FLASK_PORT in env.
try:
    DEFAULT_FLASK_PORT = int(_settings.FLASK_PORT)
except Exception:
    DEFAULT_FLASK_PORT = 5053

FLASK_PORT = int(os.getenv("FLASK_PORT", DEFAULT_FLASK_PORT))
from core.database import init_db, prune_old_print_logs
from core import store
from core.poller import poll_all, polling_loop
from core.oid.scanner import startup_scan_all, weekly_scan_loop
from web import create_app

# تنظیم اولیه لاگینگ برای نقطه ورود
log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_name, logging.INFO)
logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("PrinterMonitor")

# ─── رویداد توقف نرم (graceful shutdown) ───
stop_event = threading.Event()
shutdown_in_progress = False

def signal_handler(sig, frame):
    global shutdown_in_progress
    if shutdown_in_progress:
        return  # اگر shutdown در حال اجرا است، دوباره صدا نزن
    shutdown_in_progress = True
    log.info("\n🛑 دریافت سیگنال توقف – در حال بستن نرم برنامه...")
    stop_event.set()
    # خروج تمیز: SystemExit باعث unwind شدن Flask و بسته شدن daemon threadها می‌شود.
    # os._exit(0) قبلی cleanup/flush را دور می‌زد.
    raise SystemExit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ─── تابع پاکسازی خودکار لاگ‌های قدیمی (هر 24 ساعت) ───
def cleanup_loop():
    """
    حلقه‌ای که هر 24 ساعت یک بار اجرا می‌شود.
    با توجه به درخواست کاربر، لاگ‌های PRINT هرگز پاک نمی‌شوند (مادام‌العمر).
    این حلقه فقط برای حفظ ساختار قبلی وجود دارد و عملیات پاکسازی انجام نمی‌دهد.
    """
    while not stop_event.is_set():
        try:
            # درخواست: لاگ‌های PRINT مادام‌العمر باشند → بدون پاکسازی
            # در صورت نیاز به پاکسازی انواع دیگر در آینده می‌توان اینجا اضافه کرد
            pass
        except Exception as e:
            log.exception("Cleanup loop error: %s", e)
        # منتظر 24 ساعت یا تا زمان دریافت سیگنال توقف
        for _ in range(86400):
            if stop_event.is_set():
                break
            time.sleep(1)

# ─── تابع پاکسازی خودکار ترمینال (هر 24 ساعت) ───
def clear_terminal_loop():
    """
    هر 24 ساعت یک بار صفحه ترمینال را پاک می‌کند.
    فقط خروجی کنسول را پاک می‌کند و تأثیری بر فایل‌های لاگ ندارد.
    """
    # تشخیص سیستم عامل برای انتخاب دستور مناسب
    if platform.system() == "Windows":
        clear_cmd = "cls"
    else:
        clear_cmd = "clear"
    
    while not stop_event.is_set():
        try:
            # صبر 24 ساعت
            for _ in range(86400):
                if stop_event.is_set():
                    break
                time.sleep(1)
            if not stop_event.is_set():
                os.system(clear_cmd)
                log.info("🧹 صفحه ترمینال پاک شد (پاکسازی خودکار هر 24 ساعت)")
        except Exception as e:
            log.exception("Terminal clear loop error: %s", e)

def main():
    # راه‌اندازی DB
    try:
        init_db()
    except Exception as exc:
        # قفل موقت SQLite روی share نباید مانع بالا آمدن رابط وب شود.
        # عملیات دیتابیسی بعدی با timeout/retry خطای مناسب خود را ثبت می‌کند.
        log.exception("Database initialization deferred because the database is unavailable: %s", exc)

    # Flask app
    app = create_app()

    # نمایش وضعیت اولیه
    def _get_local_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            try:
                return socket.gethostbyname(socket.gethostname())
            except Exception:
                return "127.0.0.1"

    host_ip = _get_local_ip()
    log.info("""
╔══════════════════════════════════════════════════════╗
║    Multi Printer Monitoring  |  پایش لحظه‌ای        ║
╠══════════════════════════════════════════════════════╣""")
    with store.printers_lock:
        for p in store.PRINTERS:
            log.info(f"  🖨  {p['name']:<14} {p['ip']}  (community: {p['community']})")
    log.info(f"╠══════════════════════════════════════════════════════╣\n  Local   → http://localhost:{FLASK_PORT}/\n  Network → http://{host_ip}:{FLASK_PORT}/\n╚══════════════════════════════════════════════════════╝")

    # Startup OID Scan (background)
    with store.printers_lock:
        printers_copy = list(store.PRINTERS)

    threading.Thread(
        target=lambda: startup_scan_all(printers_copy, force=True),
        daemon=True, name="startup-scan",
    ).start()

    # Polling (حلقه‌های بی‌نهایت با پشتیبانی از stop_event)
    threading.Thread(target=poll_all,     daemon=True, name="poll-init").start()
    threading.Thread(target=polling_loop, daemon=True, name="poll-loop").start()

    # اسکن هفتگی
    threading.Thread(
        target=weekly_scan_loop,
        args=(lambda: list(store.PRINTERS),),
        daemon=True, name="weekly-scan",
    ).start()

    # پاکسازی خودکار لاگ‌های PRINT قدیمی (هر 24 ساعت) - بدون حذف PRINTها
    threading.Thread(target=cleanup_loop, daemon=True, name="cleanup-loop").start()

    # پاکسازی خودکار ترمینال هر 24 ساعت
    threading.Thread(target=clear_terminal_loop, daemon=True, name="clear-terminal").start()

    # Flask server
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=debug_mode, use_reloader=False)


if __name__ == "__main__":
    main()