"""
تشخیص نوع دستگاه با walk روی جدول مواد مصرفی (prtMarkerSuppliesTable)
با ثبت خطاها در oid_validation_errors.txt
"""

import logging
from datetime import datetime
from config.settings import VALIDATION_LOG_FILE
from core.snmp.protocol import snmp_get_with_fallback

log = logging.getLogger("PrinterMonitor")


def _log_error(ip: str, error_type: str, details: str):
    """ثبت خطا در فایل validation log"""
    try:
        timestamp = datetime.now().isoformat()
        with open(VALIDATION_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] IP: {ip} | Type: device_classifier_{error_type}\n")
            f.write(f"  Details: {details}\n\n")
    except Exception as e:
        log.exception("خطا در نوشتن device_classifier log: %s", e)


def _walk_supplies(ip, community, max_index=10, timeout=2.0):
    """
    خواندن جدول prtMarkerSuppliesTable
    بازگشت: لیستی از دیکشنری‌های {"name": ..., "type": ..., "max": ..., "remaining": ..., "percent": ...}
    """
    supplies = []
    for idx in range(1, max_index + 1):
        try:
            name = snmp_get_with_fallback(ip, f"1.3.6.1.2.1.43.11.1.1.6.1.{idx}", community, timeout=timeout)
            if name is None:
                # دو None متوالی = پایان جدول
                if idx > 1:
                    prev_name = snmp_get_with_fallback(ip, f"1.3.6.1.2.1.43.11.1.1.6.1.{idx-1}", community, timeout=timeout)
                    if prev_name is None:
                        break
                continue
            
            stype = snmp_get_with_fallback(ip, f"1.3.6.1.2.1.43.11.1.1.5.1.{idx}", community, timeout=timeout)
            max_val = snmp_get_with_fallback(ip, f"1.3.6.1.2.1.43.11.1.1.8.1.{idx}", community, timeout=timeout)
            remaining = snmp_get_with_fallback(ip, f"1.3.6.1.2.1.43.11.1.1.9.1.{idx}", community, timeout=timeout)
            
            name_str = str(name).strip() if name else f"Supply {idx}"
            
            # تبدیل امن به int
            try:
                stype_int = int(stype) if stype is not None else 0
            except (ValueError, TypeError):
                stype_int = 0
                _log_error(ip, "type_conversion", f"Supply {idx}: type={stype} قابل تبدیل به int نیست")
            
            try:
                max_int = int(max_val) if max_val is not None and str(max_val).lstrip('-').isdigit() else -2
            except (ValueError, TypeError):
                max_int = -2
                _log_error(ip, "max_conversion", f"Supply {idx}: max={max_val} قابل تبدیل به int نیست")
            
            try:
                rem_int = int(remaining) if remaining is not None and str(remaining).lstrip('-').isdigit() else -2
            except (ValueError, TypeError):
                rem_int = -2
                _log_error(ip, "remaining_conversion", f"Supply {idx}: remaining={remaining} قابل تبدیل به int نیست")
            
            # محاسبه درصد
            percent = None
            if max_int > 0 and rem_int >= 0:
                percent = round(rem_int / max_int * 100)
            
            supplies.append({
                "index": idx,
                "name": name_str,
                "type": stype_int,
                "max": max_int,
                "remaining": rem_int,
                "percent": percent
            })
            
        except Exception as e:
            _log_error(ip, "walk_exception", f"Supply {idx}: {e}")
            continue
    
    return supplies


def classify_device_detailed(ip, community, timeout=2.0):
    """
    تشخیص دقیق نوع دستگاه با walk روی جدول مواد مصرفی.
    برمی‌گرداند: ("color"|"mono"|"sensor"|"unknown", supplies_list)
    """
    try:
        # 1. تشخیص سنسور
        sys_desc = snmp_get_with_fallback(ip, "1.3.6.1.2.1.1.1.0", community, timeout=2.0)
        if sys_desc and isinstance(sys_desc, str) and "ECS100G" in sys_desc.upper():
            return "sensor", []
        for sensor_oid in (
            "1.3.6.1.4.1.47206.1.0",
            "1.3.6.1.4.1.47206.110.1.2.0",
            "1.3.6.1.4.1.47206.111.1.2.0",
        ):
            if snmp_get_with_fallback(ip, sensor_oid, community, timeout=timeout) is not None:
                return "sensor", []
        
        # 2. تلاش برای walk روی جدول مواد مصرفی
        supplies = _walk_supplies(ip, community, max_index=10, timeout=timeout)
        
        if not supplies:
            _log_error(ip, "no_supplies", "هیچ ماده مصرفی یافت نشد (دستگاه ممکن است پاسخ ندهد)")
            return "unknown", []
        
        # 3. فقط تونرها را فیلتر کن (type=3 در prtMarkerSuppliesType)
        toners = [s for s in supplies if s["type"] == 3]
        
        if not toners:
            # اگر type=3 نبود، همه supplies را به عنوان تونر در نظر بگیر
            toners = supplies
            _log_error(ip, "no_toner_type", f"هیچ supply با type=3 یافت نشد. {len(supplies)} supply دیگر بررسی شد")
        
        # 4. تشخیص رنگ بر اساس نام تونرها
        color_names = {"cyan", "magenta", "yellow"}
        has_color = False
        
        for t in toners:
            name_lower = t["name"].lower()
            for c in color_names:
                if c in name_lower:
                    has_color = True
                    break
        
        if has_color:
            log.info(f"  [classify] {ip}: رنگی تشخیص داده شد ({len(toners)} تونر)")
            return "color", supplies
        else:
            log.info(f"  [classify] {ip}: تک‌رنگ تشخیص داده شد ({len(toners)} تونر)")
            return "mono", supplies
            
    except Exception as e:
        _log_error(ip, "exception", str(e))
        return "unknown", []


# ─── تابع اصلی که در scanner.py فراخوانی می‌شود ───
def classify_from_scan(active_oids: dict, current_vals: dict, brand: str) -> str:
    """
    تشخیص نوع دستگاه با استفاده از OIDهای اسکن‌شده (روش سریع، بدون SNMP اضافی)
    """
    # 1. سنسور
    if brand == "sensor":
        return "sensor"
    
    sys_desc = current_vals.get("sys_descr")
    if sys_desc and isinstance(sys_desc, str) and "ECS100G" in sys_desc.upper():
        return "sensor"
    if any(key in active_oids for key in ("sensor_model", "sensor_temp1", "sensor_hum1")):
        return "sensor"

    # 2. Toshiba - بررسی مقدار OIDهای وضعیت تونر
    if brand == "toshiba":
        cyan_st = current_vals.get("tsh_cyan_st")
        mag_st  = current_vals.get("tsh_magenta_st")
        yel_st  = current_vals.get("tsh_yellow_st")
        
        for val in (cyan_st, mag_st, yel_st):
            if val is not None:
                try:
                    if int(val) > 0:
                        return "color"
                except (ValueError, TypeError):
                    pass
        
        if "tsh_color" in active_oids:
            try:
                color_val = int(current_vals.get("tsh_color", 0))
                if color_val > 0:
                    return "color"
            except (ValueError, TypeError):
                pass
        
        return "mono"

    # 3. Canon
    if brand == "canon":
        if "toner_name_2" in active_oids:
            t2_name = str(current_vals.get("toner_name_2", "")).lower()
            if any(c in t2_name for c in ("cyan", "magenta", "yellow")):
                return "color"
            else:
                return "mono"
        if "canon_lbp_color" in active_oids:
            return "color"
        return "mono"

    # 4. HP
    if brand == "hp":
        if "toner_name_2" in active_oids:
            t2_name = str(current_vals.get("toner_name_2", "")).lower()
            if any(c in t2_name for c in ("cyan", "magenta", "yellow")):
                return "color"
            if any(c in t2_name for c in ("drum", "feeder", "kit", "fuser")):
                if "toner_name_3" in active_oids:
                    t3_name = str(current_vals.get("toner_name_3", "")).lower()
                    if any(c in t3_name for c in ("cyan", "magenta", "yellow")):
                        return "color"
                return "mono"
        if any(k in active_oids for k in ("hp_print_color", "hp_copy_color")):
            return "color"
        return "mono"

    # 5. Brother
    if brand == "brother":
        if "toner_name_2" in active_oids:
            t2_name = str(current_vals.get("toner_name_2", "")).lower()
            if any(c in t2_name for c in ("cyan", "magenta", "yellow")):
                return "color"
            else:
                return "mono"
        if "bro_color" in active_oids:
            try:
                color_val = int(current_vals.get("bro_color", 0))
                if color_val > 0:
                    return "color"
            except (ValueError, TypeError):
                pass
        return "mono"

    return "mono"