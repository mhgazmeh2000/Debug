"""
توابع مشترک همه collectorها:
- si, ss: تبدیل ایمن مقادیر SNMP
- _g: wrapper ساده snmp_get با fallback خودکار
- _counters_event: ثبت رویدادهای چاپ و هشدار (با جلوگیری از ثبت اولیه و دلتاهای بزرگ غیرمنطقی)
- validate_counter_consistency: بررسی سازگاری شمارنده‌ها
- detect_brand: تشخیص خودکار برند
"""

import logging
from datetime import datetime

from config.settings import POLL_INTERVAL, TONER_ALERT_THRESHOLDS
from core.snmp.protocol import snmp_get_with_fallback
from core import store
from core.database import add_event

log = logging.getLogger("PrinterMonitor")
DEFAULT_YIELD_PER_PAGE = 2000

# ─── تنظیمات اعتبارسنجی ─────────────────────────────────────────
# حداکثر دلتا منطقی: مقدار پایه برای بازهٔ 30s، سپس متناسب با POLL_INTERVAL تنظیم می‌شود
_BASE_MAX_PER_30S = 200
MAX_REASONABLE_DELTA = max(100, int(_BASE_MAX_PER_30S * (POLL_INTERVAL / 30.0)))
MIN_DELTA_FOR_FALLBACK = 1        # حداقل دلتا برای fallback مبتنی بر total
MIN_VALID_TOTAL_FOR_FIRST_POLL = 50  # اگر total < 50 باشد، شاید پرینتر جدید است
MAX_TOTAL_AFTER_RESET = 5000      # اگر مقدار جدید کمتر از این باشد و قبلی بزرگ بود، ریست شده


def _elapsed_since_prev(prev: dict) -> float:
    """مدت‌زمان گذشته از آخرین snapshot ذخیره‌شده را برحسب ثانیه برمی‌گرداند."""
    updated_at = (prev or {}).get("updated_at")
    if not updated_at:
        return float(POLL_INTERVAL)
    try:
        elapsed = (datetime.now() - datetime.fromisoformat(updated_at)).total_seconds()
        return max(1.0, elapsed)
    except Exception:
        return float(POLL_INTERVAL)


def _dynamic_max_reasonable_delta(prev: dict) -> tuple[int, float]:
    """حد بالای منطقی دلتا را بر اساس فاصله واقعی بین دو poll محاسبه می‌کند."""
    elapsed_seconds = max(float(POLL_INTERVAL), _elapsed_since_prev(prev))
    dynamic_limit = max(100, int(_BASE_MAX_PER_30S * (elapsed_seconds / 30.0)))
    return dynamic_limit, elapsed_seconds


# ─── helpers ────────────────────────────────────────────────────
def si(v, d: int = 0) -> int:
    # ✅ باگ #5: لاگ خطاها به جای مخفی کردن + فقط exception های مرتبط
    if v is None:
        return d
    try:
        return int(v)
    except (ValueError, TypeError) as e:
        log.warning(f"si() conversion failed: value={v!r} → default={d} ({e})")
        return d
    except Exception as e:
        log.error(f"si() unexpected error: value={v!r} → default={d} ({e})")
        return d


def ss(v, d: str = "N/A") -> str:
    return str(v).strip() if v is not None and str(v).strip() else d


def _g(ip: str, oid: str, community: str, timeout: float = 2.5):
    """
    Wrapper برای snmp_get_with_fallback که سعی می‌کند ابتدا v2c و در صورت شکست v1 را امتحان کند.
    """
    return snmp_get_with_fallback(ip, oid, community, timeout=timeout)


def _bootstrap_yield_from_history(ip: str, prev: dict):
    """اگر yield هنوز پیش‌فرض است، از snapshotهای تاریخی برای تخمین اولیه استفاده کن."""
    if (prev or {}).get("yield_per_page", DEFAULT_YIELD_PER_PAGE) != DEFAULT_YIELD_PER_PAGE:
        return None
    try:
        from core.database import estimate_yield_from_history
        result = estimate_yield_from_history(ip, days=7, min_points=3, min_pages=500)
        if not result:
            return None
        estimated_yield = result["yield_per_page"]
        store._prev.set(ip, {
            "yield_per_page": estimated_yield,
            "yield_learning_failures": 0,
        })
        log.info(
            f"  [{ip}] historical yield bootstrap -> {estimated_yield} "
            f"(pages={result['total_pages']}, toner_drop={result['total_drop']}, samples={result['sample_points']})"
        )
        return estimated_yield
    except Exception as exc:
        log.exception("Historical yield bootstrap failed for %s: %s", ip, exc)
        return None



def _learn_yield_per_page(ip: str, delta_pages: int, prev_toner_level: int, current_toner_level: int, prev: dict):
    """یادگیری خودکار yield_per_page بر اساس مصرف تونر و صفحات چاپ شده."""
    prev = prev or {}
    if delta_pages <= 0 or prev_toner_level is None or current_toner_level is None:
        return
    # از داده‌های مشکوک/درحال تایید برای یادگیری yield استفاده نکن.
    if prev.get("last_counter_error") or prev.get("pending_overflow_total") is not None or prev.get("pending_refill_new_toner") is not None:
        log.debug("  [%s] yield learning skipped due to pending/anomalous counter state", ip)
        return
    # ✅ فیکس یادگیری دایره‌ای: وقتی override دستی فعال است، سطح تونری که به
    # این تابع می‌رسد خودش از همان yield ساخته شده؛ یادگیری از آن = تأیید خودش.
    if prev.get("manual_override"):
        log.debug("  [%s] yield learning skipped while manual override is active", ip)
        return

    current_yield = int(prev.get("yield_per_page", DEFAULT_YIELD_PER_PAGE) or DEFAULT_YIELD_PER_PAGE)
    if current_yield == DEFAULT_YIELD_PER_PAGE:
        bootstrapped = _bootstrap_yield_from_history(ip, prev)
        if bootstrapped:
            current_yield = bootstrapped

    toner_drop = prev_toner_level - current_toner_level
    if toner_drop <= 0:
        return
    # افت‌های خیلی کوچک تونر نویز زیادی دارند؛ حداقل ۵۰ صفحه برای تخمین قابل قبول لازم است.
    if toner_drop < 1 or delta_pages < 50:
        return

    try:
        estimated_yield = int(round(delta_pages * 100.0 / toner_drop))
    except ZeroDivisionError:
        log.warning("  [%s] yield learning skipped بسبب تقسیم بر صفر", ip)
        return
    except Exception as exc:
        log.exception("  [%s] yield learning error: %s", ip, exc)
        return

    if estimated_yield < 300 or estimated_yield > 20000:
        log.info(f"  [{ip}] yield learning ignored خارج از بازه: {estimated_yield}")
        return

    diff_ratio = abs(estimated_yield - current_yield) / max(current_yield, 1)
    failures = int(prev.get("yield_learning_failures", 0) or 0)
    force_estimate = int(prev.get("force_estimate", 0) or 0)

    if current_yield != DEFAULT_YIELD_PER_PAGE and diff_ratio > 0.30:
        failures += 1
        log.info(
            f"  [{ip}] yield discrepancy detected: current={current_yield}, estimated={estimated_yield}, "
            f"ratio={diff_ratio:.2f}, failures={failures}/10"
        )
        if failures >= 10 and not force_estimate:
            store._prev.set(ip, {
                "force_estimate": 1,
                "yield_learning_failures": failures,
            })
            log.info(f"  [{ip}] force_estimate enabled after repeated yield discrepancies")
        else:
            store._prev.set(ip, {"yield_learning_failures": failures})
        return

    failures = 0
    if current_yield != DEFAULT_YIELD_PER_PAGE:
        if diff_ratio < 0.05:
            return
        estimated_yield = int(round((current_yield * 0.6) + (estimated_yield * 0.4)))

    if estimated_yield == current_yield:
        return

    source = "auto_learn"
    log.info(
        f"  [{ip}] yield_per_page updated: {current_yield} -> {estimated_yield} "
        f"(source={source}, pages={delta_pages}, toner_drop={toner_drop})"
    )
    store._prev.set(ip, {
        "yield_per_page": estimated_yield,
        "yield_learning_failures": failures,
    })


def get_pages_since_last_reset(prev: dict, total: int):
    """محاسبه تعداد صفحات چاپ‌شده از زمان آخرین تنظیم مجدد کارتریج."""
    if not prev or not prev.get("manual_override"):
        return None
    override_start_total = prev.get("override_start_total")
    if override_start_total is None:
        return None
    try:
        pages_since_override = int(total) - int(override_start_total)
    except Exception:
        return None
    if pages_since_override < 0:
        return None
    return pages_since_override



def apply_toner_override(ip: str, total: int, snmp_level: int = None, color: str = None):
    """محاسبه مجدد سطح تونر بر اساس override دستی و میزان صفحات چاپ‌شده."""
    prev = store._prev.get(ip) or {}
    if not prev.get("manual_override") or color is None:
        return None

    if prev.get("override_color") != color:
        return None

    override_start_total = prev.get("override_start_total")
    override_start_toner = prev.get("override_start_toner")
    yield_per_page = prev.get("yield_per_page", DEFAULT_YIELD_PER_PAGE)

    if override_start_total is None or override_start_toner is None:
        return None

    pages_since_override = get_pages_since_last_reset(prev, total)

    # 🔥 اصلاح: اگر total کمتر از override_start_total باشد، یعنی دستگاه
    # ریست شده و override دیگر معتبر نیست → برگرداندن مقدار خام سنسور
    if pages_since_override is None:
        log.debug(f"  [{ip}] Toner override invalidated: total({total}) < start({override_start_total}). "
                  f"Returning raw SNMP level: {snmp_level}")
        return snmp_level

    if pages_since_override == 0:
        return override_start_toner

    if not isinstance(yield_per_page, int) or yield_per_page <= 0:
        yield_per_page = DEFAULT_YIELD_PER_PAGE

    estimated_drop = int(round(pages_since_override * 100.0 / yield_per_page))
    final_level = max(0, min(100, override_start_toner - estimated_drop))

    log.debug(f"  [{ip}] apply_toner_override: override_color={color}, total={total}, "
              f"start_total={override_start_total}, start_toner={override_start_toner}, "
              f"yield_per_page={yield_per_page}, pages_since_reset={pages_since_override}, final={final_level}")
    return final_level


# ─── رویدادها ─────────────────────────────────────────────────
def _counters_event(ip: str, total: int, prev: dict, alerts: list, curr_codes: list,
                    full_color: int = None, black_white: int = None,
                    paper_size: str = None, username: str = None,
                    current_toner_level: int = None, prev_toner_level: int = None,
                    uptime: int = None,
                    a3_total: int = None, a4_total: int = None,
                    poll_timestamp: str = None,
                    paper_split: dict = None,
                    paper_detail: dict = None, func_split: dict = None):
    """
    ثبت رویدادهای چاپ/هشدار با محافظت در برابر داده‌های مشکوک SNMP.

    اصل مهم: اگر counter مشکوک است، آن را به عنوان حقیقت در PrevStore ذخیره نمی‌کنیم؛
    ابتدا خطای خواندن یا anomaly ثبت می‌شود تا از زنجیره لاگ دروغین
    COUNTER_RESET → PRINT_OVERFLOW جلوگیری شود.
    """
    prev = prev or {}
    alerts = alerts or []
    curr_codes = curr_codes or []

    def _to_int(value, default=None):
        try:
            if value is None:
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    def _uptime_reset(curr, old) -> bool:
        curr_i = _to_int(curr)
        old_i = _to_int(old)
        # sysUpTime بر حسب صدم ثانیه است؛ 60 ثانیه حاشیه خطا می‌گذاریم.
        # ✅ فیکس باگ ریست کاذب: اگر خواندن uptime ناموفق بوده (None یا 0) هرگز
        # آن را «ریبوت» تلقی نکن. شواهد واقعی: ۲۷۶ مورد از ۳۸۵ COUNTER_RESET
        # با current_uptime=0 (خواندن ناموفق SNMP) ثبت شده بود.
        if curr_i is None or curr_i <= 0 or old_i is None or old_i <= 0:
            return False
        return curr_i < old_i - 60 * 100

    total = _to_int(total)
    prev_total = _to_int(prev.get("print_total")) if prev else None
    prev_fc = _to_int(prev.get("full_color")) if prev.get("full_color") is not None else None
    prev_bw = _to_int(prev.get("black_white")) if prev.get("black_white") is not None else None
    prev_uptime = prev.get("uptime") if prev else None
    prev_toner_level = prev_toner_level if prev_toner_level is not None else (prev.get("toner_level") if prev else None)
    reboot_detected = _uptime_reset(uptime, prev_uptime)
    dynamic_max_delta, elapsed_seconds = _dynamic_max_reasonable_delta(prev)

    if total is None:
        add_event(ip, "SNMP_COUNTER_READ_ERROR", {
            "message": "خواندن شمارنده کل نامعتبر بود (total=None)؛ snapshot قبلی حفظ شد",
            "severity": "warning",
            "prev_total": prev_total,
            "current_total": None,
            "prev_uptime": prev_uptime,
            "current_uptime": uptime,
        })
        store._prev.set(ip, {
            "print_total": prev_total,
            "full_color": prev_fc,
            "black_white": prev_bw,
            "toner_level": current_toner_level if current_toner_level is not None else prev_toner_level,
            "alert_codes": curr_codes,
            "last_alert_codes": curr_codes,
            "uptime": uptime if uptime is not None else prev_uptime,
            "reset_prev_total": prev.get("reset_prev_total"),
        })
        return

    # ─── ثبت رویدادهای هشدار جدید با جلوگیری از تکرار ───────────────
    suppress_toner_alerts = False
    if prev.get("manual_override") and prev.get("override_start_toner") is not None:
        try:
            current_level_int = int(current_toner_level) if current_toner_level is not None else None
            override_start = int(prev.get("override_start_toner"))
            if current_level_int is not None and current_level_int > TONER_ALERT_THRESHOLDS.get("warning", 15) and current_level_int >= override_start - 1:
                suppress_toner_alerts = True
        except (TypeError, ValueError):
            suppress_toner_alerts = False

    if curr_codes and not suppress_toner_alerts:
        alert_codes_list = prev.get("last_alert_codes", []) if prev else []
        new_codes = [c for c in curr_codes if c not in alert_codes_list]
        for code in new_codes:
            msg = next((a.get("message") for a in alerts if a.get("code") == code), f"Error {code}")
            add_event(ip, "ALERT", {"message": msg, "code": code, "severity": "warning"})

    # ─── اولین poll: baseline بگیر، لاگ چاپ نزن ─────────────────────
    if prev_total is None:
        log.warning(f"  [{ip}] جلوگیری از ثبت رویداد PRINT در اولین poll (total={total:,})")
        store._prev.set(ip, {
            "print_total": total,
            "toner_level": current_toner_level,
            "full_color": full_color,
            "black_white": black_white,
            "alert_codes": curr_codes,
            "last_alert_codes": curr_codes,
            "uptime": uptime,
            "a3_total": a3_total,
            "a4_total": a4_total,
            "pending_overflow_total": None,
            "pending_refill_new_toner": None,
        })
        return

    # ─── محافظ اصلی: total=0 یا کاهش counter بدون reboot = خطای SNMP، نه reset ───
    suspicious_zero = total == 0 and prev_total >= 1000 and not reboot_detected
    suspicious_drop = total < prev_total and not reboot_detected
    if suspicious_zero or suspicious_drop:
        etype = "SNMP_COUNTER_READ_ERROR" if suspicious_zero else "COUNTER_ANOMALY"
        add_event(ip, etype, {
            "message": (
                f"خواندن شمارنده مشکوک بود: قبلی {prev_total:,} → جدید {total:,}. "
                "uptime ریست نشده؛ snapshot قبلی حفظ شد."
            ),
            "severity": "warning",
            "prev_total": prev_total,
            "current_total": total,
            "delta": total - prev_total,
            "prev_uptime": prev_uptime,
            "current_uptime": uptime,
            "is_reboot": False,
        })
        log.warning("  [%s] suspicious counter read ignored: prev=%s current=%s uptime %s→%s", ip, prev_total, total, prev_uptime, uptime)
        store._prev.set(ip, {
            "print_total": prev_total,
            "full_color": prev_fc,
            "black_white": prev_bw,
            "toner_level": current_toner_level if current_toner_level is not None else prev_toner_level,
            "alert_codes": curr_codes,
            "last_alert_codes": curr_codes,
            "uptime": uptime if uptime is not None else prev_uptime,
            "a3_total": prev.get("a3_total"),
            "a4_total": prev.get("a4_total"),
            "last_counter_error": etype,
            "reset_prev_total": prev.get("reset_prev_total"),
        })
        return

    # ─── reset واقعی فقط با شواهد قوی ───────────────
    # شرط‌ها: (۱) شمارنده به مقدار خیلی کوچک افتاده (ریبوت معمولاً شمارنده را
    # در NVRAM حفظ می‌کند؛ ریست واقعی شمارنده فقط با سرویس/فکتوری‌ریست اتفاق می‌افتد)،
    # (۲) uptime واقعاً کم شده باشد، (۳) شمارنده قبلی معنی‌دار بزرگ بوده باشد.
    # در غیر این صورت این یک anomaly خواندنی است و فقط هشدار می‌دهیم و snapshot را حفظ می‌کنیم.
    strong_reset_evidence = (
        total < MAX_TOTAL_AFTER_RESET
        and prev_total >= MIN_DELTA_FOR_FALLBACK * 1000
        and reboot_detected
    )
    if total < prev_total and strong_reset_evidence:
        add_event(ip, "COUNTER_RESET", {
            "message": f"شمارنده از {prev_total:,} به {total:,} کاهش یافت (ریبوت دستگاه)",
            "severity": "error",
            "prev_total": prev_total,
            "current_total": total,
            "delta": total - prev_total,
            "prev_uptime": prev_uptime,
            "current_uptime": uptime,
            "is_reboot": True,
        })
        store._prev.set(ip, {
            "print_total": total,
            "toner_level": current_toner_level,
            "full_color": full_color,
            "black_white": black_white,
            "alert_codes": curr_codes,
            "last_alert_codes": curr_codes,
            "uptime": uptime,
            "a3_total": a3_total,
            "a4_total": a4_total,
            "reset_prev_total": prev_total,
            # ✅ فیکس: ریست شمارنده دیگر yield یادگرفته‌شده و تنظیم دستی تونر را
            # نابود نمی‌کند. override فقط وقتی بی‌اعتبار است که شمارنده واقعاً
            # به کمتر از نقطه‌ی شروع آن رفته باشد.
            "manual_override": (
                0 if (prev.get("override_start_total") is not None
                      and total < int(prev.get("override_start_total") or 0))
                else prev.get("manual_override", 0)
            ),
            "override_color": (
                None if (prev.get("override_start_total") is not None
                         and total < int(prev.get("override_start_total") or 0))
                else prev.get("override_color")
            ),
            "override_base_level": prev.get("override_base_level"),
            "override_start_total": (
                None if (prev.get("override_start_total") is not None
                         and total < int(prev.get("override_start_total") or 0))
                else prev.get("override_start_total")
            ),
            "override_start_toner": prev.get("override_start_toner"),
            "yield_per_page": prev.get("yield_per_page", 2000),
        })
        return

    if total < prev_total and reboot_detected and not strong_reset_evidence:
        # کاهش شمارنده همراه با ریبوت ولی بدون شواهد قوی ریست واقعی (منابع SNMP
        # متفاوت/خواندن ناقص). فقط anomaly ثبت می‌کنیم و snapshot را حفظ می‌کنیم.
        add_event(ip, "COUNTER_ANOMALY", {
            "message": (
                f"کاهش شمارنده ({prev_total:,} → {total:,}) با ریبوتِ دستگاه ولی "
                "بدون شواهد ریست واقعی شمارنده؛ snapshot قبلی حفظ شد"
            ),
            "severity": "warning",
            "prev_total": prev_total,
            "current_total": total,
            "delta": total - prev_total,
            "prev_uptime": prev_uptime,
            "current_uptime": uptime,
            "is_reboot": True,
        })
        store._prev.set(ip, {
            "print_total": prev_total,
            "full_color": prev_fc,
            "black_white": prev_bw,
            "toner_level": current_toner_level if current_toner_level is not None else prev_toner_level,
            "alert_codes": curr_codes,
            "last_alert_codes": curr_codes,
            "uptime": uptime if uptime is not None else prev_uptime,
            "a3_total": prev.get("a3_total"),
            "a4_total": prev.get("a4_total"),
            "last_counter_error": "COUNTER_ANOMALY",
            "reset_prev_total": prev.get("reset_prev_total"),
        })
        return

    actual_delta = total - prev_total
    delta_pages = actual_delta if actual_delta >= 0 else 0

    # ─── بازگشت شمارنده پس از ریبوت — باگ واقعی 172.16.25.36 (e-STUDIO257) ───
    # این مدل بلافاصله پس از ریبوت چند دقیقه total=0 را گزارش می‌کند و بعد مقدار
    # دائمی NVRAM بازمی‌گردد (مشاهده‌ی واقعی: 20,575→0→20,576). نسخه‌های قبلی
    # بازگشت را «چاپ جدید» می‌دانستند و PRINT_GAP عظیم ثبت می‌کردند (۶۱,۸۱۳
    # صفحه‌ی تخمینی در ۶ روز!). قانون: اگر ریست اخیر ثبت شده (reset_prev_total)
    # و شمارنده به نزدیکی همان مرجع بازگشت، این بازگشت شمارنده است نه چاپ —
    # رویداد اطلاعاتی COUNTER_RESTORED ثبت و نرمال با baseline جدید ادامه می‌دهیم.
    reset_ref = _to_int(prev.get("reset_prev_total")) if prev.get("reset_prev_total") is not None else None
    if reset_ref is not None and total is not None and total > 0:
        restore_tol = max(200, int(reset_ref * 0.005))
        if reset_ref - 10 <= total <= reset_ref + restore_tol:
            add_event(ip, "COUNTER_RESTORED", {
                "message": f"شمارنده پس از ریبوت به مقدار دائمی بازگشت ({total:,})؛ به‌عنوان چاپ جدید محاسبه نمی‌شود",
                "severity": "info",
                "prev_total": prev_total,
                "restored_total": total,
                "reset_reference": reset_ref,
            })
            log.warning("  [%s] counter restored after reboot: %s→%s (not printing; baseline=%s)",
                        ip, prev_total, total, reset_ref)
            store._prev.set(ip, {
                "print_total": total,
                "full_color": full_color if full_color is not None else prev_fc,
                "black_white": black_white if black_white is not None else prev_bw,
                "toner_level": current_toner_level if current_toner_level is not None else prev_toner_level,
                "alert_codes": curr_codes,
                "last_alert_codes": curr_codes,
                "uptime": uptime if uptime is not None else prev_uptime,
                "a3_total": a3_total if a3_total is not None else prev.get("a3_total"),
                "a4_total": a4_total if a4_total is not None else prev.get("a4_total"),
                "pending_overflow_total": None,
                "pending_overflow_delta": None,
                "reset_prev_total": None,
                "last_counter_error": None,
            })
            return
        if total > reset_ref + restore_tol:
            # از باند بازگشت گذشت (چاپ واقعی پس از ریست) — پرچم را بردار و ادامه بده
            reset_ref = None

    # ─── ضدنویز سطح تونر (پایین‌پرش تک‌پالی) ─────────────────────────
    # ✅ داده‌ی واقعی: TOSHIBA e-STUDIO306 روی 172.16.0.40 — سطح واقعی سیاه ۱۰۰٪
    # پایدار بود (ران ۴۴/۱۵/۲۸۰ پالی) ولی هر از چندگاهی *یک* poll عدد ۸٪ می‌خواند
    # و دوباره ۱۰۰٪؛ هر blip یک کاندیدای ساختگی REFILL می‌ساخت. افت >۲۵ واحد در
    # یک poll با <۵ صفحه چاپ از نظر فیزیکی غیرممکن است (کارتریج‌های چندهزارصفحه‌ای)
    # پس چنین خواندنی نویز است: مقدار قبلی نگه داشته می‌شود و وارد منطق
    # REFILL/یادگیری yield نمی‌شود.
    if (prev_toner_level is not None and current_toner_level is not None
            and 0 <= actual_delta < 5):
        try:
            if int(prev_toner_level) - int(current_toner_level) > 25:
                log.info("  [%s] خواندن نویزی تونر رد شد: %s%% → %s%% با %s صفحه چاپ (مقدار قبلی نگه داشته شد)",
                         ip, prev_toner_level, current_toner_level, actual_delta)
                current_toner_level = prev_toner_level
        except (TypeError, ValueError):
            pass

    # ─── REFILL خودکار دو مرحله‌ای: با یک poll قطعی ثبت نکن ─────────
    refill_confirmed = False
    pending_refill_new = prev.get("pending_refill_new_toner")
    pending_refill_prev = prev.get("pending_refill_prev_toner")
    pending_refill_total = prev.get("pending_refill_total", prev_total)
    pending_refill_hits = int(prev.get("pending_refill_hits") or 0)
    if current_toner_level is not None and pending_refill_new is not None and not prev.get("manual_override"):
        try:
            pages_since_candidate = total - int(pending_refill_total or prev_total)
            if int(current_toner_level) >= int(pending_refill_new) - 1 and pages_since_candidate < 50:
                pending_refill_hits += 1
                # ✅ فیکس FLAP (داده‌ی واقعی: TOSHIBA e-STUDIO306 روی 172.16.0.40/41
                # با پرش متناوب 8↔100 در pollهای پشت‌سر → ۴۸ REFILL جعلی در ۶ روز):
                # یک تأییدِ پشت‌سرهم کافی نیست چون نویز گاهی دو poll متوالی بالا
                # می‌ایستد. سطح جدید باید در ۲ poll متوالی پایدار بماند تا شارژ
                # تایید شود (تاخیر ≈ چند دقیقه — برای شارژ واقعی هزینه‌ای ندارد).
                if pending_refill_hits >= 2:
                    refill_confirmed = True
                    add_event(ip, "REFILL", {
                        "message": f"تشخیص خودکار تاییدشده: کارتریج شارژ شد (تونر از {pending_refill_prev}% به {current_toner_level}%)",
                        "severity": "info",
                        "auto_detected": True,
                        "confirmed": True,
                        "prev_toner": pending_refill_prev,
                        "new_toner": current_toner_level,
                        "delta_pages": max(0, pages_since_candidate),
                    })
        except (TypeError, ValueError):
            refill_confirmed = False

    if (not refill_confirmed and current_toner_level is not None and prev_toner_level is not None and actual_delta >= 0):
        try:
            delta_toner = int(current_toner_level) - int(prev_toner_level)
        except (TypeError, ValueError):
            delta_toner = 0
        if (delta_toner > 20 and delta_pages < 50 and not prev.get("manual_override")):
            log.info("  [%s] refill candidate pending: toner %s%% → %s%%", ip, prev_toner_level, current_toner_level)

        _learn_yield_per_page(ip, delta_pages, prev_toner_level, current_toner_level, prev)

    if (delta_pages > 500 and current_toner_level is not None and prev_toner_level is not None and
        abs(int(current_toner_level) - int(prev_toner_level)) < 5):
        add_event(ip, "WARNING", {
            "message": f"هشدار: {delta_pages} صفحه چاپ شده ولی تونر تغییر نکرده ({prev_toner_level}% → {current_toner_level}%). احتمال گیر کردن چیپ.",
            "severity": "warning",
            "auto_detected": True,
        })

    # ─── محاسبه و اعتبارسنجی دلتاهای رنگی/سیاه‌وسفید ───────────────
    delta_fc = (full_color - prev_fc) if (full_color is not None and prev_fc is not None) else 0
    delta_bw = (black_white - prev_bw) if (black_white is not None and prev_bw is not None) else 0
    split_delta = delta_fc + delta_bw
    total_delta = split_delta
    color_unknown = False
    counter_mismatch = False

    if actual_delta > 0:
        partial_negative = delta_fc < 0 or delta_bw < 0
        mismatch = split_delta > 0 and abs(split_delta - actual_delta) > max(2, int(actual_delta * 0.10))
        if partial_negative or mismatch or split_delta <= 0:
            if actual_delta <= dynamic_max_delta:
                total_delta = actual_delta
                split_unavailable = full_color is None and black_white is None
                color_unknown = partial_negative or mismatch or split_unavailable
                counter_mismatch = partial_negative or mismatch
                delta_fc = 0
                delta_bw = actual_delta
                if counter_mismatch:
                    log.warning(
                        "  [%s] split counter mismatch; using total delta. actual=%s split=%s fc=%s bw=%s",
                        ip, actual_delta, split_delta, delta_fc, delta_bw,
                    )
            else:
                total_delta = actual_delta
    else:
        total_delta = 0

    log.debug(f"  [{ip}] PRINT: prev_total={prev_total:,} curr_total={total:,} "
              f"delta_fc={delta_fc}, delta_bw={delta_bw}, total_delta={total_delta}, actual_delta={actual_delta}")

    # ─── Overflow: بار اول فقط pending، بار دوم تایید و ثبت تخمینی ───
    estimated = False
    if total_delta > dynamic_max_delta:
        pending_total = prev.get("pending_overflow_total")
        pending_delta = prev.get("pending_overflow_delta")
        if pending_total == total and pending_delta == total_delta:
            estimated = True
            color_unknown = True
            log.warning("  [%s] overflow confirmed on second poll; recording estimated PRINT delta=%s", ip, total_delta)
            # ✅ فیکس: رویداد برآوردی overflow (معمولاً به‌خاطر قطعی مانیتور یا
            # نوسان منبع شمارنده) نباید در آمار مصرف روزانه به‌عنوان چاپ معمولی
            # جمع خورد؛ نوع جداگانه PRINT_GAP ثبت می‌شود و snapshot جلو می‌رود.
            gap_paper = paper_size
            add_event(ip, "PRINT_GAP", {
                "message": (
                    f"جبران فاصله پایش: {total_delta} صفحه نسبت به آخرین snapshot "
                    "(احتمالاً چاپ در زمان قطعی مانیتور یا نوسان خواندن شمارنده؛ تخمینی)"
                ),
            "pages": total_delta,
            "color": "نامشخص",
            "paper_size": gap_paper,
            "severity": "warning",
            "estimated": True,
            "overflow_confirmed": True,
            "gap_backfill": True,
            **({"paper_split": paper_split} if paper_split and (paper_split.get("large") or paper_split.get("small")) else {}),
            **({"paper_detail": paper_detail} if paper_detail else {}),
            **({"func_split": func_split} if func_split else {}),
                "prev_total": prev_total,
                "current_total": total,
                "dynamic_limit": dynamic_max_delta,
                **({"poll_timestamp": poll_timestamp} if poll_timestamp else {}),
            })
            store._prev.set(ip, {
                "print_total": total,
                "full_color": full_color if full_color is not None else prev_fc,
                "black_white": black_white if black_white is not None else prev_bw,
                "toner_level": current_toner_level if current_toner_level is not None else prev_toner_level,
                "alert_codes": curr_codes,
                "last_alert_codes": curr_codes,
                "uptime": uptime if uptime is not None else prev_uptime,
                "a3_total": a3_total if a3_total is not None else prev.get("a3_total"),
                "a4_total": a4_total if a4_total is not None else prev.get("a4_total"),
                "pending_overflow_total": None,
                "pending_overflow_delta": None,
                "last_counter_error": None,
            })
            return
        else:
            add_event(ip, "PRINT_OVERFLOW", {
                "message": f"افزایش غیرمنتظره صفحات: {total_delta} صفحه در یک بازه؛ برای جلوگیری از لاگ دروغین در حالت pending نگه داشته شد",
                "severity": "warning",
                "delta": total_delta,
                "dynamic_limit": dynamic_max_delta,
                "elapsed_seconds": round(elapsed_seconds, 1),
                "prev_total": prev_total,
                "current_total": total,
                "pending_confirmation": True,
            })
            store._prev.set(ip, {
                "print_total": prev_total,
                "full_color": prev_fc,
                "black_white": prev_bw,
                "toner_level": current_toner_level if current_toner_level is not None else prev_toner_level,
                "alert_codes": curr_codes,
                "last_alert_codes": curr_codes,
                "uptime": uptime if uptime is not None else prev_uptime,
                "a3_total": prev.get("a3_total"),
                "a4_total": prev.get("a4_total"),
                "pending_overflow_total": total,
                "pending_overflow_delta": total_delta,
                "reset_prev_total": prev.get("reset_prev_total"),
            })
            return

    # ─── ثبت PRINT ────────────────────────────────────────────────
    if total_delta > 0:
        if color_unknown:
            msg = f"{total_delta} صفحه چاپ شد (تفکیک رنگ نامطمئن)"
            color = "نامشخص"
        elif delta_fc > 0 and delta_bw > 0:
            msg = f"{delta_fc} صفحه رنگی + {delta_bw} صفحه سیاه‌سفید = {total_delta} صفحه چاپ شد"
            color = "مختلط"
        elif delta_fc > 0:
            msg = f"{delta_fc} صفحه رنگی چاپ شد"
            color = "رنگی"
        else:
            msg = f"{total_delta} صفحه سیاه‌سفید چاپ شد"
            color = "سیاه‌سفید"
        if estimated:
            msg = "ثبت تخمینی پس از تایید overflow: " + msg

        event_data = {
            "message": msg,
            "pages": total_delta,
            "color": color,
            "paper_size": paper_size,
            "severity": "warning" if estimated or counter_mismatch else "info",
            # ✅ انتساب حسابرسی: مقادیر شمارنده‌ی دو سر دلتا در ذات رویداد ذخیره
            # می‌شود (مانند GAP/RESET) تا ممیزی پنجره‌ای بعد از شکاف پایش هم
            # دقیق بماند و tooltip «منبع عدد» اعداد واقعی را نشان دهد.
            "prev_total": prev_total,
            "current_total": total,
        }
        # ✅ تفکیک دقیق دسته‌ی کاغذ: در حالت Mixed (و PRINT_GAP) فقط برچسب کلی
        # کافی نیست؛ تعداد دقیق هر دسته در details ذخیره می‌شود تا لاگ قابل حسابرسی باشد.
        if paper_split and (paper_split.get("large") or paper_split.get("small")):
            event_data["paper_split"] = paper_split
        # ✅ حسابرسی کامل‌تر (گزارش کاربر از فلت واقعی): تفکیک خانواده‌ی سایز
        # به‌همراه زیرگروه «پرینت رایانه‌ای» و تفکیک عملکرد (ضبط در details و
        # مرج خودکار در API از طریق _row_to_dict) — مخصوصاً برای پاسخ به سؤال
        # «چند صفحه A3 چاپ شد / چه کسی کپی گرفت» بدون حدس.
        if paper_detail:
            event_data["paper_detail"] = paper_detail
        if func_split:
            event_data["func_split"] = func_split
        if estimated:
            event_data["estimated"] = True
            event_data["overflow_confirmed"] = True
        if counter_mismatch:
            event_data["counter_mismatch"] = True
            event_data["actual_delta"] = actual_delta
            event_data["split_delta"] = split_delta
        if username:
            event_data["username"] = username
        if poll_timestamp:
            event_data["poll_timestamp"] = poll_timestamp

        add_event(ip, "PRINT", event_data)
        log.info(f"  [{ip}] ✓ ثبت چاپ: {total_delta} صفحه ({color})")

    # ─── ذخیره snapshot جدید؛ counterهای جزئی عقب‌گرد نکنند ─────────
    safe_fc = full_color if (full_color is not None and (prev_fc is None or full_color >= prev_fc)) else prev_fc
    safe_bw = black_white if (black_white is not None and (prev_bw is None or black_white >= prev_bw)) else prev_bw

    new_prev = {
        "print_total": total,
        "full_color": safe_fc,
        "black_white": safe_bw,
        "toner_level": current_toner_level if current_toner_level is not None else prev_toner_level,
        "alert_codes": curr_codes,
        "last_alert_codes": curr_codes,
        "uptime": uptime if uptime is not None else prev_uptime,
        "pending_overflow_total": None,
        "pending_overflow_delta": None,
        "reset_prev_total": reset_ref,
        "last_counter_error": None,
    }
    if current_toner_level is not None and prev_toner_level is not None:
        try:
            if int(current_toner_level) - int(prev_toner_level) > 20 and delta_pages < 50 and not refill_confirmed:
                new_prev["pending_refill_prev_toner"] = prev_toner_level
                new_prev["pending_refill_new_toner"] = current_toner_level
                new_prev["pending_refill_total"] = total
                new_prev["pending_refill_hits"] = 0
            elif (not refill_confirmed and pending_refill_new is not None
                  and int(current_toner_level) >= int(pending_refill_new) - 1):
                # پنجره‌ی پایداری: کاندید و شمارنده‌ی hits نگه داشته می‌شوند تا
                # تأیید دو‌پالی کامل شود (فیکس FLAP — نباید این‌جا pending پاک شود)
                new_prev["pending_refill_prev_toner"] = pending_refill_prev
                new_prev["pending_refill_new_toner"] = pending_refill_new
                new_prev["pending_refill_total"] = pending_refill_total
                new_prev["pending_refill_hits"] = pending_refill_hits
            else:
                new_prev["pending_refill_prev_toner"] = None
                new_prev["pending_refill_new_toner"] = None
                new_prev["pending_refill_total"] = None
                new_prev["pending_refill_hits"] = None
        except (TypeError, ValueError):
            pass
    if a3_total is not None:
        new_prev["a3_total"] = a3_total
    if a4_total is not None:
        new_prev["a4_total"] = a4_total
    # ✅ پرچم «عقب‌افتادگی» شمارنده‌های کاغذ: اگر این poll یکی از آن‌ها خوانده
    # نشده، دلتای poll بعدی چندبازه‌ای است و attribute_paper_size باید مازاد را
    # از همین مؤلفه کم کند (نه از مؤلفه‌ی درست).
    new_prev["a3_lagged"] = a3_total is None
    new_prev["a4_lagged"] = a4_total is None
    store._prev.set(ip, new_prev)

# ─── نسبت‌دادن سایز کاغذ به رویداد چاپ ──────────────────────────
# برچسب‌های دسته‌بندی سایز. شاخه‌های Toshiba ماهیتاً دسته‌اند نه سایز دقیق:
#   شاخه‌ی ۲۰۷ = کل کاغذ «بزرگ» (A3+B4+...)، شاخه‌ی ۲۰۸ = کل کاغذ «کوچک» (A4+A5+...)
PAPER_SIZE_LARGE_LABEL = "Large (A3/B4)"
PAPER_SIZE_SMALL_LABEL = "Small (A4/A5)"
PAPER_SIZE_MIXED_LABEL = "Mixed"


def attribute_paper_size(total, prev_total, a3_total, prev_a3, a4_total, prev_a4, ip: str = "",
                         a3_lagged: bool = False, a4_lagged: bool = False):
    """
    برچسب سایز کاغذ را از دلتای شمارنده‌های کاغذ بزرگ/کوچک استخراج می‌کند.

    ✅ فیکس باگ «سایز اشتباه در لاگ»: روی Toshiba هویتِ
    a3_total + a4_total == print_total اثبات شده است؛ پس جمع دلتای دو شاخه
    باید با دلتای شمارنده کل برابر باشد. قبلاً بدون این اعتبارسنجی برچسب
    گذاشته می‌شد و در دو سناریو «مطمئن اما اشتباه» بود:
      ۱) خواندن ناموفق یکی از شمارنده‌ها در poll قبل (snapshot کهنه → دلتای
         چندبازه‌ای)،
      ۲) مسمومیت baseline با مقدار ۰ در مسیر legacy وقتی خواندن SNMP fail
         می‌شد (si(...)=0 در PrevStore ذخیره می‌شد و دلتای بعدی = کل شمارنده!).
    در این موارد حالا هشدار می‌دهیم و None برمی‌گردانیم (سایز نامشخص) به‌جای
    برچسب اشتباه. در حالت «Mixed» تعداد دقیق هر دسته در paper_split برگردانده
    می‌شود تا در جزئیات رویداد ثبت شود.

    Returns:
        (paper_size: str|None, paper_split: {"large": int, "small": int}, reliable: bool)
    """
    zero_split = {"large": 0, "small": 0}

    def _as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    a3 = _as_int(a3_total)
    a4 = _as_int(a4_total)
    pa3 = _as_int(prev_a3)
    pa4 = _as_int(prev_a4)
    if a3 is None or a4 is None or pa3 is None or pa4 is None:
        # داده ناقص (اولین poll یا خواندن ناموفق) — بدون حدس برمی‌گردانیم.
        return None, zero_split, False

    delta_a3 = a3 - pa3
    delta_a4 = a4 - pa4
    if delta_a3 < 0 or delta_a4 < 0:
        log.warning(
            "  [%s] paper counters moved backwards (a3 %s→%s, a4 %s→%s); paper size unreliable",
            ip, pa3, a3, pa4, a4,
        )
        return None, zero_split, False

    if delta_a3 == 0 and delta_a4 == 0:
        return None, zero_split, True

    expected = None
    total_i = _as_int(total)
    prev_total_i = _as_int(prev_total)
    if total_i is not None and prev_total_i is not None:
        expected = total_i - prev_total_i

    if expected is not None:
        # شکاف بین snapshot شمارنده کل و snapshot کاغذ = نسبت‌دهی نامعتبر
        tol = max(2, int(0.05 * max(expected, 1)))
        if abs((delta_a3 + delta_a4) - expected) > tol:
            log.warning(
                "  [%s] paper delta misaligned: a3+a4=%s but total delta=%s; "
                "skipping paper-size attribution (stale baseline)",
                ip, delta_a3 + delta_a4, expected,
            )
            return None, zero_split, False

        # ✅ جذب خطای گردش/تأخیر شمارنده در داخل تلورانس: اگر جمع تفکیک کمی
        # بیشتر از دلتای واقعی است (مثلاً یک صفحه‌ی بازه‌ی قبلی که آن موقع
        # شمارنده‌اش خوانده نشده بود)، مازاد از مؤلفه‌ی «عقب‌مانده» کم می‌شود
        # (پرچم lagged) تا paper_split هرگز از pages رویداد بیشتر نشود.
        # بدون پرچم، heuristic: مازاد معمولاً در مؤلفه‌ی کوچک‌ترِ دیررس است.
        # حالت کمبود (شمارنده‌ای که عقب‌مانده) را پر نمی‌کنیم — جعل نسبت‌دهی نمی‌کنیم.
        if expected >= 0:
            overflow = (delta_a3 + delta_a4) - expected
            if overflow > 0:
                if a3_lagged and not a4_lagged:
                    delta_a3 = max(0, delta_a3 - overflow)
                elif a4_lagged and not a3_lagged:
                    delta_a4 = max(0, delta_a4 - overflow)
                elif delta_a3 <= delta_a4:
                    delta_a3 = max(0, delta_a3 - overflow)
                else:
                    delta_a4 = max(0, delta_a4 - overflow)

    if delta_a3 == 0 and delta_a4 == 0:
        return None, zero_split, True

    split = {"large": delta_a3, "small": delta_a4}
    if delta_a3 > 0 and delta_a4 == 0:
        return PAPER_SIZE_LARGE_LABEL, split, True
    if delta_a4 > 0 and delta_a3 == 0:
        return PAPER_SIZE_SMALL_LABEL, split, True
    return PAPER_SIZE_MIXED_LABEL, split, True


# ─── سازگاری شمارنده‌ها ─────────────────────────────────────────
def validate_counter_consistency(counters: dict, brand: str) -> list:
    warnings = []
    total   = counters.get("total",       0) or 0
    color   = counters.get("full_color",  0) or 0
    bw      = counters.get("black_white", 0) or 0
    copy_   = counters.get("copy",        0) or 0
    printer = counters.get("printer",     0) or 0

    if brand == "toshiba" and total > 0:
        twin_ = counters.get("twin", 0) or 0
        if color + bw > 0:
            diff = abs(total - (color + bw + twin_))
            if diff > max(100, total * 0.01):
                warnings.append(
                    f"⚠ Toshiba: fc({color:,})+bw({bw:,})+twin({twin_:,})={color+bw+twin_:,} ≠ total({total:,}) diff={diff:,}"
                )
        copy_fc = counters.get("copy_fc",   0) or 0
        ptr_fc  = counters.get("printer_fc",0) or 0
        if color > 0 and (copy_fc + ptr_fc) > color + 1000:
            warnings.append(
                f"⚠ Toshiba FC: copy_fc({copy_fc:,})+ptr_fc({ptr_fc:,})={copy_fc+ptr_fc:,} > fc_total({color:,})"
            )

    if brand == "canon" and total > 0 and copy_ > 0 and printer > 0:
        diff = abs(total - (copy_ + printer))
        if diff > max(300, total * 0.01):
            warnings.append(
                f"⚠ Canon: copy({copy_:,})+print({printer:,})={copy_+printer:,} ≠ total({total:,}) diff={diff:,}"
            )

    return warnings


# ─── تشخیص برند ─────────────────────────────────────────────────
def fetch_first_web_page(ip: str, urls, timeout: float = 4.0):
    """اولین صفحه‌ی وبِ قابل‌خواندن از پنل دستگاه را برمی‌گرداند.

    برای fallbackهای اسکریپ تونر (Canon/Brother/HP) وقتی SNMP عدد نمی‌دهد.
    - ابتدا requests (اگر نصب باشد) و در غیر این صورت urllib
    - HTTPS هم پشتیبانی می‌شود؛ گواهی‌های خودامضای چاپگرها پذیرفته می‌شوند
      (بسیاری HPها پورت ۸۰ را می‌بندند و فقط روی ۴۴۳ پاسخ می‌دهند).
    خروجی: (url, html) یا (None, None)
    """
    try:
        import requests as _req
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
        for url in urls:
            try:
                resp = _req.get(url, timeout=timeout,
                                headers={"User-Agent": "PrinterMonitor/1.0"},
                                allow_redirects=True, verify=False)
                if resp.status_code == 200 and resp.text:
                    return url, resp.text
            except Exception:
                continue
    except ImportError:
        pass
    try:
        import urllib.request as _ur
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        for url in urls:
            try:
                req = _ur.Request(url, headers={"User-Agent": "PrinterMonitor/1.0"})
                with _ur.urlopen(req, timeout=timeout, context=ctx) as resp:
                    data = resp.read(300_000).decode("utf-8", errors="replace")
                    if data:
                        return url, data
            except Exception:
                continue
    except Exception:
        pass
    return None, None


def detect_brand(ip: str, community: str) -> str:
    sys_oid  = snmp_get_with_fallback(ip, "1.3.6.1.2.1.1.2.0", community, timeout=2.0)
    sys_desc = str(snmp_get_with_fallback(ip, "1.3.6.1.2.1.1.1.0", community, timeout=2.0) or "").lower()
    oid_str  = str(sys_oid) if sys_oid else ""

    if "ecs100g" in sys_desc or "1.3.6.1.4.1.47206" in oid_str:
        return "sensor"
    # برخی ECS100Gها sysDescr استاندارد نمی‌دهند؛ با OIDهای اختصاصی هم probe می‌کنیم.
    for sensor_oid in (
        "1.3.6.1.4.1.47206.1.0",
        "1.3.6.1.4.1.47206.110.1.2.0",
        "1.3.6.1.4.1.47206.111.1.2.0",
    ):
        if snmp_get_with_fallback(ip, sensor_oid, community, timeout=1.5) is not None:
            return "sensor"
    if "1.3.6.1.4.1.1129" in oid_str or "toshiba"   in sys_desc: return "toshiba"
    if "1.3.6.1.4.1.1602" in oid_str or "canon"     in sys_desc: return "canon"
    if "1.3.6.1.4.1.2435" in oid_str or "brother"   in sys_desc: return "brother"
    if ("1.3.6.1.4.1.11"   in oid_str or
            "jetdirect" in sys_desc or
            "hp " in sys_desc or
            "hewlett" in sys_desc or
            "laserjet" in sys_desc or
            "officejet" in sys_desc or
            "pagewide" in sys_desc):
        return "hp"
    return "unknown"