"""
اسکن OIDهای یک پرینتر و ذخیره پروفایل در oid_profiles.json
+ اسکن startup و اسکن هفتگی دوره‌ای
+ تشخیص و ذخیره نوع دستگاه (رنگی/تک‌رنگ/دماسنج) با استفاده از OIDهای فعال
"""

import os
import json
import time
import threading
import logging
from datetime import datetime

from config.settings import OID_PROFILES_FILE, VALIDATION_LOG_FILE
from core.snmp.protocol import snmp_get_with_fallback
from core.collectors.base import si, detect_brand
from core.oid.catalog import OID_CATALOG
from core.oid.validator import _validate_oid_value_strict, validate_oid_value, log_validation_error
from core.device_classifier import classify_from_scan   # ← classifier مبتنی بر OIDهای اسکن‌شده
from core import store

log = logging.getLogger("PrinterMonitor")

WEEK = 7 * 24 * 3600
_profiles_lock = threading.Lock()

def _load_oid_profiles() -> dict:
    if os.path.exists(OID_PROFILES_FILE):
        try:
            with open(OID_PROFILES_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"خطا در بارگذاری OID profiles: {e}")
    return {}


def _save_oid_profiles(profiles: dict) -> bool:
    try:
        with open(OID_PROFILES_FILE, "w", encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log.error(f"خطا در save profiles: {e}")
        return False


def _oid_unit(category: str, key: str) -> str:
    unit_map = {
        "counter":  "pages",
        "supply":   "%" if "level" in key or "status" in key else "dots",
        "tray":     "%",
        "status":   "code",
        "sys":      "str",
        "identity": "str",
    }
    return unit_map.get(category, "")


def scan_printer_oids(ip: str, community: str = "public", force: bool = False) -> dict:
    with _profiles_lock:
        profiles = _load_oid_profiles()

    if not force and ip in profiles:
        cached = profiles[ip]
        log.info(f"  [scan] {ip} → از cache ({len(cached.get('oids',{}))} OID)")
        return cached

    log.info(f"  [scan] {ip} → شروع probe {len(OID_CATALOG)} OID ...")
    t0    = time.time()
    brand = detect_brand(ip, community)

    active_oids   = {}
    current_vals  = {}
    rejected_oids = {}

    # فیلتر OID بر اساس برند
    brand_prefixes = {
        "toshiba": ("tsh_", "sys_", "cover_", "tray", "toner"),
        "canon":   ("canon_", "sys_", "cover_", "tray", "toner", "model_", "serial_"),
        "hp":      ("hp_", "sys_", "cover_", "tray", "toner", "print_", "model_", "serial_"),
        "brother": ("bro_", "sys_", "cover_", "tray", "toner", "print_", "model_", "serial_"),
    }

    for key, (oid, typ, category, desc) in OID_CATALOG.items():
        if brand in brand_prefixes:
            if not any(key.startswith(p) for p in brand_prefixes[brand]):
                continue

        val = snmp_get_with_fallback(ip, oid, community, timeout=2.0)
        if val is None:
            continue
        str_val = str(val).strip()
        if not str_val or str_val in ("None", "0.0"):
            continue

        valid, reason, error_type = _validate_oid_value_strict(key, val, category)
        if not valid:
            rejected_oids[key] = {"oid": oid, "value": str_val,
                                  "reason": reason, "error_type": error_type}
            log.debug(f"  [scan] {ip} OID رد شد: {key}={val!r} → {reason}")
            log_validation_error(ip, f"rejected_{error_type}",
                                 f"Key: {key} | OID: {oid} | Value: {str_val} | Reason: {reason}")
            continue

        active_oids[key]  = oid
        current_vals[key] = val

    elapsed = int((time.time() - t0) * 1000)

    def gv(k, default=None):
        return current_vals.get(k, default)

    model = str(gv("tsh_model") or gv("canon_model") or gv("bro_model") or
                gv("model_mib") or gv("sys_hostname") or "Unknown").strip()[:100] or "Unknown"

    serial = str(gv("tsh_serial") or gv("canon_serial") or gv("bro_serial") or
                 gv("serial_mib") or "N/A").strip()[:100]

    if brand == "toshiba":
        total = si(gv("tsh_total", 0))
    elif brand == "canon":
        total = si(gv("canon_total") or gv("print_total", 0))
    else:
        total = si(gv("print_total", 0))
    total = max(0, total)

    t_rem = gv("toner_remain_1")
    t_max = gv("toner_max_1", 100)
    try:
        tr = int(t_rem) if t_rem is not None else 0
        tm = int(t_max) if t_max is not None else 100
        toner_pct = round(tr / tm * 100) if tm > 0 and 0 <= tr <= tm else None
    except:
        toner_pct = None

    oids_rich = {}
    for key, oid in active_oids.items():
        cat_info = OID_CATALOG.get(key, (oid, "unknown", "unknown", key))
        val = current_vals.get(key)
        oids_rich[key] = {
            "oid":         oid,
            "type":        cat_info[1] if len(cat_info) > 1 else "unknown",
            "category":    cat_info[2] if len(cat_info) > 2 else "unknown",
            "description": cat_info[3] if len(cat_info) > 3 else key,
            "unit":        _oid_unit(cat_info[2], key),
            "active":      True,
            "last_value":  str(val) if val is not None else None,
        }

    with _profiles_lock:
        old_profile = _load_oid_profiles().get(ip, {})
    for key, old_oid_data in old_profile.get("oids", {}).items():
        if key not in oids_rich:
            oids_rich[key] = {**old_oid_data, "active": False, "last_value": None}

    # ═══ تشخیص نوع دستگاه (رنگی/تک‌رنگ/دماسنج) از روی OIDهای فعال ═══
    device_type = classify_from_scan(active_oids, current_vals, brand)
    with store.printers_lock:
        for p in store.PRINTERS:
            if p["ip"] == ip:
                p["device_type"] = device_type
                store.save_printers(store.PRINTERS)
                break

    profile = {
        "ip":           ip,
        "brand":        brand,
        "device_type":  device_type,
        "scanned_at":   datetime.now().isoformat(),
        "scan_ms":      elapsed,
        "oid_total":    len(OID_CATALOG),
        "oid_active":   len(active_oids),
        "oid_inactive": len([k for k, v in oids_rich.items() if not v.get("active", False)]),
        "oid_rejected": len(rejected_oids),
        "oids":         oids_rich,
        "current_vals": {k: str(v) for k, v in current_vals.items()},
        "rejected_oids": rejected_oids,
        "summary": {
            "model":       model,
            "serial":      serial,
            "brand":       brand,
            "total_pages": int(total),
            "toner_pct":   toner_pct,
            "device_type": device_type,
        },
    }

    with _profiles_lock:
        profiles = _load_oid_profiles()
        profiles[ip] = profile
        _save_oid_profiles(profiles)

    log.info(f"  [scan] {ip} ✓ {brand.upper()} | {model} | pages={total:,} | "
             f"type={device_type} | {len(active_oids)}/{len(OID_CATALOG)} OID | {elapsed}ms")
    return profile



def startup_scan_all(printers: list, force: bool = False) -> dict:
    try:
        with open(VALIDATION_LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"═══ OID Validation Log — {datetime.now().isoformat()} ═══\n\n")
    except:
        pass

    log.info(f"═══ Startup OID Scan — {len(printers)} پرینتر ═══")
    results = {}

    def scan_one(p):
        ip        = p["ip"]
        community = p.get("community", "public")
        try:
            profile = scan_printer_oids(ip, community, force=force)
            results[ip] = profile
            if not p.get("brand") and profile and profile.get("brand") != "unknown":
                p["brand"] = profile["brand"]
        except Exception as e:
            log.error(f"  [scan] {ip} خطا: {e}")
            log_validation_error(ip, "scan_exception", str(e))
            results[ip] = None

    threads = [threading.Thread(target=scan_one, args=(p,), daemon=True) for p in printers]
    for t in threads: t.start()
    for t in threads: t.join(timeout=30)

    ok     = sum(1 for v in results.values() if v)
    failed = sum(1 for v in results.values() if v is None)
    log.info(f"═══ Scan تمام شد: {ok}/{len(printers)} موفق، {failed} ناموفق ═══")
    return results


def weekly_scan_loop(get_printers_fn):
    """اسکن هفتگی دوره‌ای OIDها"""
    time.sleep(WEEK)
    while True:
        log.info("═══ اسکن دوره‌ای هفتگی OIDها ═══")
        startup_scan_all(get_printers_fn(), force=True)
        time.sleep(WEEK)