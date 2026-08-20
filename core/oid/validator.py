"""
اعتبارسنجی مقادیر OID و ثبت خطاها در فایل log
"""

import logging
from datetime import datetime
from config.settings import VALIDATION_LOG_FILE

log = logging.getLogger("PrinterMonitor")

# مقادیر منفی معتبر در SNMP
# -1 = other, -2 = unknown, -3 = not supported
VALID_NEGATIVE_VALUES = {-1, -2, -3}


def _validate_oid_value_strict(key: str, value, category: str) -> tuple:
    if value is None:
        return False, "مقدار None است", "null_value"

    if category == "counter":
        if not isinstance(value, int):
            return False, f"counter باید int باشد، دریافت: {type(value).__name__}", "type_error"
        if value < 0 or value > 999_000_000:
            return False, f"counter خارج از بازه [0, 999M]: {value}", "range_error"

    elif category == "supply":
        if not isinstance(value, int):
            return False, f"supply باید int باشد، دریافت: {type(value).__name__}", "type_error"
        # 🔥 تغییر: پذیرش مقادیر -1, -2, -3 (مقادیر منفی معتبر SNMP)
        if value < -3 or value > 200_000_000:
            return False, f"supply خارج از بازه [-3, 200M]: {value}", "range_error"

    elif category == "tray":
        if not isinstance(value, int):
            return False, f"tray باید int باشد، دریافت: {type(value).__name__}", "type_error"
        if value < -9 or value > 5000:
            return False, f"tray خارج از بازه [-9, 5000]: {value}", "range_error"

    elif category == "status":
        if not isinstance(value, int):
            return False, f"status باید int باشد، دریافت: {type(value).__name__}", "type_error"
        if value < 0 or value > 30:
            return False, f"status خارج از بازه [0, 30]: {value}", "range_error"

    elif category in ("identity", "sys"):
        str_val = str(value).strip()
        if not str_val or str_val in ("None", "0"):
            return False, f"{category} خالی است: {value!r}", "empty_value"

    return True, "✓ معتبر", "valid"


def validate_oid_value(key: str, value, category: str) -> tuple:
    """برمی‌گرداند: (valid: bool, reason: str)"""
    valid, reason, _ = _validate_oid_value_strict(key, value, category)
    return valid, reason


def log_validation_error(ip: str, error_type: str, details: str):
    try:
        timestamp = datetime.now().isoformat()
        with open(VALIDATION_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] IP: {ip} | Type: {error_type}\n")
            f.write(f"  Details: {details}\n\n")
    except Exception as e:
        log.error(f"خطا در نوشتن validation log: {e}")