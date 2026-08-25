# core/enhanced_collector.py

"""
جمع‌آوری پیشرفته داده‌ها با استفاده از روش‌های test_toner.py:
- Walk کامل جدول prtMarkerSuppliesTable
- Walk جدول prtInputTable (سینی‌ها)
- OIDهای جایگزین برای HP, Canon, Brother
- تشخیص خودکار نسخه SNMP
- ذخیره اطلاعات دقیق تونر در دیتابیس
- ثبت لاگ در toner_report.txt
"""

import time
import logging
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

from config.settings import VALIDATION_LOG_FILE, TONER_ALERT_THRESHOLDS
from core.snmp.protocol import snmp_get_with_fallback, _detect_snmp_version
from core.snmp.oid_map import OIDS
from core import store
from core.collectors.base import _counters_event, apply_toner_override, _bootstrap_yield_from_history, get_pages_since_last_reset, attribute_paper_size
from core.database import add_event

log = logging.getLogger("PrinterMonitor")

import json
import os

# ─── تنظیمات ─────────────────────────────────────────────────────
ENHANCED_TIMEOUT = 3.0   # timeout برای هر OID
ENHANCED_MAX_SUPPLIES = 15  # حداکثر تعداد مواد مصرفی برای walk

# OIDهای جایگزین برای HP
# نکته: نام کارتریج روی دستگاه دقیقاً همین رشته‌هاست (مثلاً «Black Cartridge CF287X»)؛
# فقط CF287A بودنِ نقشه باعث می‌شد مدل‌های X/Y — و کل E52645 (CF289*) — هیچ‌وقت match نشوند.
_HP_NPCL_PCT_OID = "1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.2.4.1.2.1.5.5.1.1"
HP_ALTERNATE_OIDS = {
    "CE505A": [_HP_NPCL_PCT_OID],
    "CF283A": [_HP_NPCL_PCT_OID],
    "CC388A": [_HP_NPCL_PCT_OID],
    # M506 / M527 (87A/87X/87Y)
    "CF287A": [_HP_NPCL_PCT_OID],
    "CF287X": [_HP_NPCL_PCT_OID],
    "CF287Y": [_HP_NPCL_PCT_OID],
    # E52645 / Managed E52645 (89A/89X/89Y)
    "CF289A": [_HP_NPCL_PCT_OID],
    "CF289X": [_HP_NPCL_PCT_OID],
    "CF289Y": [_HP_NPCL_PCT_OID],
    # M604/M605/M606 (81A/81X)
    "CF281A": [_HP_NPCL_PCT_OID],
    "CF281X": [_HP_NPCL_PCT_OID],
    "W9008MC": [_HP_NPCL_PCT_OID],
}

# OIDهای جایگزین برای Canon
CANON_ALTERNATE_OIDS = [
    "1.3.6.1.4.1.1602.1.2.1.1.1.1.1",
    "1.3.6.1.4.1.1602.1.2.1.1.1.2.1",
]

# OID تونر Brother
BROTHER_TONER_OID = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.1"
BROTHER_DRUM_OID = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.2"


# ─── الگوهای تشخیص رنگ تونر (دقیق‌تر) ─────────────────────────────
import re as _re
_TONER_COLOR_PATTERNS = {
    "black": _re.compile(r'\b(black|bk)\b', _re.IGNORECASE),
    "cyan": _re.compile(r'\b(cyan)\b', _re.IGNORECASE),
    "magenta": _re.compile(r'\b(magenta|mgt)\b', _re.IGNORECASE),
    "yellow": _re.compile(r'\b(yellow)\b', _re.IGNORECASE),
}

def _detect_toner_color(name: str) -> Optional[str]:
    """✅ باگ #8: تشخیص دقیق رنگ تونر با regex (جلوگیری از match اشتباه)"""
    if not name:
        return None
    for color, pattern in _TONER_COLOR_PATTERNS.items():
        if pattern.search(name):
            return color
    return None


# ─── برند خودشفابخش از هویت واقعی دستگاه (sysDescr/sysObjectID) ─────────────
# مورد واقعی: روی 172.16.25.43 در printers.json برند «canon» مانده بود ولی بعد از
# جابه‌جایی DHCP یک Brother MFC-8510DN روی همان IP نشست؛ نتیجه: منطق تونر Canon
# (کد گسسته + اسکریپ Remote UI) بیهوده روی Brother اجرا می‌شد و هیچ تونری نمایش
# داده نمی‌شد. با این لایه، اگر هویت SNMP دستگاه با برند ذخیره‌شده ناسازگار باشد،
# برند به‌صورت خودکار اصلاح و در printers.json ماندگار می‌شود (+ رویداد لاگ).
_SYS_VENDOR_HINTS = (
    ("toshiba", "toshiba"),
    ("brother", "brother"),
    ("canon", "canon"),
    ("hewlett", "hp"),
    ("jetdirect", "hp"),
    ("hp ", "hp"),
    ("laserjet", "hp"),
    ("officejet", "hp"),
    ("pagewide", "hp"),
    ("kyocera", "kyocera"),
    ("ricoh", "ricoh"),
    ("xerox", "xerox"),
    ("lexmark", "lexmark"),
    ("samsung", "samsung"),
    ("epson", "epson"),
)

_SYS_OBJECTID_HINTS = (
    ("1.3.6.1.4.1.1129", "toshiba"),
    ("1.3.6.1.4.1.1602", "canon"),
    ("1.3.6.1.4.1.2435", "brother"),
    ("1.3.6.1.4.1.11", "hp"),
)


def _vendor_hint_from_identity(sys_desc: str = "", sys_object_id: str = "") -> Optional[str]:
    """از sysDescr (و در صورت خالی‌بودن، sysObjectID) برند واقعی دستگاه را حدس می‌زند."""
    ls = (sys_desc or "").lower()
    for token, hint in _SYS_VENDOR_HINTS:
        if token in ls:
            return hint
    oid = sys_object_id or ""
    for prefix, hint in _SYS_OBJECTID_HINTS:
        if prefix in oid:
            return hint
    return None


def _toner_level_missing(toners: Dict) -> bool:
    """آیا برای هیچ «تونرِ» واقعی عدد داریم؟ (درام/OPC شمرده نمی‌شوند)

    نکته: در rum Brother ممکن است درصد درام سالم باشد ولی تونر unknown؛
    شرط ساده‌ی «هیچ raw_level نیست» در آن حالت به‌غلط مانع fallback وب می‌شود.
    """
    toner_keys = [k for k in toners.keys() if k not in ("drum", "opc")]
    if not toner_keys:
        return True
    return not any(toners[k].get("raw_level") is not None for k in toner_keys)


def _merge_scraped_toners(toners: Dict, scraped: Dict, source: str) -> int:
    """ادغام نتیجه‌ی اسکریپ وب در دیکشنری toners (همان الگوی Canon/TopAccess).
    کلیدهای «_» فراداده‌اند (مثل «_used» در HP) و رد می‌شوند.
    خروجی: تعداد تونرهایی که مقدارشان تازه‌شده است."""
    merged = 0
    for color_key, pct in scraped.items():
        if color_key.startswith("_") or not isinstance(pct, (int, float)) or isinstance(pct, bool):
            continue
        pct = int(pct)
        t = toners.get(color_key)
        if not t:
            toners[color_key] = {
                "level": pct, "raw_level": pct, "status": "unknown",
                "name": f"{color_key} toner", "remaining": -1, "max": -1,
                "source": source,
            }
            merged += 1
            continue
        if t.get("raw_level") is None:
            t["raw_level"] = pct
        if t.get("level") is None:
            t["level"] = pct
            t["source"] = source
            if pct == 0:
                t["status"] = "empty"
            elif pct <= 10:
                t["status"] = "critical"
            elif pct <= 25:
                t["status"] = "low"
            else:
                t["status"] = "ok"
            merged += 1
    return merged

# ─── توابع کمکی ───────────────────────────────────────────────────
def _log_to_toner_report(content: str):
    """اضافه کردن خط به فایل toner_report.txt"""
    try:
        with open("toner_report.txt", "a", encoding="utf-8") as f:
            f.write(content + "\n")
    except Exception as e:
        log.error(f"خطا در نوشتن toner_report: {e}")


def _log_validation_error(ip: str, error_type: str, details: str):
    """ثبت خطا در فایل validation log"""
    try:
        timestamp = datetime.now().isoformat()
        with open(VALIDATION_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] IP: {ip} | Type: enhanced_{error_type}\n")
            f.write(f"  Details: {details}\n\n")
    except Exception as e:
        log.error(f"خطا در نوشتن validation log: {e}")


def _save_counters_to_db(ip: str, total: int, color, bw, black_level, 
                          prev_override: dict, device_type: str,
                          toners: dict, supplies: list, alerts: list):
    """✅ باگ #12: ذخیره امن در دیتابیس با context manager"""
    try:
        from core.database import db_connection
        with db_connection(commit=True) as conn:
            conn.execute('''
                INSERT OR REPLACE INTO printer_counters 
                (ip, print_total, full_color, black_white, toner_level, 
                 manual_override, override_color, override_base_level, 
                 override_start_total, override_start_toner, yield_per_page, 
                 updated_at, device_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                ip,
                total,
                color if color else 0,
                bw,
                black_level,
                prev_override.get('manual_override', 0),
                prev_override.get('override_color'),
                prev_override.get('override_base_level'),
                prev_override.get('override_start_total'),
                prev_override.get('override_start_toner'),
                prev_override.get('yield_per_page', 2000),
                datetime.now().isoformat(),
                device_type
            ))
            
            # ذخیره اطلاعات تونرها
            toner_data = {
                "toners": {
                    k: {"level": v["level"], "status": v["status"], "name": v.get("name", "")}
                    for k, v in toners.items()
                },
                "supplies": [
                    {"name": s["name"], "percent": s["percent"], "status": s["status"], "type": s["type_name"]}
                    for s in supplies if s["percent"] is not None
                ]
            }
            # 🔥 اصلاح: ذخیره toner_data در alert_codes باعث آلودگی دیتابیس می‌شد
            # در عوض فقط کدهای هشدار در alert_codes و لیست تونرها در last_alert_codes ذخیره می‌شود
            alert_codes_json = json.dumps([a["code"] for a in alerts], ensure_ascii=False)
            toner_data_json = json.dumps(toner_data, ensure_ascii=False)
            
            conn.execute('''
                UPDATE printer_counters SET alert_codes = ?, last_alert_codes = ? WHERE ip = ?
            ''', (alert_codes_json, toner_data_json, ip))
    except Exception as e:
        log.error(f"خطا در ذخیره enhanced data در دیتابیس: {e}")


def detect_snmp_version(ip: str, community: str = "public", timeout: float = 2.0) -> Optional[int]:
    """
    تشخیص نسخه SNMP با تکیه بر cache و negative-cache ماژول protocol.
    بازگشت: 1, 2, یا None
    """
    return _detect_snmp_version(ip, community, probe_timeout=timeout)


def try_alternative_oids(ip: str, community: str, brand: str, cartridge_model: str = "", 
                         snmp_version: int = None, timeout: float = 3.0) -> Optional[int]:
    """تلاش با OIDهای جایگزین برای دریافت سطح تونر"""
    
    if brand == "hp":
        # اول OIDهای مخصوص مدل کارتریج (match بدون حساسیت به حروف)
        cart_upper = (cartridge_model or "").upper()
        for model_key, oid_list in HP_ALTERNATE_OIDS.items():
            if model_key in cart_upper:
                for oid in oid_list:
                    val = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=timeout)
                    if val is not None:
                        try:
                            int_val = int(val)
                            if 0 <= int_val <= 100:
                                return int_val
                        except Exception:
                            pass
        
        # OIDهای عمومی HP
        general_oids = [
            "1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.2.1.5.5.1.1",
            "1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.2.4.1.2.1.5.5.1.1",
            "1.3.6.1.4.1.11.2.3.9.1.1.7.0",
        ]
        for oid in general_oids:
            val = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=timeout)
            if val is not None:
                try:
                    int_val = int(val)
                    if 0 <= int_val <= 100:
                        return int_val
                except Exception:
                    pass
    
    elif brand == "canon":
        for oid in CANON_ALTERNATE_OIDS:
            val = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=timeout)
            if val is not None:
                try:
                    int_val = int(val)
                    if 0 <= int_val <= 100:
                        return int_val
                except Exception:
                    pass
    
    return None


def walk_supplies_table(ip: str, community: str, brand: str = "unknown", 
                        snmp_version: int = None, timeout: float = 2.0, current_total: int = None) -> List[Dict]:
    """
    Walk کامل روی جدول prtMarkerSuppliesTable
    بازگشت: لیستی از دیکشنری‌های حاوی اطلاعات کارتریج‌ها
    """
    supplies = []
    
    # برای Brother، اول روش اختصاصی امتحان می‌شود.
    # اگر سطح تونر از OID اختصاصی به دست نیاید (مثل بعضی مدل‌های NC-8300h)،
    # به روش استاندارد prtMarkerSuppliesTable fallback می‌کنیم.
    if brand == "brother":
        toner_level = snmp_get_with_fallback(ip, BROTHER_TONER_OID, community, 
                                              version=snmp_version, timeout=timeout)
        if toner_level is not None:
            try:
                level = int(toner_level)
                if 0 <= level <= 100:
                    supplies.append({
                        "index": 1,
                        "name": "Black Toner",
                        "model": "Toner Cartridge",
                        "type": 3,
                        "type_name": "toner",
                        "unit": "percent",
                        "max": 100,
                        "remaining": level,
                        "percent": level,
                        "status": "critical" if level <= 10 else "low" if level <= 25 else "ok",
                    })
            except Exception:
                pass
        
        # درام هم بخوانیم
        drum_level = snmp_get_with_fallback(ip, BROTHER_DRUM_OID, community,
                                             version=snmp_version, timeout=timeout)
        if drum_level is not None:
            try:
                level = int(drum_level)
                if 0 <= level <= 100:
                    supplies.append({
                        "index": 2,
                        "name": "Drum Unit",
                        "model": "Drum Unit",
                        "type": 7,  # OPC type
                        "type_name": "opc",
                        "unit": "percent",
                        "max": 100,
                        "remaining": level,
                        "percent": level,
                        "status": "critical" if level <= 10 else "low" if level <= 25 else "ok",
                    })
            except Exception:
                pass

        # اگر تونر اختصاصی با موفقیت خوانده شد، همان را برگردان.
        # در غیر این صورت، به روش عمومی prtMarkerSuppliesTable ادامه می‌دهیم.
        if any(s.get("type_name") == "toner" for s in supplies):
            return supplies
    
    # روش استاندارد برای سایر برندها
    for idx in range(1, ENHANCED_MAX_SUPPLIES + 1):
        try:
            name_oid = f"1.3.6.1.2.1.43.11.1.1.6.1.{idx}"
            name = snmp_get_with_fallback(ip, name_oid, community, 
                                          version=snmp_version, timeout=timeout)
            
            if name is None:
                if idx >= 5 and brand in ["canon", "hp", "brother"]:
                    break
                continue
            
            name_str = str(name).strip()
            
            type_oid = f"1.3.6.1.2.1.43.11.1.1.5.1.{idx}"
            stype = snmp_get_with_fallback(ip, type_oid, community,
                                           version=snmp_version, timeout=timeout)
            
            unit_oid = f"1.3.6.1.2.1.43.11.1.1.7.1.{idx}"
            unit_val = snmp_get_with_fallback(ip, unit_oid, community,
                                              version=snmp_version, timeout=timeout)

            max_oid = f"1.3.6.1.2.1.43.11.1.1.8.1.{idx}"
            max_val = snmp_get_with_fallback(ip, max_oid, community,
                                             version=snmp_version, timeout=timeout)
            
            rem_oid = f"1.3.6.1.2.1.43.11.1.1.9.1.{idx}"
            rem_val = snmp_get_with_fallback(ip, rem_oid, community,
                                             version=snmp_version, timeout=timeout)
            
            stype_int = 0
            if stype is not None and str(stype).lstrip('-').isdigit():
                stype_int = int(stype)
            
            type_names = {
                1: "other", 2: "unknown", 3: "toner", 4: "wasteToner",
                5: "ink", 6: "wasteInk", 7: "OPC", 8: "developer",
                9: "fuser", 10: "cleaner", 11: "transfer", 12: "staples",
                21: "cartridge"
            }
            type_name = type_names.get(stype_int, f"type_{stype_int}")
            # Brother برخی consumableها را با typeهای غیر دقیق گزارش می‌کند.
            # برای نمایش صحیح درام و تونر، از نام مصرفی هم کمک می‌گیریم.
            if brand == "brother":
                lowered_name = name_str.lower()
                if "drum" in lowered_name:
                    type_name = "drum"
                elif "toner" in lowered_name:
                    type_name = "toner"

            percent = None
            max_int = -2
            rem_int = -2
            unit_int = None
            percent_source = None
            unit_names = {
                1: "other", 2: "unknown", 3: "tenThousandthsOfInches",
                4: "micrometers", 7: "impressions", 8: "sheets",
                16: "feet", 17: "meters", 18: "items", 19: "percent",
            }
            
            try:
                if unit_val is not None and str(unit_val).lstrip('-').isdigit():
                    unit_int = int(unit_val)
                if max_val is not None and str(max_val).lstrip('-').isdigit():
                    max_int = int(max_val)
                if rem_val is not None and str(rem_val).lstrip('-').isdigit():
                    rem_int = int(rem_val)
                
                # ✅ باگ #20: فقط وقتی max_int معتبره درصد حساب کن
                # حذف شرط elif که فرض می‌کرد rem_int بین 0-100 = درصد
                if max_int > 0 and rem_int >= 0:
                    percent = round(rem_int / max_int * 100)
                elif unit_int == 19 and 0 <= rem_int <= 100:
                    # ✅ فیکس HP FutureSmart (M527 / E52645): با واحد «percent (19)»
                    # خودِ Level همان درصد است، حتی اگر MaxCapacity خوانده نشود یا
                    # دستگاه آن را -3 (ظرفیت نامشخص) برگرداند. بدون این قانون، این
                    # دو مدل دقیقاً همان «تونر دریافت نمی‌شود» کاربر را تجربه می‌کردند.
                    percent = rem_int
                    percent_source = "unit_percent"
            except (ValueError, TypeError) as e:
                log.warning(f"Supply conversion error for {ip} idx={idx}: max={max_val}, rem={rem_val}: {e}")
            
            # اگر درصد نداریم، OIDهای جایگزین را امتحان کن
            _canon_discrete = (brand == "canon" and max_int <= 0 and rem_int in (0, 5, 7))
            if percent is None and brand in ["hp", "canon"] and not _canon_discrete:
                alt_percent = try_alternative_oids(ip, community, brand, name_str, snmp_version, timeout)
                if alt_percent is not None:
                    percent = alt_percent
                    percent_source = "alternative_oid"
                    rem_int = alt_percent
                    max_int = 100

            # وضعیت
            status = "N/A"
            supply_present = False
            if percent is not None and percent_source != "canon_status_code":
                if percent == 0:
                    status = "empty"
                elif percent <= 10:
                    status = "critical"
                elif percent <= 25:
                    status = "low"
                else:
                    status = "ok"
            elif brand == "canon" and percent is None and max_int <= 0 and rem_int in (0, 5, 7):
                # ✅ فیکس Canon i-SENSYS (مثل LBP233dw): این خانواده به‌جای درصد
                # واقعی، کد وضعیت گسسته برمی‌گرداند: 0=Empty، 5=Low، 7=OK.
                # عدد نمایشی فقط «برآورد نمایشی» است و هرگز نباید به‌عنوان
                # raw_level در یادگیری yield مصرف شود (percent_source مشخص است).
                status = {0: "empty", 5: "low", 7: "ok"}[rem_int]
                percent = {0: 0, 5: 15, 7: 70}[rem_int]
                percent_source = "canon_status_code"
                supply_present = rem_int != 0
            elif rem_int == -3 or max_int == -3:
                # ✅ RFC 3805: -3 یعنی «دستگاه می‌داند مقداری مصرفی/فضا هست» ولی
                # عدد ندارد. وضعیت unknown+present است، نه «پشتیبانی نمی‌شود».
                status = "unknown"
                supply_present = True
            elif rem_int == -2:
                # ✅ باگ #15: وقتی max معتبر نداریم، هرگز rem را به‌عنوان درصد فرض نکن.
                # این حالت معمولاً یعنی سنسور/مقدار قابل‌اعتماد در دسترس نیست.
                status = "no_sensor" if name_str and name_str != "Unknown" else "not_supported"
                log.info(f"  [{ip}] Supply {idx} ({name_str}): no sensor data available")

            # فیلتر Unknownهای تکراری
            if name_str.startswith("Unknown"):
                unknown_count = sum(1 for s in supplies if s["name"].startswith("Unknown"))
                if unknown_count > 2:
                    continue
            
            unit_name = unit_names.get(unit_int, "unknown")
            # ✅ فیکس سطرهای فانتوم (کشف واقعی: Canon LBP2330K روی 172.16.0.43):
            # این دستگاه سطر اول را صحیح می‌دهد (Cartridge 324 II، level=10) ولی
            # سه سطر بعدی «نام خالی + بدون داده» دارند. قبل از این فیکس، آن سطرها
            # همین جدول را رد می‌کردند و چون نامشان خالی بود، هنگام ساخت toners همگی
            # به کلید «black» می‌خوردند و مقدار واقعی ۱۰٪ را با unknown بازنویسی
            # می‌کردند — نتیجه: «تونر خوانده نمی‌شود» با اینکه SNMP سالم بود!
            if not name_str and percent is None:
                log.info(f"  [{ip}] Supply {idx}: رد سطر فانتوم (نام خالی و بدون داده قابل‌استفاده)")
                continue
            # اگر max بزرگ‌تر از ۱۰۰ باشد و unit درصد نباشد، معمولاً ظرفیت اعلام‌شده
            # توسط خود دستگاه است (صفحه/Impressions/Items). این مقدار به Yield Engine
            # داده می‌شود تا default=2000 فقط آخرین fallback باشد.
            device_capacity_pages = None
            if max_int > 100 and unit_int != 19 and type_name in ("toner", "cartridge", "drum", "OPC", "opc"):
                device_capacity_pages = max_int

            supplies.append({
                "index": idx,
                "name": name_str,
                "model": name_str,
                "type": stype_int,
                "type_name": type_name,
                "unit": unit_name,
                "unit_code": unit_int,
                "max": max_int if max_int != -2 else (100 if percent is not None else "N/A"),
                "remaining": rem_int if rem_int >= 0 else ("N/A" if rem_int == -2 else "unsupported"),
                "percent": percent,
                "percent_source": percent_source,
                "supply_present": supply_present,
                "status": status,
                "device_capacity_pages": device_capacity_pages,
            })
            
        except Exception as e:
            if idx <= 3:
                _log_validation_error(ip, "walk_supplies_exception", f"idx={idx}: {e}")
    
    return supplies


def walk_input_trays(ip: str, community: str, snmp_version: int = None, timeout: float = 2.0) -> List[Dict]:
    """Walk روی جدول prtInputTable برای سینی‌ها"""
    trays = []
    
    for idx in range(1, 8):
        try:
            name_oid = f"1.3.6.1.2.1.43.8.2.1.13.1.{idx}"
            name = snmp_get_with_fallback(ip, name_oid, community, 
                                          version=snmp_version, timeout=timeout)
            
            cap_oid = f"1.3.6.1.2.1.43.8.2.1.9.1.{idx}"
            cap_val = snmp_get_with_fallback(ip, cap_oid, community,
                                             version=snmp_version, timeout=timeout)
            
            level_oid = f"1.3.6.1.2.1.43.8.2.1.10.1.{idx}"
            level_val = snmp_get_with_fallback(ip, level_oid, community,
                                               version=snmp_version, timeout=timeout)
            
            if name is None and cap_val is None and level_val is None:
                continue
            
            name_str = str(name).strip() if name else f"Tray {idx}"
            
            try:
                cap_int = int(cap_val) if cap_val is not None and str(cap_val).lstrip('-').isdigit() else 0
            except Exception:
                cap_int = 0
            
            try:
                if level_val is not None and str(level_val).lstrip('-').isdigit():
                    level_int = int(level_val)
                else:
                    level_int = -2
            except Exception:
                level_int = -2
            
            fill_percent = None
            status = "unknown"
            
            if level_int == -2:
                status = "no_sensor"
            elif level_int == -3:
                status = "not_supported"
            elif cap_int > 0 and level_int >= 0:
                fill_percent = round(level_int / cap_int * 100)
                if level_int == 0:
                    status = "empty"
                elif fill_percent <= 25:
                    status = "low"
                elif fill_percent <= 75:
                    status = "medium"
                else:
                    status = "ok"
            elif level_int >= 0 and level_int <= 100 and cap_int == 0:
                fill_percent = level_int
                status = "ok" if level_int > 25 else "low" if level_int > 10 else "critical"
            
            trays.append({
                "index": idx,
                "name": name_str,
                "capacity": cap_int,
                "level": level_int if level_int >= 0 else ("N/A" if level_int == -2 else "unsupported"),
                "fill_percent": fill_percent,
                "status": status,
            })
            
        except Exception as e:
            continue
    
    return trays


def _save_oid_profile(ip: str, community: str, snmp_version: int, model: str,
                      device_type: str, total: int, color: Optional[int], bw: int,
                      supplies: List[Dict], trays: List[Dict], scan_ms: int):
    """پروفایل OID را برای یک IP بسازد و در oid_profiles.json ذخیره کند."""
    try:
        candidates = {
            "sys_descr": "1.3.6.1.2.1.1.1.0",
            "sys_uptime": "1.3.6.1.2.1.1.3.0",
            "sys_hostname": "1.3.6.1.2.1.1.5.0",
        }
        oids = {}
        active = 0
        rejected = {}
        for key, oid in candidates.items():
            try:
                val = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=2.0)
                if val is not None:
                    oids[key] = {
                        "oid": oid,
                        "type": "int" if isinstance(val, int) else "str",
                        "category": "sys",
                        "description": key,
                        "unit": "str",
                        "active": True,
                        "last_value": str(val)
                    }
                    active += 1
                else:
                    oids[key] = {"oid": oid, "active": False}
                    rejected[key] = oid
            except Exception as e:
                rejected[key] = str(e)

        # counters and supplies/trays probing (indexes)
        counter_oids = {
            "print_total": "1.3.6.1.2.1.43.10.2.1.4.1.1",
            "print_color": "1.3.6.1.2.1.43.10.2.1.4.1.2",
            "print_mono": "1.3.6.1.2.1.43.10.2.1.4.1.3",
            "prt_marker_total": "1.3.6.1.4.1.1602.1.11.2.1.1.3.1",
        }
        for k, oid in counter_oids.items():
            try:
                val = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=2.0)
                if val is not None:
                    oids[k] = {"oid": oid, "type": "int", "category": "counter", "active": True, "last_value": str(val)}
                    active += 1
                else:
                    oids[k] = {"oid": oid, "active": False}
                    rejected[k] = oid
            except Exception as e:
                rejected[k] = str(e)

        for idx in range(1, ENHANCED_MAX_SUPPLIES + 1):
            name_oid = f"1.3.6.1.2.1.43.11.1.1.6.1.{idx}"
            rem_oid = f"1.3.6.1.2.1.43.11.1.1.9.1.{idx}"
            max_oid = f"1.3.6.1.2.1.43.11.1.1.8.1.{idx}"
            try:
                name = snmp_get_with_fallback(ip, name_oid, community, version=snmp_version, timeout=1.5)
                rem = snmp_get_with_fallback(ip, rem_oid, community, version=snmp_version, timeout=1.5)
                mx = snmp_get_with_fallback(ip, max_oid, community, version=snmp_version, timeout=1.5)
                if name is None and rem is None and mx is None:
                    continue
                key_name = f"toner_name_{idx}"
                oids[key_name] = {"oid": name_oid, "type": "str", "category": "identity", "active": bool(name), "last_value": str(name) if name is not None else None}
                oids[f"toner_remain_{idx}"] = {"oid": rem_oid, "type": "int", "category": "supply", "active": bool(rem is not None), "last_value": str(rem) if rem is not None else None}
                oids[f"toner_max_{idx}"] = {"oid": max_oid, "type": "int", "category": "supply", "active": bool(mx is not None), "last_value": str(mx) if mx is not None else None}
                active += 1
            except Exception as e:
                rejected[f"supply_{idx}"] = str(e)

        for idx in range(1, 9):
            t_name = f"1.3.6.1.2.1.43.8.2.1.13.1.{idx}"
            t_cap = f"1.3.6.1.2.1.43.8.2.1.9.1.{idx}"
            t_lvl = f"1.3.6.1.2.1.43.8.2.1.10.1.{idx}"
            try:
                nm = snmp_get_with_fallback(ip, t_name, community, version=snmp_version, timeout=1.5)
                cap = snmp_get_with_fallback(ip, t_cap, community, version=snmp_version, timeout=1.5)
                lvl = snmp_get_with_fallback(ip, t_lvl, community, version=snmp_version, timeout=1.5)
                if nm is None and cap is None and lvl is None:
                    continue
                key = f"tray{idx}_name"
                oids[key] = {"oid": t_name, "type": "str", "category": "identity", "active": bool(nm), "last_value": str(nm) if nm is not None else None}
                oids[f"tray{idx}_cap"] = {"oid": t_cap, "type": "int", "category": "tray", "active": bool(cap is not None), "last_value": str(cap) if cap is not None else None}
                oids[f"tray{idx}_level"] = {"oid": t_lvl, "type": "int", "category": "tray", "active": bool(lvl is not None), "last_value": str(lvl) if lvl is not None else None}
                active += 1
            except Exception as e:
                rejected[f"tray_{idx}"] = str(e)

        profile = {
            "ip": ip,
            "brand": (model or "unknown").split()[0].lower() if model else "unknown",
            "device_type": device_type or "unknown",
            "scanned_at": datetime.now().isoformat(),
            "scan_ms": scan_ms,
            "oid_total": sum(1 for _ in oids),
            "oid_active": active,
            "oid_inactive": sum(1 for v in oids.values() if not v.get("active")),
            "oid_rejected": len(rejected),
            "oids": oids,
            "current_vals": {k: v.get("last_value") for k, v in oids.items() if v.get("last_value") is not None},
            "rejected_oids": rejected,
            "summary": {
                "model": model or "Unknown",
                "serial": "N/A",
                "brand": (model or "unknown").split()[0].lower() if model else "unknown",
                "total_pages": total,
                "toner_pct": None,
                "device_type": device_type or "mono",
            }
        }

        serial_oids = [
            "1.3.6.1.2.1.43.5.1.1.17.1",
            "1.3.6.1.4.1.1602.1.2.1.4.0",
        ]
        for so in serial_oids:
            try:
                s = snmp_get_with_fallback(ip, so, community, version=snmp_version, timeout=1.5)
                if s:
                    profile["summary"]["serial"] = str(s)
                    break
            except Exception:
                continue

        try:
            from core.oid.scanner import _profiles_lock, _load_oid_profiles, _save_oid_profiles
            with _profiles_lock:
                data = _load_oid_profiles()
                data[ip] = profile
                _save_oid_profiles(data)
            log.info(f"OID profile saved for {ip}")
        except Exception as e:
            log.error(f"Error saving oid_profiles for {ip}: {e}")
    except Exception as e:
        log.debug(f"_save_oid_profile failed for {ip}: {e}")


def detect_printer_type_from_supplies(supplies: List[Dict]) -> str:
    """تشخیص نوع پرینتر از روی مواد مصرفی"""
    toners = [s for s in supplies if s.get("type") == 3 or s.get("type_name") == "toner"]
    if not toners:
        toners = supplies
    
    color_keywords = ["cyan", "magenta", "yellow", "سیان", "مژنتا", "color", "colour"]
    for t in toners:
        name_lower = t.get("name", "").lower()
        for c in color_keywords:
            if c in name_lower:
                return "color"
    
    return "mono"


def _canon_display_percent(model: str, supply_name: str, percent: Optional[int]) -> Optional[int]:
    """Canon panel values are rounded more coarsely than raw PRT-MIB supply values."""
    if percent is None:
        return None
    model_upper = (model or "").upper()
    name_upper = (supply_name or "").upper()
    if "CANON MF" in model_upper and "CARTRIDGE 137" in name_upper and 10 < percent < 20:
        return 20
    return percent



def _read_toshiba_value(ip: str, community: str, key: str, snmp_version: int, default=None, timeout: float = 2.0):
    oid = OIDS.get(key)
    if not oid:
        return default
    value = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=timeout)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default



def _collect_toshiba_job_data(ip: str, community: str, snmp_version: int, total: int, color: Optional[int]):
    """خواندن شمارنده‌های اختصاصی Toshiba برای جلوگیری از overcount و تکمیل UI/log.

    برای مدل‌های قدیمی‌تر (مثل e-STUDIO3540) اگر OID اصلی total در دسترس نباشد،
    از مجموع paper-size یا sub-counterها total پیشنهادی ساخته می‌شود.
    """
    a3_total = _read_toshiba_value(ip, community, "a3_total", snmp_version, default=None)
    a4_total = _read_toshiba_value(ip, community, "a4_total", snmp_version, default=None)
    prev = store._prev.get(ip) or {}

    # ✅ فیکس باگ «سایز کاغذ اشتباه در لاگ»: برچسب فقط وقتی گذاشته می‌شود که
    # دلتای شمارنده‌های کاغذ با دلتای شمارنده کل هم‌راستا باشد (هویت اثبات‌شده‌ی
    # a3_total + a4_total == print_total). در غیر این صورت snapshot کهنه است و
    # برچسب قبلی «مطمئن اما اشتباه» بود — حالا سایز نامشخص ثبت می‌شود.
    paper_size, paper_split, paper_size_reliable = attribute_paper_size(
        total=total,
        prev_total=prev.get("print_total"),
        a3_total=a3_total,
        prev_a3=prev.get("a3_total"),
        a4_total=a4_total,
        prev_a4=prev.get("a4_total"),
        ip=ip,
        a3_lagged=bool(prev.get("a3_lagged")),
        a4_lagged=bool(prev.get("a4_lagged")),
    )

    copy_fc = _read_toshiba_value(ip, community, "print_copy_fc", snmp_version, default=0)
    copy_bw = _read_toshiba_value(ip, community, "print_copy_bw", snmp_version, default=0)
    printer_fc = _read_toshiba_value(ip, community, "print_printer_fc", snmp_version, default=0)
    printer_bw = _read_toshiba_value(ip, community, "print_printer_bw", snmp_version, default=0)
    print_bw_raw = _read_toshiba_value(ip, community, "print_bw", snmp_version, default=None)
    twin = _read_toshiba_value(ip, community, "print_twin", snmp_version, default=0)
    fax = _read_toshiba_value(ip, community, "print_fax", snmp_version, default=None)
    list_count = _read_toshiba_value(ip, community, "print_list", snmp_version, default=None)

    # برای ثبت رویداد، به total خام تکیه می‌کنیم و twin را وارد محاسبه pages نمی‌کنیم
    # چون در برخی مدل‌های Toshiba دو فاز update باعث duplicate PRINT می‌شود.
    bw_for_event = print_bw_raw if isinstance(print_bw_raw, int) and print_bw_raw > 0 else (max(0, total - (color or 0)) if total is not None else None)
    scan_fc = _read_toshiba_value(ip, community, "scan_fc", snmp_version, default=None)
    scan_bw = _read_toshiba_value(ip, community, "scan_bw", snmp_version, default=None)
    scan_net_fc = _read_toshiba_value(ip, community, "scan_net_fc", snmp_version, default=None)
    scan_net_bw = _read_toshiba_value(ip, community, "scan_net_bw", snmp_version, default=None)

    paper_sizes = {}
    for key in ["a4", "a3", "a4r", "a5", "b4"]:
        total_key = _read_toshiba_value(ip, community, f"{key}_total", snmp_version, default=None)
        fc_key = _read_toshiba_value(ip, community, f"{key}_fc", snmp_version, default=None)
        bw_key = _read_toshiba_value(ip, community, f"{key}_bw", snmp_version, default=None)
        if total_key is not None or fc_key is not None or bw_key is not None:
            paper_sizes[key.upper()] = {
                "total": total_key or 0,
                "fc": fc_key or 0,
                "bw": bw_key or 0,
            }

    paper_total = None
    if a3_total is not None or a4_total is not None:
        paper_total = (a3_total or 0) + (a4_total or 0)

    sub_counter_total = None
    components = [
        (copy_fc or 0) + (copy_bw or 0),
        (printer_fc or 0) + (printer_bw or 0),
        fax or 0,
        list_count or 0,
    ]
    comp_sum = sum(v for v in components if isinstance(v, int) and v >= 0)
    if comp_sum > 0:
        sub_counter_total = comp_sum

    suggested_total = None
    total_candidates = [v for v in [paper_total, sub_counter_total, print_bw_raw] if isinstance(v, int) and v > 0]
    if total_candidates:
        suggested_total = max(total_candidates)

    return {
        "paper_size": paper_size,
        "paper_split": paper_split,
        "paper_size_reliable": paper_size_reliable,
        "a3_total": a3_total,
        "a4_total": a4_total,
        "black_white_for_event": bw_for_event,
        "suggested_total": suggested_total,
        "suggested_bw": print_bw_raw if isinstance(print_bw_raw, int) and print_bw_raw >= 0 else None,
        "counters": {
            "printer": (printer_fc + printer_bw) if (printer_fc is not None and printer_bw is not None) else total,
            "printer_fc": printer_fc,
            "printer_bw": printer_bw,
            "copy": (copy_fc + copy_bw) if (copy_fc is not None and copy_bw is not None) else None,
            "copy_fc": copy_fc,
            "copy_bw": copy_bw,
            "fax": fax,
            "list": list_count,
            "twin": twin,
            "scan_fc": scan_fc,
            "scan_bw": scan_bw,
            "scan_net_fc": scan_net_fc,
            "scan_net_bw": scan_net_bw,
        },
        "paper_sizes": paper_sizes,
    }


def collect_enhanced(printer: dict, save_to_db: bool = True) -> dict:
    """
    جمع‌آوری پیشرفته داده‌ها با استفاده از روش test_toner.py
    """
    ip = printer["ip"]
    name = printer["name"]
    nickname = printer.get("nickname", "")
    community = printer.get("community", "public")
    brand = printer.get("brand", "").lower()
    start_time = time.time()
    
    log.info(f"[ENHANCED] Pulling {name} ({ip})")
    _log_to_toner_report(f"\n{'='*80}")
    _log_to_toner_report(f"🖨  {name} ({ip}) | زمان: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # ─── تشخیص SNMP ───────────────────────────────────────────────
    snmp_version = detect_snmp_version(ip, community, timeout=2.0)
    if snmp_version is None:
        elapsed = int((time.time() - start_time) * 1000)
        _log_to_toner_report(f"   ❌ بدون پاسخ SNMP")
        return {
            "ip": ip, "name": name, "nickname": nickname, "brand": brand,
            "online": False, "last_poll": datetime.now().isoformat(), "poll_ms": elapsed,
            "error": "No SNMP response",
        }
    
    # ─── اطلاعات پایه ────────────────────────────────────────────
    sys_desc = snmp_get_with_fallback(ip, "1.3.6.1.2.1.1.1.0", community, version=snmp_version, timeout=2.0)
    sys_desc_str = str(sys_desc) if sys_desc else ""
    toshiba_vendor_model_probe = None

    # تشخیص سنسور؛ فقط به sysDescr متکی نیستیم چون بعضی ECS100Gها فقط OID اختصاصی را جواب می‌دهند.
    sensor_probe = None
    if brand == "sensor" or "ECS100G" in sys_desc_str.upper():
        sensor_probe = True
    else:
        for sensor_oid in (
            "1.3.6.1.4.1.47206.1.0",
            "1.3.6.1.4.1.47206.110.1.2.0",
            "1.3.6.1.4.1.47206.111.1.2.0",
        ):
            sensor_probe = snmp_get_with_fallback(ip, sensor_oid, community, version=snmp_version, timeout=1.5)
            if sensor_probe is not None:
                break
    if sensor_probe:
        from core.collectors.sensor import collect_sensor
        result = collect_sensor(ip, name, community, start_time)
        result["nickname"] = nickname
        return result

    # ─── برند خودشفابخش: اگر برند ذخیره‌شده با هویت واقعی دستگاه ناسازگار است ───
    # (ناشی از جابه‌جایی IP/DHCP یا ورود دستی اشتباه) آن را اصلاح و ماندگار کن.
    sys_object_id_str = ""
    vendor_hint = _vendor_hint_from_identity(sys_desc_str)
    if not vendor_hint and not sys_desc_str:
        _soi = snmp_get_with_fallback(ip, "1.3.6.1.2.1.1.2.0", community,
                                      version=snmp_version, timeout=2.0)
        if _soi:
            sys_object_id_str = str(_soi)
            vendor_hint = _vendor_hint_from_identity("", sys_object_id_str)
    if vendor_hint and brand != vendor_hint and brand not in ("sensor",):
        if brand:
            log.warning(
                "  [%s] برند ذخیره‌شده «%s» با دستگاه واقعی («%s») ناسازگار است (%s) — اصلاح خودکار",
                ip, brand, vendor_hint, (sys_desc_str or sys_object_id_str)[:60],
            )
            _log_to_toner_report(f"   ⚠️ اصلاح خودکار برند: {brand} → {vendor_hint}")
            try:
                add_event(ip, "BRAND_CORRECTED", {
                    "message": f"برند از «{brand}» به «{vendor_hint}» اصلاح شد (sysDescr دستگاه معتبرتر از مقدار ذخیره‌شده است)",
                    "severity": "warning",
                })
            except Exception:
                pass
        brand = vendor_hint
        printer["brand"] = brand
        try:
            with store.printers_lock:
                for _p in store.PRINTERS:
                    if _p.get("ip") == ip:
                        _p["brand"] = brand
                        store.save_printers(store.PRINTERS)
                        break
        except Exception as _exc:
            log.error("Persisting corrected brand for %s failed: %s", ip, _exc)

    # ─── شمارنده‌های اصلی ─────────────────────────────────────────
    # تلاش برای خواندن total از OIDهای مختلف
    total = 0
    total_oids = [
        "1.3.6.1.2.1.43.10.2.1.4.1.1",  # standard
        "1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.2.1.4",  # Toshiba
    ]
    for oid in total_oids:
        val = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=2.0)
        if val is not None:
            try:
                total = int(val)
                # ✅ باگ #12: validation مقدار total (جلوگیری از مقادیر منفی)
                if total < 0:
                    log.warning(f"Negative total ({total}) for {ip}, using 0")
                    total = 0
                elif total > 0:
                    break
            except (ValueError, TypeError) as e:
                log.warning(f"Total conversion error for {ip}: {val!r}: {e}")

    # ─── خواندن اطلاعات پیشرفته ───────────────────────────────────
    supplies = walk_supplies_table(ip, community, brand, snmp_version, timeout=ENHANCED_TIMEOUT, current_total=total)
    trays = walk_input_trays(ip, community, snmp_version, timeout=ENHANCED_TIMEOUT)
    
    # ─── تشخیص رنگ ───────────────────────────────────────────────
    # اگر supplies در یک poll خوانده نشود، نباید دستگاه رنگی را اشتباهاً mono فرض کنیم.
    prev_snapshot = store._prev.get(ip) or {}
    configured_type = (printer.get("device_type") or "").strip().lower()
    previous_type = (prev_snapshot.get("device_type") or "").strip().lower()
    detected_type = detect_printer_type_from_supplies(supplies) if supplies else "unknown"
    device_type = detected_type if detected_type != "unknown" else (configured_type or previous_type or "unknown")
    if device_type == "color":
        # تلاش برای خواندن شمارنده رنگی
        color = 0
        color_oids = [
            "1.3.6.1.2.1.43.10.2.1.4.1.2",  # standard color
            "1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.2.1.1",  # Toshiba color
        ]
        for oid in color_oids:
            val = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=2.0)
            if val is not None:
                try:
                    color = int(val)
                    if color > 0:
                        break
                except Exception:
                    pass
        # ✅ باگ #16: validation مقدار color (جلوگیری از color > total)
        if color > total and total > 0:
            log.warning(f"color ({color}) > total ({total}) for {ip}, correcting")
            color = None
            bw = total
        elif color > 0:
            bw = max(0, total - color)
        else:
            color = None
            bw = total
    elif device_type == "mono":
        color = None
        bw = total
    else:
        # نوع دستگاه نامطمئن است؛ تفکیک رنگ/سیاه‌وسفید را قطعی فرض نکن.
        color = None
        bw = None

    # برخی Toshibaها به sysDescr استاندارد پاسخ نمی‌دهند. برای این مدل‌ها،
    # با model OID اختصاصی هم probe می‌کنیم تا vendor-specific path فعال شود.
    if brand != "toshiba" and "toshiba" not in sys_desc_str.lower():
        try:
            toshiba_vendor_model_probe = snmp_get_with_fallback(
                ip,
                OIDS.get("model"),
                community,
                version=snmp_version,
                timeout=2.0,
            )
            if toshiba_vendor_model_probe:
                brand = "toshiba"
        except Exception:
            toshiba_vendor_model_probe = None

    is_toshiba = brand == "toshiba" or "toshiba" in sys_desc_str.lower() or bool(toshiba_vendor_model_probe)
    toshiba_data = None
    toshiba_vendor_total_failed = False
    if is_toshiba:
        try:
            toshiba_total = _read_toshiba_value(ip, community, "print_total", snmp_version, default=None)
            if toshiba_total is not None and toshiba_total >= 0:
                total = toshiba_total
            else:
                # ✅ فیکس نوسان شمارنده Toshiba (علت اصلی spikeهای +۱۲۳هزار صفحه‌ای
                # در لاگ واقعی): وقتی OID vendor خوانده نمی‌شود، نباید به شمارنده
                # استاندارد/ترکیبی با مقیاس متفاوت سوییچ کرد. Snapshot قبلی حفظ می‌شود.
                toshiba_vendor_total_failed = True
                log.warning(
                    "  [%s] Toshiba vendor print_total unreadable this poll; keeping previous snapshot",
                    ip,
                )
            toshiba_color = _read_toshiba_value(ip, community, "print_fc", snmp_version, default=None)
            if toshiba_color is not None:
                color = min(toshiba_color, total) if total is not None and total > 0 else toshiba_color
                device_type = "color" if color > 0 or device_type == "color" else device_type
            bw = max(0, total - (color or 0)) if total is not None else bw
            toshiba_data = _collect_toshiba_job_data(ip, community, snmp_version, total, color)
            has_baseline = bool((store._prev.get(ip) or {}).get("print_total") is not None)
            if (not total or total <= 0) and not has_baseline and toshiba_data and isinstance(toshiba_data.get("suggested_total"), int) and toshiba_data.get("suggested_total") > 0:
                # فقط در اولین poll (بدون baseline) از ترکیب شمارنده‌های جانبی استفاده کن.
                # بعد از baseline، سوییچ به این شمارنده‌های جانبی (مثل comp_sum=۲۱۲۴ در
                # لاگ واقعی در مقابل print_total=۱۶۰هزار) باعث deltaهای انفجاری می‌شد.
                total = toshiba_data["suggested_total"]
                log.info("Toshiba total fallback (first poll only) for %s -> %s", ip, total)
            if (bw is None or bw <= 0) and toshiba_data and isinstance(toshiba_data.get("suggested_bw"), int) and toshiba_data.get("suggested_bw") > 0:
                bw = toshiba_data["suggested_bw"]
                log.info("Toshiba BW fallback for %s -> %s", ip, bw)
        except Exception as exc:
            log.warning("Toshiba vendor counters unavailable for %s: %s", ip, exc)
            toshiba_data = None
    
    # ─── مدل و سریال ─────────────────────────────────────────────
    model = "Unknown"
    serial = "N/A"
    
    # تلاش برای خواندن مدل از OIDهای مختلف
    model_oids = [
        "1.3.6.1.2.1.43.5.1.1.16.1",  # standard
        "1.3.6.1.4.1.1129.2.3.50.1.2.3.1.3.1.1",  # Toshiba
        "1.3.6.1.4.1.11.2.3.9.1.1.3.1.1.1.1.2.0",  # HP
        "1.3.6.1.4.1.1602.1.1.1.1.0",  # Canon
        "1.3.6.1.4.1.2435.2.4.3.99.3.1.6.1.2.1",  # Brother
        "1.3.6.1.2.1.1.5.0",  # sysName (آخرین fallback)
    ]
    for oid in model_oids:
        val = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=2.0)
        if val and str(val).strip() not in ("", "N/A", "None"):
            model = str(val).strip()[:100]
            break
    
    serial_oids = [
        "1.3.6.1.2.1.43.5.1.1.17.1",
        "1.3.6.1.4.1.1129.2.3.50.1.2.4.1.8.1.1",
        "1.3.6.1.4.1.11.2.3.9.1.1.3.1.1.1.1.3.0",
        "1.3.6.1.4.1.1602.1.2.1.4.0",
        "1.3.6.1.4.1.2435.2.4.3.99.3.1.6.1.2.3",  # Brother
    ]
    for oid in serial_oids:
        val = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=2.0)
        if val and str(val).strip() not in ("", "N/A", "None"):
            serial = str(val).strip()[:100]
            break
    
    # ─── تبدیل تونرها به فرمت toners ─────────────────────────────
    toners = {}
    for s in supplies:
        if s["type_name"] in ("toner", "cartridge", "drum", "opc"):
            lowered_name = str(s.get("name", "")).lower()
            if s["type_name"] in ("drum", "opc") or "drum" in lowered_name:
                color_key = "drum"
                display_level = s["percent"]
            else:
                # ✅ باگ #8: استفاده از تشخیص رنگ دقیق با regex
                color_key = _detect_toner_color(s["name"]) or "black"
                display_level = _canon_display_percent(model, s["name"], s["percent"])

            # ✅ محافظ «مقدار بهتر را نگه دار»: اگر برای این کلید قبلاً عدد واقعی
            # ثبت شده (مثلاً idx=1 با level=10) و کاندیدای جدید داده‌ای ندارد
            # (سطر فانتوم/ثانویه)، روی مقدار واقعی بازنویسی نکن.
            existing = toners.get(color_key)
            if existing and existing.get("level") is not None and s["percent"] is None:
                continue

            toners[color_key] = {
                "level": display_level,
                # ✅ فیکس: raw_level باید واقعاً خام باشد تا یادگیری yield از داده‌ی
                # مصنوعی ساخته‌نشده استفاده نکند. مقادیر برآوردی (کد وضعیت گسسته‌ی
                # Canon، و…) فقط برای نمایش‌اند و raw را می‌سازند None.
                "raw_level": s["percent"] if s.get("percent_source") != "canon_status_code" else None,
                "percent_source": s.get("percent_source"),
                "supply_present": s.get("supply_present", False),
                "status": s["status"] if s["status"] != "N/A" else "unknown",
                "name": s["name"],
                "remaining": s["remaining"],
                "max": s["max"],
                "unit": s.get("unit"),
                "unit_code": s.get("unit_code"),
                "device_capacity_pages": s.get("device_capacity_pages"),
                "index": s.get("index"),
            }

    # ─── Toshiba TopAccess fallback ───
    # ✅ فیکس دقت تونر: بسیاری از e-STUDIOها درصد تونر را از prtMarkerSuppliesTable
    # نمی‌دهند (percent=None) و فقط وضعیت سطحی (kestrel NE flags) دارند که به
    # پله‌های تخمینی ۸۵٪/۷۰٪/۳۰٪/… تبدیل می‌شود. در این حالت از scrape پنل
    # TopAccess (همان روش کالکتور legacy با کش داخلی) درصد واقعی می‌خوانیم.
    if is_toshiba:
        need_scrape = False
        for t in toners.values():
            lvl = t.get("raw_level", t.get("level"))
            if lvl is None and (t.get("status") in ("unknown", "no_sensor", "not_supported", "N/A") or t.get("percent_missing", False)):
                need_scrape = True
                break
        if not any((t.get("raw_level", t.get("level")) is not None) for t in toners.values()):
            need_scrape = True
        if need_scrape:
            try:
                from core.collectors.toshiba import _scrape_toshiba_toners
                scraped = _scrape_toshiba_toners(ip)
                if scraped:
                    for color_key, pct in scraped.items():
                        t = toners.get(color_key)
                        if not t:
                            toners[color_key] = {
                                "level": pct, "raw_level": pct, "status": "unknown",
                                "name": f"{color_key} toner", "remaining": -1, "max": -1,
                                "source": "topaccess_scrape",
                            }
                            continue
                        if (t.get("raw_level") is None):
                            t["raw_level"] = pct
                        if (t.get("level") is None):
                            t["level"] = pct
                            t["source"] = "topaccess_scrape"
                            if pct <= 5:
                                t["status"] = "critical"
                            elif pct <= 15:
                                t["status"] = "low"
                            else:
                                t["status"] = "ok"
                    log.info("  [%s] Toshiba toner percent via TopAccess: %s", ip, scraped)
            except Exception as exc:
                log.debug("Toshiba TopAccess fallback failed for %s: %s", ip, exc)

    # ─── Canon Remote UI fallback ───
    # ✅ فیکس Canon i-SENSYS (مثل LBP233dw): اگر از مسیر SNMP هیچ درصد «واقعی»
    # نرسید (کد گسسته یا کلاً نامشخص)، از پنل Remote UI دستگاه اسکریپ می‌کنیم —
    # همان روشی که کالکتور legacy دارد. داده‌ی scrape واقعی است پس raw_level هم
    # صادقانه پر می‌شود.
    if brand == "canon" or "canon" in sys_desc_str.lower():
        if not any(t.get("raw_level") is not None for t in toners.values()):
            try:
                from core.collectors.canon import _scrape_canon_toners
                scraped = _scrape_canon_toners(ip)
                if scraped:
                    for color_key, pct in scraped.items():
                        t = toners.get(color_key)
                        if not t:
                            toners[color_key] = {
                                "level": pct, "raw_level": pct, "status": "unknown",
                                "name": f"{color_key} toner", "remaining": -1, "max": -1,
                                "source": "canon_remote_ui",
                            }
                            continue
                        if t.get("raw_level") is None:
                            t["raw_level"] = pct
                        if t.get("level") is None:
                            t["level"] = pct
                            t["source"] = "canon_remote_ui"
                            if pct == 0:
                                t["status"] = "empty"
                            elif pct <= 10:
                                t["status"] = "critical"
                            elif pct <= 25:
                                t["status"] = "low"
                            else:
                                t["status"] = "ok"
                    log.info("  [%s] Canon toner percent via Remote UI scrape: %s", ip, scraped)
            except Exception as exc:
                log.debug("Canon Remote UI fallback failed for %s: %s", ip, exc)

    # ─── Brother web fallback ───
    # ✅ خانواده‌ی NC-8xxx/MFC-85xx در جدول استاندارد فقط level=-3 (مقداری هست)
    # می‌دهند و OID اختصاصی هم همیشه هست نیست؛ پنل وب آن‌ها (مورد تأیید: 172.16.25.43)
    # معمولاً باز است و درصد را متنی نشان می‌دهد. شرط «تونرِ واقعیِ گمشده» (نه فقط
    # «هیچ level») تا درامِ سالم (مثلاً ۹۰٪) مانع اجرای اسکریپ نشود.
    if brand == "brother" or "brother" in sys_desc_str.lower():
        if _toner_level_missing(toners):
            try:
                from core.collectors.brother import _scrape_brother_toners
                scraped = _scrape_brother_toners(ip)
                if scraped:
                    merged = _merge_scraped_toners(toners, scraped, "brother_web")
                    if merged:
                        log.info("  [%s] Brother toner percent via web scrape: %s", ip, scraped)
            except Exception as exc:
                log.debug("Brother web fallback failed for %s: %s", ip, exc)

    # ─── HP web fallback ───
    # ✅ مورد واقعی E52645: دستگاه برای کارتریج W9008MC مقدار level=-2 (unknown)
    # برمی‌گرداند و OID خصوصی NPCL هم پاسخ نمی‌دهد. بسیاری از FutureSmartها پورت
    # ۸۰ را بسته‌اند ولی ۴۴۳ باز است — تلاش HTTP+HTTPS + XML مصرفی‌های DevMgmt.
    if brand == "hp" or "hp" in sys_desc_str.lower() or "laserjet" in sys_desc_str.lower():
        if _toner_level_missing(toners):
            try:
                from core.collectors.hp import _scrape_hp_toners
                scraped = _scrape_hp_toners(ip)
                if scraped:
                    merged = _merge_scraped_toners(toners, scraped, "hp_web")
                    if merged:
                        log.info("  [%s] HP toner percent via web scrape: %s", ip, scraped)
                # ✅ کشف روی دستگاه واقعی (E52645 + M401dn): وقتی SNMP برای
                # کارتریج HP مقدار -2 (unknown) می‌دهد، معمولاً چون کارتریج
                # شارژی/استفاده‌شده است و خود EWS هم عدد ندارد. اسکریپر وضعیت
                # «used» را برمی‌گرداند تا UI به‌جای «نامشخص مبهم» بگوید علت چیست.
                hp_used = set((scraped or {}).get("_used") or [])
                for color_key in hp_used:
                    t = toners.get(color_key)
                    if t is None:
                        toners[color_key] = {
                            "level": None, "raw_level": None, "status": "used",
                            "name": f"{color_key} cartridge", "remaining": -1,
                            "max": -1, "source": "hp_web_used",
                            "note": "used_cartridge",
                        }
                        continue
                    if t.get("level") is None:
                        t["status"] = "used"
                        t["note"] = "used_cartridge"
                        t.setdefault("source", "hp_web_used")
                        log.info("  [%s] HP %s: کارتریج شارژی/استفاده‌شده (SupplyState=Used) — خود HP درصد را گزارش نمی‌کند",
                                 ip, color_key)
                if not scraped:
                    log.info(
                        "  [%s] HP: تونر از SNMP و وب در دسترس نیست — احتمال کارتریج شارژی/طرح غیراصل "
                        "(chip ناشناخته) یا بسته‌بودن EWS روی دستگاه/فایروال", ip)
            except Exception as exc:
                log.debug("HP web fallback failed for %s: %s", ip, exc)

    # اگر تونری پیدا نشد، یک تونر مشکی پیش‌فرض
    if not toners:
        toners["black"] = {"level": None, "raw_level": None, "status": "unknown", "name": "Toner", "remaining": -1, "max": -1}

    # فیلدهای مصرف برای همه برندها به صورت سازگار نگه داشته می‌شوند؛ اگر داده‌ای
    # وجود نداشته باشد UI آن را نمایش نمی‌دهد.
    for toner_data in toners.values():
        toner_data.setdefault("usage", None)
        toner_data.setdefault("usage_m", None)

    if is_toshiba:
        for color_key in ("black", "cyan", "magenta", "yellow"):
            if color_key not in toners:
                continue
            usage_raw = _read_toshiba_value(ip, community, f"toner_{color_key}_usage", snmp_version, default=None)
            if usage_raw is not None and usage_raw > 0:
                toners[color_key]["usage"] = usage_raw
                toners[color_key]["usage_m"] = round(usage_raw / 1_000_000, 2)

    # ─── اعمال override دستی تونر بر اساس مصرف صفحات ─────────────────
    prev_override = store._prev.get(ip) or {}
    if prev_override.get('yield_per_page', 2000) == 2000 and not prev_override.get('force_estimate'):
        boot = _bootstrap_yield_from_history(ip, prev_override)
        if boot:
            prev_override = store._prev.get(ip) or prev_override

    override_color = prev_override.get('override_color')
    pages_since_last_reset = get_pages_since_last_reset(prev_override, total)
    if prev_override.get('manual_override') and override_color and override_color in toners:
        snmp_level = toners[override_color].get('level')
        final_level = apply_toner_override(ip, total, snmp_level, color=override_color)
        if final_level is not None:
            toners[override_color]['level'] = final_level
            if prev_override.get('force_estimate'):
                toners[override_color]['source'] = 'forced_estimate'
            if final_level == 0:
                toners[override_color]['status'] = 'empty'
            elif final_level <= 5:
                toners[override_color]['status'] = 'critical'
            elif final_level <= 15:
                toners[override_color]['status'] = 'low'
            else:
                toners[override_color]['status'] = 'ok'

    # ─── هشدارها ─────────────────────────────────────────────────
    alerts = []
    for color_key, toner in toners.items():
        level = toner.get("level")
        if level is None:
            continue
        try:
            level = int(level)
        except (TypeError, ValueError):
            continue
        if level > TONER_ALERT_THRESHOLDS.get("warning", 15):
            continue
        status = "critical" if level <= TONER_ALERT_THRESHOLDS.get("critical", 5) else "low"
        toner["status"] = "empty" if level == 0 else status
        alerts.append({
            "message": f"{toner.get('name', color_key)}: {toner['status']} ({level}%)",
            # کد هشدار باید پایدار باشد؛ index جدول SNMP ممکن است بین pollها تغییر کند.
            "code": f"toner:{color_key}",
        })
    
    # ─── uptime ──────────────────────────────────────────────────
    # ✅ فیکس مهم: خواندن ناموفق uptime نباید به int(0) تبدیل شود؛ مقدار صفر در
    # _uptime_reset به‌اشتباه «ریبوت» تشخیص داده می‌شد (۲۷۶ ریست کاذب در لاگ واقعی).
    ut = None
    ut_raw = snmp_get_with_fallback(ip, "1.3.6.1.2.1.1.3.0", community, version=snmp_version, timeout=2.0)
    if ut_raw is None:
        # SNMP stack برخی دستگاه‌ها (مخصوصاً Toshiba) کند است؛ یک retry بخاطر‌سپرده‌تر
        ut_raw = snmp_get_with_fallback(ip, "1.3.6.1.2.1.1.3.0", community, version=snmp_version, timeout=4.0)
    if ut_raw is not None:
        try:
            ut = int(ut_raw)
        except (TypeError, ValueError):
            ut = None
    us = (ut or 0) // 100
    uptime_str = f"{us//86400}d {(us%86400)//3600:02d}:{(us%3600)//60:02d}" if ut else "N/A"
    
    elapsed = int((time.time() - start_time) * 1000)
    
    # ─── ثبت در toner_report.txt ─────────────────────────────────
    _log_to_toner_report(f"   SNMP v{snmp_version} | مدل: {model} | نوع: {device_type}")
    bw_display = f"{bw:,}" if isinstance(bw, int) else "نامشخص"
    color_display = f"{color:,}" if isinstance(color, int) else "نامشخص"
    _log_to_toner_report(f"   کل صفحات: {total:,} | رنگی: {color_display} | سیاه‌سفید: {bw_display}")
    for color_key, t in toners.items():
        pct_str = f"{t['level']}%" if t['level'] is not None else "N/A"
        status_icon = {"ok": "✅", "low": "🟡", "critical": "🟠", "empty": "🔴"}.get(t["status"], "❓")
        _log_to_toner_report(f"   {color_key}: {pct_str} {status_icon}")
    _log_to_toner_report(f"   زمان پاسخ: {elapsed}ms")
    try:
        # ذخیره پروفایل OID برای این دستگاه (به‌روز رسانی یا ساخت جدید)
        _save_oid_profile(ip, community, snmp_version, model, device_type, total, color, bw, supplies, trays, elapsed)
    except Exception:
        log.debug("Saving oid profile failed, continuing")
    
    # ✅ باگ #2 + #5 + #12: حذف نوشتن مستقیم DB
    # نوشتن در دیتابیس حالا بعد از _counters_event انجام می‌شه
    # (به خطوط بعدی مراجعه کنید)
    
    # ─── ثبت رویداد PRINT / REFILL ────────────────────────────────
    # ✅ باگ #2: ثبت رویداد BEFORE نوشتن در دیتابیس
    prev = store._prev.get(ip) or {}
    black_level = None
    # ✅ یادگیری yield و منطق REFILL باید از سطح خام دستگاه استفاده کنند، نه از
    # مقدار نمایشی/override؛ در غیر این صورت چرخه‌ی تخمین-یادگیری دایره‌ای می‌شود.
    if toners.get("black", {}).get("raw_level") is not None:
        black_level = toners["black"]["raw_level"]
        toners["black"]["level"] = toners["black"]["level"] if toners["black"].get("level") is not None else black_level
    elif toners.get("black", {}).get("level") is not None:
        black_level = toners["black"]["level"]
    else:
        for t in toners.values():
            if t.get("raw_level") is not None:
                black_level = t["raw_level"]
                break
            if t.get("level") is not None:
                black_level = t["level"]
                break
    prev_toner = prev.get("toner_level")
    paper_size = (toshiba_data or {}).get("paper_size")
    a3_total = (toshiba_data or {}).get("a3_total")
    a4_total = (toshiba_data or {}).get("a4_total")
    bw_for_event = (toshiba_data or {}).get("black_white_for_event", bw)

    if is_toshiba and toshiba_vendor_total_failed:
        # مسیر حفاظت‌شده: None → _counters_event هشدار می‌دهد و snapshot قبلی را
        # نگه می‌دارد؛ هیچ delta تقلبی (COUNTER_RESET/PRINT_OVERFLOW زنجیره‌ای)
        # ساخته نمی‌شود. وقتی vendor دوباره به‌کار افتاد، مقادیر سازگار می‌مانند.
        total_for_event = None
    else:
        total_for_event = total

    # ✅ باگ #2: ثبت رویداد اول (قبل از نوشتن DB)
    _counters_event(ip, total_for_event, prev, alerts, [a["code"] for a in alerts],
                    full_color=color, black_white=bw_for_event, paper_size=paper_size,
                    current_toner_level=black_level, prev_toner_level=prev_toner,
                    uptime=ut, a3_total=a3_total, a4_total=a4_total,
                    poll_timestamp=datetime.fromtimestamp(start_time).isoformat(),
                    paper_split=(toshiba_data or {}).get("paper_split"))

    # ─── Yield Engine جدید: یادگیری per-cartridge/per-color بر اساس history و anchor ───
    # این لایه مستقل از منطق قدیمی است و برای پرینترهای کم‌مصرف و رنگی طراحی شده است.
    yield_engine_meta = {}
    try:
        from core.yield_engine import process_printer_yield_snapshot
        yield_engine_meta = process_printer_yield_snapshot(
            ip=ip,
            printer_model=model,
            counters={"total": total, "full_color": color, "black_white": bw},
            toners=toners,
            device_type=device_type,
            timestamp=datetime.fromtimestamp(start_time).isoformat(),
            source="poll",
        )
    except Exception as exc:
        log.exception("Yield engine processing failed for %s: %s", ip, exc)
    
    # ✅ باگ #5: منبع حقیقت فقط PrevStore/`printer_counters` است.
    # از نوشتن مستقیم و مضاعف در دیتابیس خودداری می‌کنیم.
    if save_to_db:
        log.debug("Enhanced snapshot persisted via PrevStore only for %s", ip)

    # آخرین مقدار yield ممکن است داخل _counters_event یاد گرفته/به‌روز شده باشد؛
    # بنابراین برای خروجی API دوباره از PrevStore می‌خوانیم.
    final_prev = store._prev.get(ip) or prev_override or {}
    try:
        yield_per_page = int(final_prev.get("yield_per_page", 2000) or 2000)
    except (TypeError, ValueError):
        yield_per_page = 2000
    yield_source = "default" if yield_per_page == 2000 else "learned"
    if final_prev.get("force_estimate"):
        yield_source = "forced_estimate"

    # مقدار legacy برای backward compatibility نگه داشته می‌شود، ولی اگر Yield Engine
    # برای black خروجی داشته باشد، خروجی API از مقدار دقیق‌تر per-cartridge استفاده می‌کند.
    api_yield_per_page = yield_per_page
    api_yield_source = yield_source
    api_yield_confidence = "low" if yield_source == "default" else "medium"
    black_meta = yield_engine_meta.get("black") if isinstance(yield_engine_meta, dict) else None
    if black_meta:
        api_yield_per_page = int(black_meta.get("yield_per_page") or yield_per_page)
        api_yield_source = black_meta.get("yield_source") or yield_source
        api_yield_confidence = black_meta.get("confidence") or api_yield_confidence

    # sync با مسیر legacy: فایل missing_yield_printers.txt و printer_counters قدیمی
    # قبلاً فقط مقدار PrevStore را می‌دیدند و از catalog/device_capacity بی‌خبر بودند.
    if api_yield_per_page and api_yield_per_page != yield_per_page:
        try:
            from core.database import update_missing_yield_list
            store._prev.set(ip, {"yield_per_page": api_yield_per_page})
            update_missing_yield_list(ip, api_yield_per_page, api_yield_source)
        except Exception as exc:
            log.exception("legacy yield sync failed for %s: %s", ip, exc)

    for toner_color, toner_data in toners.items():
        engine_info = yield_engine_meta.get(toner_color) if isinstance(yield_engine_meta, dict) else None
        if engine_info:
            toner_yield = int(engine_info.get("yield_per_page") or yield_per_page)
            toner_source = engine_info.get("yield_source") or yield_source
            toner_confidence = engine_info.get("confidence") or "low"
            toner_data["capacity_pages"] = toner_yield
            toner_data["capacity_source"] = toner_source
            toner_data["yield_per_page"] = toner_yield
            toner_data["yield_confidence"] = toner_confidence
            toner_data["yield_sample_count"] = engine_info.get("sample_count", 0)
            toner_data["yield_total_weight"] = engine_info.get("total_weight", 0)
            toner_data["cycle_status"] = engine_info.get("cycle_status")
            toner_data["pages_after_zero"] = engine_info.get("pages_after_zero", 0)
            toner_data["cycle_start_counter"] = engine_info.get("cycle_start_counter")
            toner_data["cycle_start_level"] = engine_info.get("cycle_start_level")
            toner_data["yield_cartridge_key"] = engine_info.get("cartridge_key")
            if engine_info.get("shared_profile"):
                toner_data["shared_yield_profile"] = engine_info["shared_profile"]
        else:
            # fallback قدیمی برای دستگاه‌هایی که هنوز level معتبر ندارند.
            toner_data["capacity_pages"] = yield_per_page
            toner_data["capacity_source"] = yield_source if toner_color == "black" else f"{yield_source}_global"
            toner_data["yield_per_page"] = yield_per_page
            toner_data["yield_confidence"] = "low" if yield_source == "default" else "medium"

    if is_toshiba and toshiba_vendor_total_failed:
        prev_counters = (store._prev.get(ip) or {})
        total = prev_counters.get("print_total") if prev_counters.get("print_total") is not None else total
        color = prev_counters.get("full_color", color)
        bw = prev_counters.get("black_white", bw)

    vendor_counters = (toshiba_data or {}).get("counters", {})
    return {
        "ip": ip, "name": name, "nickname": nickname, "brand": brand,
        "device_type": device_type,
        "online": True,
        "last_poll": datetime.now().isoformat(),
        "poll_ms": elapsed,
        "device": {
            "model": model,
            "serial": serial,
            "firmware": "N/A",
            "uptime_str": uptime_str,
        },
        "counters": {
            "total": total,
            "full_color": color if color else None,
            "black_white": bw,
            "printer": vendor_counters.get("printer", total),
            "printer_fc": vendor_counters.get("printer_fc"),
            "printer_bw": vendor_counters.get("printer_bw"),
            "copy": vendor_counters.get("copy"),
            "copy_fc": vendor_counters.get("copy_fc"),
            "copy_bw": vendor_counters.get("copy_bw"),
            "fax": vendor_counters.get("fax"),
            "list": vendor_counters.get("list"),
            "twin": vendor_counters.get("twin"),
            "scan_fc": vendor_counters.get("scan_fc"),
            "scan_bw": vendor_counters.get("scan_bw"),
            "scan_net_fc": vendor_counters.get("scan_net_fc"),
            "scan_net_bw": vendor_counters.get("scan_net_bw"),
            "pages_since_last_reset": pages_since_last_reset,
            "yield_per_page": api_yield_per_page,
            "yield_source": api_yield_source,
            "yield_confidence": api_yield_confidence,
            "force_estimate": final_prev.get("force_estimate", 0),
            "yield_learning_failures": final_prev.get("yield_learning_failures", 0),
        },
        "paper_sizes": (toshiba_data or {}).get("paper_sizes", {}),
        "trays": trays,
        "toners": toners,
        "alerts": alerts,
    }