# core/poller.py (PATCHED - All Critical Bugs Fixed)

"""
چرخه polling:
- collect: جمع‌آوری داده از یک پرینتر با routing به collector مناسب
- poll_all: polling موازی همه پرینترها
- polling_loop: حلقه بی‌نهایت با POLL_INTERVAL
"""

import time
import threading
from threading import Lock
import logging
from datetime import datetime

from config.settings import POLL_INTERVAL
from core import store
from core.database import add_event
from core.snmp.protocol import snmp_get_with_fallback, snmp_get_first, snmp_debug_get
from core.snmp.oid_map import OIDS
from core.collectors.base import si, detect_brand

# 🔥 تغییر: استفاده از enhanced_collector به جای کالکتورهای جداگانه
from core.collectors.base_enhanced import collect_enhanced

# fallback به collectorهای برند-محور برای حفظ ثبت رویدادها در صورت خطای enhanced
from core.collectors.sensor import collect_sensor
from core.collectors.toshiba import collect_toshiba
from core.collectors.hp import collect_hp
from core.collectors.canon import collect_canon
from core.collectors.brother import collect_brother

log = logging.getLogger("PrinterMonitor")

# قفل برای جلوگیری از اجرای هم‌زمان poll_all
_polling_lock = threading.Lock()

# ✅ باگ #1: قفل جداگانه برای processed_ips (جلوگیری از Race Condition)
_processed_ips_lock = Lock()
_ip_locks_guard = Lock()
_ip_poll_locks = {}

_SNMP_REACHABLE_STATUSES = {
    "ok",
    "no_such_object",
    "no_such_instance",
    "end_of_mib_view",
    "error_status",
    "request_id_mismatch",
}

# ✅ پنجرهی ارفاق قبل از اعلام آفلاین: یک چرخه‌ی گمشده‌ی UDP (شایع در شبکه‌های
# شلوغ و SNMP stack کند Toshibaها) نباید دستگاه را آفلاین کند. دستگاه فقط بعد از
# دو شکست health متوالی آفلاین اعلام می‌شود و رویداد STATUS ثبت می‌گردد.
_OFFLINE_GRACE_CYCLES = 2
_health_fail_lock = threading.Lock()
_health_fail_counts: dict[str, int] = {}


def _note_health_failure(ip: str) -> int:
    with _health_fail_lock:
        n = _health_fail_counts.get(ip, 0) + 1
        _health_fail_counts[ip] = n
        return n


def _reset_health_failure(ip: str) -> None:
    with _health_fail_lock:
        _health_fail_counts.pop(ip, None)


def _get_ip_lock(ip: str) -> Lock:
    """برای جلوگیری از poll هم‌زمان یک IP، حتی اگر thread قبلی دیر تمام شود."""
    with _ip_locks_guard:
        lock = _ip_poll_locks.get(ip)
        if lock is None:
            lock = Lock()
            _ip_poll_locks[ip] = lock
        return lock


def _probe_snmp_agent_reachable(ip: str, community: str, health_oids: list[str], timeout: float = 1.5):
    """بررسی می‌کند آیا SNMP agent پاسخ می‌دهد، حتی اگر هیچ OID قابل‌خواندنی نداشته باشد."""
    seen = set()
    for idx, oid in enumerate(health_oids, 1):
        if not oid or oid in seen:
            continue
        seen.add(oid)
        for version in (2, 1):
            diag = snmp_debug_get(ip, oid, community, timeout=timeout, request_id=(idx * 10 + version), version=version)
            if diag.get("status") in _SNMP_REACHABLE_STATUSES:
                return diag
    return None


def collect(printer: dict) -> dict:
    """
    جمع‌آوری داده از یک پرینتر.
    🔥 تغییر: استفاده از enhanced_collector برای همه دستگاه‌ها (به جز سنسور)

    ✅ فیکس duplicate: قفل per-IP اینجا گرفته می‌شود (نه فقط در poll_all) تا
    هیچ دو poll هم‌زمانی روی یک دستگاه اتفاق نیفتد — قبلاً مسیرهای api_add /
    bulk-add / auto-add بدون قفل collect را صدا می‌زدند و امکان دو PRINT
    هم‌زمان از یک snapshot وجود داشت.
    """
    ip = printer["ip"]
    ip_lock = _get_ip_lock(ip)
    if not ip_lock.acquire(blocking=False):
        log.warning("Skipping %s because another poll for this IP is still running", ip)
        with store.data_lock:
            previous = store.printer_data.get(ip) or {}
        # خروجی قدیمی را برگردان تا UI با داده‌ی ناقص جایگزین نشود
        result = dict(previous) if previous else {
            "ip": ip,
            "name": printer.get("name", ip),
            "nickname": printer.get("nickname", ""),
            "brand": printer.get("brand", ""),
            "device_type": printer.get("device_type", "unknown"),
            "online": True,
            "partial": True,
            "last_poll": None,
            "error": "poll already running",
        }
        result["skipped_concurrent_poll"] = True
        return result
    try:
        return _collect_inner(printer)
    finally:
        ip_lock.release()


def _collect_inner(printer: dict) -> dict:
    """بدنه‌ی اصلی collect — فقط از طریق collect() (دارای قفل) صدا زده می‌شود."""
    ip = printer["ip"]
    name = printer["name"]
    nickname = printer.get("nickname", "")
    community = printer.get("community", "public")
    brand = printer.get("brand", "").lower()
    device_type = printer.get("device_type", "unknown")

    log.info(f"Pulling {name} ({ip}) [{brand or 'auto'}] - using enhanced collector")
    start = time.time()

    # تست اولیه برای آنلاین بودن
    # بعضی دستگاه‌ها (مثل برخی Toshibaها) به sysDescr پاسخ نمی‌دهند اما به sysUpTime
    # یا OIDهای vendor-specific پاسخ معتبر می‌دهند. برای backward compatibility،
    # چند health OID را به ترتیب امتحان می‌کنیم.
    health_oids = [
        "1.3.6.1.2.1.1.1.0",           # sysDescr
        "1.3.6.1.2.1.1.3.0",           # standard sysUpTime
        "1.3.6.1.2.1.1.5.0",           # sysName
        OIDS.get("uptime", "1.3.6.1.2.1.1.3.0"),  # vendor uptime (e.g. Toshiba)
        "1.3.6.1.2.1.43.5.1.1.16.1",   # Printer-MIB model/name
        OIDS.get("model"),             # vendor model (e.g. Toshiba)
    ]
    # ✅ Toshiba: برخی مدل‌ها به OIDهای استاندارد در view محدود پاسخ نمی‌دهند؛
    # شمارنده‌ی vendor را به ابتدای لیست بیاور تا health probe سریع‌تر موفق شود.
    if brand == "toshiba":
        health_oids = [
            OIDS.get("print_total"),   # Toshiba vendor total counter
            OIDS.get("model"),         # Toshiba vendor model
            OIDS.get("uptime", "1.3.6.1.2.1.1.3.0"),
        ] + [o for o in health_oids if o not in (OIDS.get("model"),)]
    # حذف تکراری‌ها با حفظ ترتیب
    if brand == "sensor":
        health_oids = [
            "1.3.6.1.4.1.47206.1.0",
            "1.3.6.1.4.1.47206.110.1.2.0",
            "1.3.6.1.4.1.47206.111.1.2.0",
        ] + health_oids
    health_oids = list(dict.fromkeys([oid for oid in health_oids if oid]))
    health_timeout = 3.0 if brand == "toshiba" else 2.0
    test, used_oid = snmp_get_first(ip, health_oids, community, timeout=health_timeout)
    online = test is not None
    snmp_reachable_diag = None
    if online and used_oid != "1.3.6.1.2.1.1.1.0":
        log.info(f"Online probe fallback succeeded for {ip} using OID {used_oid}")
    if not online:
        snmp_reachable_diag = _probe_snmp_agent_reachable(ip, community, health_oids, timeout=1.5)

    with store.data_lock:
        was_online = store.printer_data.get(ip, {}).get("online", None)

    if online:
        _reset_health_failure(ip)

    if not online:
        # ✅ ارفاق: اولین شکست health، دستگاهِ قبلاً آنلاین را فوری آفلاین نمی‌کند
        fail_count = _note_health_failure(ip)
        if was_online and fail_count < _OFFLINE_GRACE_CYCLES:
            with store.data_lock:
                previous_data = dict(store.printer_data.get(ip, {}) or {})
            previous_data["degraded"] = True
            previous_data["degraded_reason"] = (
                f"health probe ناموفق (تلاش {fail_count}/{_OFFLINE_GRACE_CYCLES}) — حفظ وضعیت قبلی تا چرخه بعد"
            )
            log.info(
                "%s آفلاین اعلام نشد (grace %s/%s): UDP drop/پاسخ ناقص موقتی؛ چرخه بعد تأیید می‌شود",
                ip, fail_count, _OFFLINE_GRACE_CYCLES,
            )
            return previous_data

        elapsed = int((time.time() - start) * 1000)

        # اگر agent پاسخ SNMP داده ولی OIDها قابل‌خواندن نیستند، دستگاه را به‌عنوان
        # reachable نمایش می‌دهیم اما با هشدار واضح، تا با حالت offline واقعی اشتباه نشود.
        if snmp_reachable_diag:
            msg = (
                f"SNMP agent reachable but required OIDs are not readable "
                f"(community={community}, status={snmp_reachable_diag.get('status')}, oid={snmp_reachable_diag.get('oid')})"
            )
            log.warning("%s -> %s", ip, msg)
            with store.data_lock:
                previous_data = store.printer_data.get(ip, {}) or {}
            prev = store._prev.get(ip) or {}
            return {
                "ip": ip,
                "name": name,
                "nickname": nickname,
                "brand": brand,
                "device_type": device_type,
                "online": True,
                "partial": True,
                "last_poll": datetime.now().isoformat(),
                "poll_ms": elapsed,
                "error_type": "snmp_restricted",
                "error": msg,
                "device": previous_data.get("device") or {"model": "Unknown", "serial": "N/A", "firmware": "N/A", "uptime_str": "N/A"},
                "counters": previous_data.get("counters") or {
                    "total": prev.get("print_total", 0) or 0,
                    "full_color": prev.get("full_color"),
                    "black_white": prev.get("black_white", 0),
                },
                "paper_sizes": previous_data.get("paper_sizes") or {},
                "trays": previous_data.get("trays") or [],
                "toners": previous_data.get("toners") or {},
                "alerts": [
                    {
                        "message": "SNMP پاسخ می‌دهد اما OIDهای لازم با community فعلی قابل خواندن نیستند",
                        "code": "snmp_restricted",
                    }
                ],
            }

        if was_online:
            add_event(ip, "STATUS", {"message": "دستگاه آفلاین شد", "severity": "error"})
        return {
            "ip": ip, "name": name, "nickname": nickname, "brand": brand, "device_type": device_type,
            "online": False,
            "last_poll": datetime.now().isoformat(),
            "poll_ms": elapsed,
            "error": "Device unreachable",
        }

    if was_online is False:
        add_event(ip, "STATUS", {"message": "دستگاه آنلاین شد", "severity": "success"})

    # تشخیص برند (اگر قبلاً مشخص نبود)
    if brand == "sensor":
        # سنسورها با کالکتور مخصوص خود
        result = collect_sensor(ip, name, community, start)
        result["nickname"] = nickname
        result["device_type"] = "sensor"
        return result
    
    if not brand or brand == "unknown":
        brand = detect_brand(ip, community)
        log.info(f"  → برند شناسایی شد: {brand}")
        with store.printers_lock:
            for p in store.PRINTERS:
                if p["ip"] == ip:
                    p["brand"] = brand
                    store.save_printers(store.PRINTERS)
                    break

    # 🔥 استفاده از enhanced_collector برای همه پرینترها
    try:
        result = collect_enhanced(printer)
        result["nickname"] = nickname
        result["device_type"] = result.get("device_type", device_type)
        return result
    except Exception as e:
        log.error(f"Enhanced collector failed for {ip}: {e}, falling back to legacy collector")

        legacy_collectors = {
            "toshiba": collect_toshiba,
            "hp": collect_hp,
            "canon": collect_canon,
            "brother": collect_brother,
        }
        legacy_collector = legacy_collectors.get(brand)
        if legacy_collector is not None:
            try:
                result = legacy_collector(ip, name, community, start)
                result["nickname"] = nickname
                result["device_type"] = result.get("device_type", device_type)
                result["error"] = str(e)
                return result
            except Exception as legacy_error:
                log.exception("Legacy collector also failed for %s: %s", ip, legacy_error)

        # آخرین fallback: حفظ snapshot قبلی به جای صفر کردن شمارنده‌ها
        prev = store._prev.get(ip) or {}
        prev_total = prev.get("print_total", 0) or 0
        elapsed = int((time.time() - start) * 1000)
        return {
            "ip": ip, "name": name, "nickname": nickname, "brand": brand,
            "online": True,
            "last_poll": datetime.now().isoformat(),
            "poll_ms": elapsed,
            "device": {"model": "Unknown", "serial": "N/A", "firmware": "N/A", "uptime_str": "N/A"},
            "counters": {
                "total": prev_total,
                "full_color": prev.get("full_color"),
                "black_white": prev.get("black_white", 0),
            },
            "paper_sizes": {}, "trays": [], "toners": {}, "alerts": [],
            "error": str(e),
        }


def poll_one(p: dict):
    """Poll یک پرینتر واحد"""
    data = collect(p)
    with store.data_lock:
        store.printer_data[p["ip"]] = data


def poll_all():
    """اجرای poll برای همه پرینترها با جلوگیری از اجرای هم‌زمان"""
    with _polling_lock:
        # ✅ فلگ وضعیت برای UI: وقتی چرخه در حال اجراست، دکمه Pull دستی
        # حالت «در حال اجرا» نشان می‌دهد به‌جای خطای ۴۰۹ گیج‌کننده.
        store.poll_stats["is_polling"] = True
        try:
            with store.printers_lock:
                current = list(store.PRINTERS)

            log.info(f"🔄 Starting pull cycle for {len(current)} devices (interval={POLL_INTERVAL}s)")
            results = {}
            processed_ips = set()

            def _poll(p):
                ip = p["ip"]
                # ✅ باگ #1: Race Condition - استفاده از قفل برای جلوگیری از polling مضاعف
                with _processed_ips_lock:
                    if ip in processed_ips:
                        log.warning(f"Skipping duplicate poll for {ip}")
                        return
                    processed_ips.add(ip)
                try:
                    results[ip] = collect(p)
                except Exception as e:
                    log.error(f"Error polling {ip}: {e}")

            threads = [threading.Thread(target=_poll, args=(p,), daemon=True) for p in current]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)  # ✅ افزایش timeout به 60 ثانیه

            with store.data_lock:
                store.printer_data.update(results)
                store.poll_stats["count"] += 1
                store.poll_stats["last"] = datetime.now().isoformat()
                store.poll_stats["errors"] = sum(1 for d in results.values() if not d.get("online"))

            log.info(f"✅ Pull cycle completed: {len(results)} devices, "
                     f"{store.poll_stats['errors']} errors, "
                     f"next pull in {POLL_INTERVAL}s")
        finally:
            store.poll_stats["is_polling"] = False


def polling_loop():
    """حلقه بی‌نهایت polling"""
    # poll_all در startup یک چرخه فوری اجرا می‌کند؛ این sleep مانع اجرای
    # بلافاصلهٔ چرخهٔ دوم و ثبت PRINTهای تکراری در چند ثانیهٔ اول می‌شود.
    time.sleep(POLL_INTERVAL)
    while True:
        try:
            poll_all()
        except Exception as e:
            log.error(f"Error in pull loop: {e}")
        time.sleep(POLL_INTERVAL)