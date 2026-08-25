"""
Yield Engine
============

محاسبه اصولی ظرفیت کارتریج (yield_per_page) به‌صورت per-cartridge/per-color.

ویژگی‌ها:
- anchor-based learning برای پرینترهای کم‌مصرف
- پشتیبانی از کارتریج‌های رنگی
- confidence/source برای هر کارتریج
- snapshot و sample قابل audit
- انتشار yield معتبر به کارتریج‌های هم‌مدل و هم‌نام

این ماژول عمداً مستقل از منطق قدیمی `printer_counters.yield_per_page` نگه داشته شده
تا migration بدون ریسک انجام شود و داده قبلی خراب نشود.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from core.database import db_connection

log = logging.getLogger("PrinterMonitor")

DEFAULT_YIELD_PER_PAGE = 2000
MIN_PAGES_FOR_SAMPLE = 10
MIN_ESTIMATED_YIELD = 300
MAX_ESTIMATED_YIELD = 100000
REFILL_JUMP_PERCENT = 20
SMALL_LEVEL_BOUNCE_PERCENT = 5
SNAPSHOT_MIN_SECONDS = 6 * 3600
CATALOG_ENV_VAR = "CARTRIDGE_YIELD_CATALOG"
DEFAULT_CATALOG_FILE = Path(__file__).resolve().parents[1] / "cartridge_yield_catalog.json"

# ─── قوانین ضد-نویز و اصولی‌سازی ظرفیت (داده‌ی واقعی: لاگ ۶ روزه‌ی فلت) ──────
# یک poll تکی که سطح را بیش از این مقدار پایین می‌آورد ولی تقریباً هیچ صفحه‌ای
# چاپ نشده، از نظر فیزیکی غیرممکن است (کارتریج‌های چندهزارصفحه‌ای) و نویز
# خواندن است — مورد واقعی: TOSHIBA e-STUDIO306 با blipهای 100→8→100 که
# cycle_learn را با yieldهای ۳۰۰-۴۰۰ صفحه مسموم می‌کرد.
LEVEL_NOISE_DROP_PERCENT = 25
LEVEL_NOISE_MAX_PAGES = 5
# گام ۱٪یِ کوانتیزه‌شده با صفحات کم، خطای تخمین yield را انفجاری می‌کند
# (مورد واقعی: Δp=11/Δt=1 → yield=1100 برای Cartridge 737 با ظرفیت واقعی 2400).
QUANT_MIN_PAGES_1PCT = 150
# وقتی ظرفیت «مرجع» (کاتالوگ/اعلامی دستگاه) داریم، مقدار یادگرفته فقط در این
# باند اجازه‌ی نمایش دارد؛ خارج از آن = داده‌ی مسموم/نویز و به مقدار مرجع
# برمی‌گردانیم (self-heal برای stateهای قدیمی مسموم مثل yield=23800 روی T-4590).
LEARNED_DEVIATION_LIMIT = 0.40
CYCLE_RATED_BAND = (0.30, 2.5)

_COLOR_ORDER = ("black", "cyan", "magenta", "yellow", "drum")


def _now() -> str:
    return datetime.now().isoformat()


def _norm_text(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^a-z0-9آ-یءئؤإأۀة،._\- ]+", "", value)
    return value or "unknown"


def build_cartridge_key(printer_model: str, cartridge_name: str, color: str) -> str:
    """کلید اشتراک‌گذاری yield: هم مدل دستگاه، هم نام کارتریج، هم رنگ."""
    return f"{_norm_text(printer_model)}|{_norm_text(cartridge_name)}|{_norm_text(color)}"


def _to_int(value, default=None):
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _valid_level(value) -> Optional[int]:
    level = _to_int(value)
    if level is None or level < 0 or level > 100:
        return None
    return level


def _valid_device_capacity(value) -> Optional[int]:
    """ظرفیت اعلام‌شده توسط دستگاه؛ فقط مقادیر منطقی و بزرگ‌تر از درصد را می‌پذیریم."""
    capacity = _to_int(value)
    if capacity is None:
        return None
    # max=100 معمولاً یعنی درصد، نه ظرفیت صفحه. مقادیر خیلی بزرگ هم مشکوک‌اند.
    if 101 <= capacity <= MAX_ESTIMATED_YIELD:
        return capacity
    return None


def _parse_dt(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _catalog_path() -> Path:
    override = os.getenv(CATALOG_ENV_VAR, "").strip()
    return Path(override) if override else DEFAULT_CATALOG_FILE


def _load_yield_catalog() -> list:
    """کاتالوگ محلی ظرفیت کارتریج‌ها را می‌خواند. خرابی فایل نباید polling را متوقف کند."""
    path = _catalog_path()
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("entries", data if isinstance(data, list) else [])
        return entries if isinstance(entries, list) else []
    except Exception as exc:
        log.warning("Failed to load cartridge yield catalog %s: %s", path, exc)
        return []


def _contains_any(haystack: str, needles) -> bool:
    if not needles:
        return True
    haystack = _norm_text(haystack)
    if isinstance(needles, str):
        needles = [needles]
    for needle in needles:
        needle_norm = _norm_text(str(needle))
        if needle_norm and needle_norm != "unknown" and needle_norm in haystack:
            return True
    return False


def _catalog_entry_yield(entry: dict) -> Optional[int]:
    """انتخاب مقدار yield از کاتالوگ بر اساس شرایط بازار/مصرف.

    حالت‌ها با ENV قابل تنظیم‌اند:
      CARTRIDGE_YIELD_MODE=oem|compatible|refill|local
      CARTRIDGE_YIELD_FACTOR=0.70  # اعمال ضریب محافظه‌کارانه روی مقدار کاتالوگ

    اگر فیلد حالت انتخاب‌شده وجود نداشته باشد، به `yield_per_page` fallback می‌کند.
    """
    mode = os.getenv("CARTRIDGE_YIELD_MODE", "local").strip().lower() or "local"
    mode_keys = {
        "oem": ("yield_per_page_oem", "yield_per_page"),
        "compatible": ("yield_per_page_compatible", "yield_per_page_local", "yield_per_page"),
        "refill": ("yield_per_page_refill", "yield_per_page_local", "yield_per_page_compatible", "yield_per_page"),
        "local": ("yield_per_page_local", "yield_per_page_iran", "yield_per_page_refill", "yield_per_page_compatible", "yield_per_page"),
    }.get(mode, ("yield_per_page_local", "yield_per_page",))

    ypp = None
    for key in mode_keys:
        ypp = _valid_device_capacity(entry.get(key))
        if ypp:
            break
    if not ypp:
        return None

    try:
        factor = float(os.getenv("CARTRIDGE_YIELD_FACTOR", "1") or "1")
    except (TypeError, ValueError):
        factor = 1.0
    if factor > 0 and factor != 1:
        ypp = int(round(ypp * factor))
    return _valid_device_capacity(ypp)


def _catalog_lookup(printer_model: str, cartridge_name: str, color: str) -> Optional[dict]:
    """بهترین match در کاتالوگ را پیدا می‌کند.

    matching محافظه‌کارانه است: اگر شرط مدل/کارتریج/رنگ در entry آمده باشد، باید match شود.
    """
    model_norm = _norm_text(printer_model)
    cart_norm = _norm_text(cartridge_name)
    color_norm = _norm_text(color)
    best = None
    best_score = -1
    for entry in _load_yield_catalog():
        if not isinstance(entry, dict) or entry.get("enabled") is False:
            continue
        ypp = _catalog_entry_yield(entry)
        if not ypp:
            continue
        match = entry.get("match") or {}
        entry_color = _norm_text(match.get("color") or entry.get("color") or "")
        if entry_color and entry_color != "unknown" and entry_color != color_norm:
            continue
        if not _contains_any(model_norm, match.get("model_contains")):
            continue
        if not _contains_any(cart_norm, match.get("cartridge_contains")):
            continue
        score = 0
        if entry_color and entry_color != "unknown":
            score += 2
        if match.get("model_contains"):
            score += 3
        if match.get("cartridge_contains"):
            score += 5
        if score > best_score:
            best_score = score
            best = {
                "yield_per_page": ypp,
                "confidence": entry.get("confidence") or "medium",
                "catalog_id": entry.get("id") or entry.get("cartridge") or "catalog",
            }
    return best


def _confidence_rank(conf: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get((conf or "low").lower(), 0)


def ensure_yield_tables() -> None:
    """ساخت جدول‌های Yield Engine. چندباره و امن قابل اجراست."""
    with db_connection(commit=True) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cartridge_state (
                printer_ip TEXT NOT NULL,
                color TEXT NOT NULL,
                printer_model TEXT,
                cartridge_name TEXT,
                cartridge_key TEXT,
                current_level INTEGER,
                last_valid_level INTEGER,
                last_counter INTEGER,
                anchor_counter INTEGER,
                anchor_level INTEGER,
                anchor_timestamp TEXT,
                yield_per_page INTEGER DEFAULT 2000,
                yield_source TEXT DEFAULT 'default',
                confidence TEXT DEFAULT 'low',
                sample_count INTEGER DEFAULT 0,
                total_weight REAL DEFAULT 0,
                learned_at TEXT,
                last_refill_at TEXT,
                last_refill_counter INTEGER,
                pending_refill_level INTEGER,
                pending_refill_counter INTEGER,
                pending_refill_timestamp TEXT,
                cycle_start_counter INTEGER,
                cycle_start_level INTEGER,
                cycle_start_timestamp TEXT,
                zero_reached_counter INTEGER,
                zero_reached_timestamp TEXT,
                pages_after_zero INTEGER DEFAULT 0,
                cycle_status TEXT DEFAULT 'active',
                force_estimate INTEGER DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (printer_ip, color)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS yield_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printer_ip TEXT NOT NULL,
                color TEXT NOT NULL,
                printer_model TEXT,
                cartridge_name TEXT,
                cartridge_key TEXT,
                start_counter INTEGER,
                end_counter INTEGER,
                start_level INTEGER,
                end_level INTEGER,
                pages_delta INTEGER,
                toner_drop INTEGER,
                estimated_yield INTEGER,
                counter_basis TEXT,
                confidence_weight REAL,
                accepted INTEGER DEFAULT 1,
                reject_reason TEXT,
                source TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS toner_snapshots_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printer_ip TEXT NOT NULL,
                color TEXT NOT NULL,
                printer_model TEXT,
                cartridge_name TEXT,
                cartridge_key TEXT,
                timestamp TEXT NOT NULL,
                print_total INTEGER,
                full_color INTEGER,
                black_white INTEGER,
                usage_counter INTEGER,
                toner_level INTEGER,
                raw_level INTEGER,
                source TEXT,
                valid INTEGER DEFAULT 1,
                reject_reason TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cartridge_yield_profiles (
                cartridge_key TEXT PRIMARY KEY,
                printer_model TEXT,
                cartridge_name TEXT,
                color TEXT,
                yield_per_page INTEGER NOT NULL,
                confidence TEXT NOT NULL DEFAULT 'high',
                sample_count INTEGER DEFAULT 0,
                total_weight REAL DEFAULT 0,
                source_printer_ip TEXT,
                updated_at TEXT
            )
            """
        )
        # migration امن برای دیتابیس‌های قبلی
        for column_sql in (
            "ALTER TABLE cartridge_state ADD COLUMN cycle_start_counter INTEGER",
            "ALTER TABLE cartridge_state ADD COLUMN cycle_start_level INTEGER",
            "ALTER TABLE cartridge_state ADD COLUMN cycle_start_timestamp TEXT",
            "ALTER TABLE cartridge_state ADD COLUMN zero_reached_counter INTEGER",
            "ALTER TABLE cartridge_state ADD COLUMN zero_reached_timestamp TEXT",
            "ALTER TABLE cartridge_state ADD COLUMN pages_after_zero INTEGER DEFAULT 0",
            "ALTER TABLE cartridge_state ADD COLUMN cycle_status TEXT DEFAULT 'active'",
            "ALTER TABLE cartridge_state ADD COLUMN cartridge_serial TEXT",
            "ALTER TABLE cartridge_state ADD COLUMN last_cartridge_name TEXT",
            "ALTER TABLE cartridge_state ADD COLUMN replacement_detected_at TEXT",
            "ALTER TABLE toner_snapshots_v2 ADD COLUMN cartridge_serial TEXT",
            "ALTER TABLE cartridge_state ADD COLUMN cartridge_name_override TEXT",
        ):
            try:
                conn.execute(column_sql)
            except Exception:
                pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cartridge_state_key ON cartridge_state(cartridge_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_yield_samples_key ON yield_samples(cartridge_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_toner_snapshots_v2_ip_color_ts ON toner_snapshots_v2(printer_ip, color, timestamp)")


def _row_to_state(row) -> Optional[dict]:
    if not row:
        return None
    cols = [
        "printer_ip", "color", "printer_model", "cartridge_name", "cartridge_key",
        "current_level", "last_valid_level", "last_counter", "anchor_counter", "anchor_level",
        "anchor_timestamp", "yield_per_page", "yield_source", "confidence", "sample_count",
        "total_weight", "learned_at", "last_refill_at", "last_refill_counter",
        "pending_refill_level", "pending_refill_counter", "pending_refill_timestamp",
        "cycle_start_counter", "cycle_start_level", "cycle_start_timestamp",
        "zero_reached_counter", "zero_reached_timestamp", "pages_after_zero", "cycle_status",
        "force_estimate", "updated_at",
        "cartridge_serial", "last_cartridge_name", "replacement_detected_at",
        "cartridge_name_override",
    ]
    return dict(zip(cols, row))


def _load_state(conn, ip: str, color: str) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT printer_ip, color, printer_model, cartridge_name, cartridge_key,
               current_level, last_valid_level, last_counter, anchor_counter, anchor_level,
               anchor_timestamp, yield_per_page, yield_source, confidence, sample_count,
               total_weight, learned_at, last_refill_at, last_refill_counter,
               pending_refill_level, pending_refill_counter, pending_refill_timestamp,
               cycle_start_counter, cycle_start_level, cycle_start_timestamp,
               zero_reached_counter, zero_reached_timestamp, pages_after_zero, cycle_status,
               force_estimate, updated_at,
               cartridge_serial, last_cartridge_name, replacement_detected_at,
               cartridge_name_override
        FROM cartridge_state
        WHERE printer_ip = ? AND color = ?
        """,
        (ip, color),
    ).fetchone()
    return _row_to_state(row)


def _load_profile(conn, cartridge_key: str) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT cartridge_key, printer_model, cartridge_name, color, yield_per_page,
               confidence, sample_count, total_weight, source_printer_ip, updated_at
        FROM cartridge_yield_profiles
        WHERE cartridge_key = ?
        """,
        (cartridge_key,),
    ).fetchone()
    if not row:
        return None
    return {
        "cartridge_key": row[0],
        "printer_model": row[1],
        "cartridge_name": row[2],
        "color": row[3],
        "yield_per_page": row[4],
        "confidence": row[5],
        "sample_count": row[6],
        "total_weight": row[7],
        "source_printer_ip": row[8],
        "updated_at": row[9],
    }


def _upsert_state(conn, state: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO cartridge_state (
            printer_ip, color, printer_model, cartridge_name, cartridge_key,
            current_level, last_valid_level, last_counter, anchor_counter, anchor_level,
            anchor_timestamp, yield_per_page, yield_source, confidence, sample_count,
            total_weight, learned_at, last_refill_at, last_refill_counter,
            pending_refill_level, pending_refill_counter, pending_refill_timestamp,
            cycle_start_counter, cycle_start_level, cycle_start_timestamp,
            zero_reached_counter, zero_reached_timestamp, pages_after_zero, cycle_status,
            force_estimate, updated_at,
            cartridge_serial, last_cartridge_name, replacement_detected_at,
            cartridge_name_override
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            state.get("printer_ip"), state.get("color"), state.get("printer_model"),
            state.get("cartridge_name"), state.get("cartridge_key"), state.get("current_level"),
            state.get("last_valid_level"), state.get("last_counter"), state.get("anchor_counter"),
            state.get("anchor_level"), state.get("anchor_timestamp"), state.get("yield_per_page", DEFAULT_YIELD_PER_PAGE),
            state.get("yield_source", "default"), state.get("confidence", "low"), state.get("sample_count", 0),
            state.get("total_weight", 0.0), state.get("learned_at"), state.get("last_refill_at"),
            state.get("last_refill_counter"), state.get("pending_refill_level"), state.get("pending_refill_counter"),
            state.get("pending_refill_timestamp"), state.get("cycle_start_counter"), state.get("cycle_start_level"),
            state.get("cycle_start_timestamp"), state.get("zero_reached_counter"), state.get("zero_reached_timestamp"),
            state.get("pages_after_zero", 0), state.get("cycle_status", "active"),
            state.get("force_estimate", 0), state.get("updated_at"),
            state.get("cartridge_serial"), state.get("last_cartridge_name"), state.get("replacement_detected_at"),
            state.get("cartridge_name_override"),
        ),
    )


def _record_snapshot(conn, ip: str, color: str, printer_model: str, cartridge_name: str,
                     cartridge_key: str, timestamp: str, counters: dict,
                     usage_counter: Optional[int], level: Optional[int], raw_level,
                     source: str, valid: bool = True, reject_reason: str = None,
                     cartridge_serial: str = None) -> None:
    conn.execute(
        """
        INSERT INTO toner_snapshots_v2 (
            printer_ip, color, printer_model, cartridge_name, cartridge_key, timestamp,
            print_total, full_color, black_white, usage_counter, toner_level, raw_level,
            source, valid, reject_reason, cartridge_serial
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ip, color, printer_model, cartridge_name, cartridge_key, timestamp,
            counters.get("total"), counters.get("full_color"), counters.get("black_white"),
            usage_counter, level, raw_level, source, 1 if valid else 0, reject_reason,
            cartridge_serial,
        ),
    )


def _record_sample(conn, *, ip: str, color: str, printer_model: str, cartridge_name: str,
                   cartridge_key: str, start_counter, end_counter, start_level, end_level,
                   pages_delta, toner_drop, estimated_yield, counter_basis: str,
                   weight: float, accepted: bool, reject_reason: str = None,
                   source: str = "anchor") -> None:
    conn.execute(
        """
        INSERT INTO yield_samples (
            printer_ip, color, printer_model, cartridge_name, cartridge_key,
            start_counter, end_counter, start_level, end_level, pages_delta,
            toner_drop, estimated_yield, counter_basis, confidence_weight,
            accepted, reject_reason, source, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ip, color, printer_model, cartridge_name, cartridge_key,
            start_counter, end_counter, start_level, end_level, pages_delta,
            toner_drop, estimated_yield, counter_basis, weight,
            1 if accepted else 0, reject_reason, source, _now(),
        ),
    )


def _compute_usage_counter(color: str, counters: dict, device_type: str) -> Tuple[Optional[int], str, float]:
    """انتخاب counter مناسب برای هر رنگ + ضریب اعتماد."""
    total = _to_int(counters.get("total"))
    full_color = _to_int(counters.get("full_color"))
    black_white = _to_int(counters.get("black_white"))
    color = (color or "black").lower()
    device_type = (device_type or "unknown").lower()

    if color in ("cyan", "magenta", "yellow"):
        if full_color is not None:
            return full_color, "full_color", 1.0
        if total is not None:
            return total, "total_for_color", 0.45
        return None, "missing", 0.0

    if color == "black":
        # برای مشکی، total محافظه‌کارانه‌تر و معمولاً موجودتر است؛ در پرینترهای mono دقیق است.
        if total is not None:
            quality = 1.0 if device_type != "color" else 0.75
            return total, "total_for_black", quality
        if black_white is not None:
            return black_white, "black_white", 0.9
        return None, "missing", 0.0

    # drum/opc و مصرفی‌های غیرتونر: اگر level دارند، با total کم‌اعتماد track می‌شوند.
    if total is not None:
        return total, "total_for_supply", 0.5
    return None, "missing", 0.0


def _sample_weight(pages_delta: int, toner_drop: int, counter_quality: float) -> float:
    if pages_delta <= 0 or toner_drop <= 0 or counter_quality <= 0:
        return 0.0
    page_factor = min(1.0, max(0.2, pages_delta / 100.0))
    drop_factor = min(5.0, float(toner_drop))
    return round(page_factor * drop_factor * counter_quality, 3)


def _derive_confidence(sample_count: int, total_weight: float, counter_basis: str = "") -> str:
    # پرینترهای کم‌مصرف ممکن است هر sample فقط ۱٪ افت و حدود ۵۰ صفحه داشته باشند؛
    # بنابراین برای mono/black با counter قابل‌قبول، ۴ نمونه مستقل برای high کافی است.
    # برای رنگ‌های CMY وقتی فقط total_for_color داریم، high نمی‌دهیم چون counter اختصاصی نیست.
    basis = str(counter_basis or "")
    if basis == "full_color":
        # برای CMY حتی با counter رنگی، پوشش رنگ صفحات متغیر است؛ دیرتر high می‌دهیم.
        if sample_count >= 8 and total_weight >= 5.0:
            return "high"
    elif sample_count >= 4 and total_weight >= 2.0 and not basis.startswith("total_for_color"):
        return "high"
    if sample_count >= 2 and total_weight >= 1.0:
        return "medium"
    return "low"


def _blend_yield(old_yield: int, old_weight: float, estimated_yield: int, sample_weight: float) -> int:
    if old_yield is None or old_yield <= 0 or old_yield == DEFAULT_YIELD_PER_PAGE and old_weight <= 0:
        return int(estimated_yield)
    total_w = max(0.0, old_weight) + max(0.1, sample_weight)
    return int(round(((old_yield * max(0.0, old_weight)) + (estimated_yield * max(0.1, sample_weight))) / total_w))


def _robust_learned_yield(conn, ip: str, color: str, cartridge_key: str,
                          rated: Optional[int] = None, limit: int = 12):
    """تخمین مقاوم yield از روی نمونه‌های پذیرفته‌شده — میانه‌ی وزنیِ N نمونه‌ی اخیر.

    چرا میانه: میانگین وزنی متحرک با یک نمونه‌ی outlier (مثل cycle جعلی ۳۲۰ صفحه
    یا کوانتیزه‌ی ۳۸٬۶۰۰) تا هفته‌ها منحرف می‌ماند؛ میانه‌ی وزنی چنین نمونه‌هایی
    را بی‌اثر می‌کند. اگر ظرفیت مرجع (کاتالوگ/دستگاه) موجود باشد، نمونه‌های خیلی
    دور از آن (بیرون باند ۰٫۲۵× تا ۲٫۵×) اصلاً وارد محاسبه نمی‌شوند.

    خروجی: (yield یا None, وزن تجمیعی این مینی‌نمونه‌ها)
    """
    try:
        rows = conn.execute(
            """SELECT estimated_yield, confidence_weight FROM yield_samples
               WHERE accepted=1 AND estimated_yield IS NOT NULL
                 AND (cartridge_key = ? OR (printer_ip = ? AND color = ?))
               ORDER BY id DESC LIMIT ?""",
            (cartridge_key, ip, color, int(limit)),
        ).fetchall()
    except Exception as exc:
        log.debug("robust yield query failed: %s", exc)
        return None, 0.0
    pts = [(int(y), max(0.1, float(w or 0.1))) for y, w in rows if y]
    if rated:
        lo, hi = rated * 0.25, rated * 2.5
        pts = [p for p in pts if lo <= p[0] <= hi]
    if not pts:
        return None, 0.0
    pts.sort(key=lambda p: p[0])
    total_w = sum(w for _, w in pts)
    acc = 0.0
    for y, w in pts:
        acc += w
        if acc >= total_w / 2.0:
            return y, total_w
    return pts[-1]


def _upsert_profile_if_high(conn, state: dict) -> None:
    if state.get("confidence") != "high":
        return
    ypp = _to_int(state.get("yield_per_page"))
    if not ypp or ypp <= 0 or ypp == DEFAULT_YIELD_PER_PAGE:
        return

    cartridge_key = state.get("cartridge_key")
    existing = _load_profile(conn, cartridge_key)
    if existing:
        old_weight = float(existing.get("total_weight") or 0.0)
        new_weight = float(state.get("total_weight") or 0.0)
        old_samples = int(existing.get("sample_count") or 0)
        new_samples = int(state.get("sample_count") or 0)
        if existing.get("source_printer_ip") == state.get("printer_ip"):
            # همان دستگاه قبلاً profile را ساخته؛ profile را با وضعیت جدید جایگزین می‌کنیم
            # تا sample_count/weight با هر poll دوباره جمع نشود.
            total_weight = max(old_weight, new_weight)
            sample_count = max(old_samples, new_samples)
        elif old_weight > 0 and new_weight > 0:
            # دستگاه دیگری با همان مدل و نام کارتریج هم high شده؛ میانگین وزنی بگیریم.
            ypp = int(round(((existing["yield_per_page"] * old_weight) + (ypp * new_weight)) / (old_weight + new_weight)))
            total_weight = old_weight + new_weight
            sample_count = old_samples + new_samples
        else:
            total_weight = max(old_weight, new_weight)
            sample_count = max(old_samples, new_samples)
    else:
        total_weight = float(state.get("total_weight") or 0.0)
        sample_count = int(state.get("sample_count") or 0)

    conn.execute(
        """
        INSERT OR REPLACE INTO cartridge_yield_profiles (
            cartridge_key, printer_model, cartridge_name, color, yield_per_page,
            confidence, sample_count, total_weight, source_printer_ip, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'high', ?, ?, ?, ?)
        """,
        (
            cartridge_key, state.get("printer_model"), state.get("cartridge_name"),
            state.get("color"), ypp, sample_count, total_weight,
            state.get("printer_ip"), _now(),
        ),
    )

    # ثبت برای بقیه کارتریج‌های هم مدل و هم اسم؛ learned/high خودشان overwrite نمی‌شود.
    conn.execute(
        """
        UPDATE cartridge_state
        SET yield_per_page = ?, yield_source = 'shared_profile', confidence = 'high', updated_at = ?
        WHERE cartridge_key = ?
          AND NOT (yield_source = 'auto_learn' AND confidence = 'high')
        """,
        (ypp, _now(), cartridge_key),
    )


def _state_public_dict(state: dict, profile: Optional[dict] = None) -> dict:
    result = {
        "yield_per_page": _to_int(state.get("yield_per_page"), DEFAULT_YIELD_PER_PAGE),
        "yield_source": state.get("yield_source") or "default",
        "confidence": state.get("confidence") or "low",
        "sample_count": _to_int(state.get("sample_count"), 0),
        "total_weight": float(state.get("total_weight") or 0.0),
        "anchor_level": state.get("anchor_level"),
        "anchor_counter": state.get("anchor_counter"),
        "last_refill_at": state.get("last_refill_at"),
        "cycle_start_counter": state.get("cycle_start_counter"),
        "cycle_start_level": state.get("cycle_start_level"),
        "zero_reached_counter": state.get("zero_reached_counter"),
        "pages_after_zero": _to_int(state.get("pages_after_zero"), 0),
        "cycle_status": state.get("cycle_status") or "active",
        "cartridge_key": state.get("cartridge_key"),
        "cartridge_serial": state.get("cartridge_serial"),
        "last_cartridge_name": state.get("last_cartridge_name"),
        "replacement_detected_at": state.get("replacement_detected_at"),
        "cartridge_name_override": state.get("cartridge_name_override"),
    }
    if profile:
        result["shared_profile"] = {
            "yield_per_page": profile.get("yield_per_page"),
            "confidence": profile.get("confidence"),
            "sample_count": profile.get("sample_count"),
            "source_printer_ip": profile.get("source_printer_ip"),
        }
    return result


def _maybe_close_cycle(conn, state: dict, *, ip: str, color: str, printer_model: str,
                       cartridge_name: str, cartridge_key: str, usage_counter: int,
                       timestamp: str, counter_basis: str, source: str,
                       rated_capacity: Optional[int] = None) -> Optional[dict]:
    """اگر چرخه قبلی کامل شده باشد، از اختلاف counter دو شارژ yield واقعی بساز.

    این مسیر برای حالت‌هایی است که تونر روی 0% می‌ماند اما چاپ ادامه دارد؛ وقتی
    reset/refill بعدی رخ دهد، کل صفحات چرخه قبلی تبدیل به sample با confidence بالا می‌شود.
    """
    start_counter = _to_int(state.get("cycle_start_counter"))
    start_level = _to_int(state.get("cycle_start_level"), 100)
    if start_counter is None or usage_counter is None or usage_counter <= start_counter or start_level <= 0:
        return None
    pages_delta = usage_counter - start_counter
    estimated_yield = int(round(pages_delta * 100.0 / min(start_level, 100)))
    if estimated_yield < MIN_ESTIMATED_YIELD or estimated_yield > MAX_ESTIMATED_YIELD:
        _record_sample(
            conn, ip=ip, color=color, printer_model=printer_model, cartridge_name=cartridge_name,
            cartridge_key=cartridge_key, start_counter=start_counter, end_counter=usage_counter,
            start_level=start_level, end_level=100, pages_delta=pages_delta, toner_drop=start_level,
            estimated_yield=estimated_yield, counter_basis=counter_basis, weight=0,
            accepted=False, reject_reason="cycle_yield_out_of_range", source=source,
        )
        return None
    # ✅ باندِ سازگاری با ریتینگ مرجع (مورد واقعی: cycle جعلی ۳۲۰ صفحه روی
    # T-4590 با ریتینگ ~۳۶,۶۰۰ که از blip تشکیل شده بود): چرخه‌ای که چندین
    # برابر کوچک‌تر/بزرگ‌تر از مرجع است نمی‌تواند یک کارتریج واقعی باشد.
    if rated_capacity and not (CYCLE_RATED_BAND[0] * rated_capacity
                               <= estimated_yield <= CYCLE_RATED_BAND[1] * rated_capacity):
        log.info("  [%s/%s] cycle yield %s بیرون از باند مرجع %s — رد شد", ip, color,
                 estimated_yield, rated_capacity)
        _record_sample(
            conn, ip=ip, color=color, printer_model=printer_model, cartridge_name=cartridge_name,
            cartridge_key=cartridge_key, start_counter=start_counter, end_counter=usage_counter,
            start_level=start_level, end_level=100, pages_delta=pages_delta, toner_drop=start_level,
            estimated_yield=estimated_yield, counter_basis=counter_basis, weight=0,
            accepted=False, reject_reason="cycle_vs_rated_out_of_band", source=source,
        )
        return None
    weight = max(4.0, min(12.0, pages_delta / 100.0))
    _record_sample(
        conn, ip=ip, color=color, printer_model=printer_model, cartridge_name=cartridge_name,
        cartridge_key=cartridge_key, start_counter=start_counter, end_counter=usage_counter,
        start_level=start_level, end_level=100, pages_delta=pages_delta, toner_drop=start_level,
        estimated_yield=estimated_yield, counter_basis=counter_basis, weight=weight,
        accepted=True, reject_reason=None, source="cycle_learn",
    )
    return {
        "yield_per_page": estimated_yield,
        "yield_source": "cycle_learn",
        "confidence": "high",
        "sample_count": int(state.get("sample_count") or 0) + 1,
        "total_weight": float(state.get("total_weight") or 0.0) + weight,
        "learned_at": timestamp,
        "cycle_status": "closed",
    }



def detect_cartridge_replacement(state: dict, new_level, new_cartridge_name: str,
                                  usage_counter: int, cartridge_serial: str = None) -> bool:
    """تشخیص تعویض کارتریج از روی الگوی رفتاری (signature-based).

    چون سریال کارتریج در MIB استاندارد SNMP وجود ندارد، از ترکیب چند سیگنال استفاده می‌کنیم:

    سیگنال‌ها:
    1. تغییر نام کارتریج (قوی‌ترین سیگنال)
    2. سطح قبلی پایین + جهش به بالا
    3. صفحات کم از آخرین anchor
    4. سطح قبلی بسیار پایین (تقریباً تمام)

    آستانه: score >= 5 → تعویض واقعی
    """
    old_level = state.get("last_valid_level")
    old_name = state.get("last_cartridge_name") or state.get("cartridge_name")
    old_serial = state.get("cartridge_serial")
    old_counter = state.get("last_counter") or state.get("anchor_counter")

    if new_level is None or old_level is None:
        return False

    jump = new_level - old_level
    if jump < REFILL_JUMP_PERCENT:
        return False

    score = 0

    # سیگنال ۱: تغییر نام کارتریج (قوی‌ترین)
    new_name_norm = _norm_text(new_cartridge_name)
    old_name_norm = _norm_text(old_name) if old_name else ""
    if new_name_norm and old_name_norm and new_name_norm != old_name_norm:
        score += 5
    elif new_name_norm and old_name_norm and new_name_norm == old_name_norm:
        # نام ثابت = احتمالاً شارژ، نه تعویض
        score -= 2

    # سیگنال ۲: سطح قبلی پایین
    if old_level <= 20:
        score += 3
    elif old_level <= 40:
        score += 1

    # سیگنال ۳: صفحات کم از آخرین anchor (کارتریج نو = صفحات چاپ نشده)
    if old_counter is not None and usage_counter is not None:
        pages_since_anchor = usage_counter - old_counter
        if old_level <= 20 and pages_since_anchor < 50:
            score += 2

    # سیگنال ۴: سطح قبلی بسیار پایین (تقریباً تمام)
    if old_level <= 5:
        score += 2

    # سیگنال ۵: سریال تغییر کرده (اگر موجود باشد)
    if cartridge_serial and old_serial and cartridge_serial != old_serial:
        score += 5

    is_replacement = score >= 5

    if is_replacement:
        log.info(
            "  [%s] تشخیص تعویض کارتریج: score=%d (old_level=%s, new_level=%s, old_name=%s, new_name=%s)",
            state.get("printer_ip"), score, old_level, new_level,
            old_name or "?", new_cartridge_name or "?"
        )

    return is_replacement


def process_cartridge_snapshot(*, ip: str, color: str, printer_model: str, cartridge_name: str,
                               level, raw_level=None, counters: dict, device_type: str,
                               timestamp: str = None, source: str = "poll",
                               device_capacity_pages: int = None,
                               cartridge_serial: str = None,
                               _ensure_tables: bool = True) -> dict:
    """پردازش یک کارتریج و برگرداندن metadata قابل نمایش در API/UI."""
    if _ensure_tables:
        ensure_yield_tables()
    timestamp = timestamp or _now()
    color = (color or "black").lower()
    printer_model = printer_model or "Unknown"
    cartridge_name = cartridge_name or color
    cartridge_key = build_cartridge_key(printer_model, cartridge_name, color)
    counters = counters or {}
    level_i = _valid_level(level)
    device_capacity = _valid_device_capacity(device_capacity_pages)
    catalog_match = _catalog_lookup(printer_model, cartridge_name, color)
    catalog_capacity = _valid_device_capacity((catalog_match or {}).get("yield_per_page"))
    usage_counter, counter_basis, counter_quality = _compute_usage_counter(color, counters, device_type)

    with db_connection(commit=True) as conn:
        state = _load_state(conn, ip, color)
        profile = _load_profile(conn, cartridge_key)
        now = _now()

        if state is None:
            inherited_yield = DEFAULT_YIELD_PER_PAGE
            inherited_source = "default"
            inherited_conf = "low"
            if catalog_capacity:
                inherited_yield = catalog_capacity
                inherited_source = "catalog"
                inherited_conf = (catalog_match or {}).get("confidence") or "medium"
            if device_capacity:
                inherited_yield = device_capacity
                inherited_source = "device_capacity"
                inherited_conf = "medium"
            if profile and profile.get("confidence") == "high":
                inherited_yield = int(profile.get("yield_per_page") or DEFAULT_YIELD_PER_PAGE)
                inherited_source = "shared_profile"
                inherited_conf = "high"
            state = {
                "printer_ip": ip,
                "color": color,
                "printer_model": printer_model,
                "cartridge_name": cartridge_name,
                "cartridge_key": cartridge_key,
                "current_level": level_i,
                "last_valid_level": level_i,
                "last_counter": usage_counter,
                "anchor_counter": usage_counter if level_i is not None else None,
                "anchor_level": level_i,
                "anchor_timestamp": timestamp if level_i is not None else None,
                "yield_per_page": inherited_yield,
                "yield_source": inherited_source,
                "confidence": inherited_conf,
                "sample_count": 0,
                "total_weight": 0.0,
                "cycle_start_counter": usage_counter if level_i is not None else None,
                "cycle_start_level": level_i,
                "cycle_start_timestamp": timestamp if level_i is not None else None,
                "zero_reached_counter": usage_counter if level_i == 0 else None,
                "zero_reached_timestamp": timestamp if level_i == 0 else None,
                "pages_after_zero": 0,
                "cycle_status": "active",
                "updated_at": now,
            }
            _upsert_state(conn, state)
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source,
                             valid=level_i is not None and usage_counter is not None,
                             reject_reason=None if level_i is not None and usage_counter is not None else "missing_level_or_counter")
            return _state_public_dict(state, profile)

        # ظرفیت مرجع (اعلامی دستگاه در اولویت، بعد کاتالوگ) — لنگر «اصولی» ظرفیت
        rated_capacity = device_capacity or catalog_capacity

        # اگر نام override توسط کاربر تنظیم شده باشد، از آن استفاده کن
        # (مثلاً Canon MF220 همیشه 'Cartridge 137' برمیگرداند حتی با کارتریج 737)
        name_override = state.get("cartridge_name_override")
        if name_override:
            cartridge_name = name_override
            cartridge_key = build_cartridge_key(printer_model, name_override, color)

        # metadata همیشه به‌روز شود، چون ممکن است نام کارتریج یا مدل در pollهای بعدی کامل‌تر شود.
        state.update({
            "printer_model": printer_model,
            "last_cartridge_name": state.get("cartridge_name") or state.get("last_cartridge_name"),
            "cartridge_name": cartridge_name,
            "cartridge_key": cartridge_key,
            "cartridge_serial": cartridge_serial or state.get("cartridge_serial"),
            "updated_at": now,
        })

        # اگر هنوز فقط default داریم و خود دستگاه ظرفیت معتبر اعلام کرده، از آن به‌عنوان
        # fallback بهتر استفاده می‌کنیم. auto_learn/shared/high را override نمی‌کنیم.
        state_yield = _to_int(state.get("yield_per_page"), DEFAULT_YIELD_PER_PAGE)
        state_source = state.get("yield_source") or "default"
        state_confidence = state.get("confidence") or "low"
        low_conf_auto = state_source == "auto_learn" and state_confidence == "low"
        if catalog_capacity and (state_source == "default" or low_conf_auto or (state_source == "catalog" and state_yield != catalog_capacity)):
            state["yield_per_page"] = catalog_capacity
            state["yield_source"] = "catalog"
            state["confidence"] = (catalog_match or {}).get("confidence") or "medium"
            state_yield = catalog_capacity
            state_source = "catalog"
            state_confidence = state["confidence"]
        if device_capacity and (
            state_source in ("default", "catalog") or low_conf_auto or
            (state_source == "device_capacity" and state_yield != device_capacity)
        ):
            state["yield_per_page"] = device_capacity
            state["yield_source"] = "device_capacity"
            state["confidence"] = "medium"

        # ✅ خودترمیمی stateهای مسموم (داده‌ی واقعی: Toshiba .40 با yield=23,800 از
        # نویز، درحالی‌که rating کاتالوگ ۳۶,۶۰۰ است؛ یا .41 با cycle جعلی ~۳۲۰):
        # اگر ظرفیت «یادگرفته» بیش از ۴۰٪ با مرجع فاصله دارد، مرجع نمایش داده
        # می‌شود و شمارنده‌های آلوده صفر می‌شوند تا یادگیری تازه و تمیز شروع شود.
        if rated_capacity and state.get("yield_source") in ("auto_learn", "cycle_learn", "learned"):
            cur_yield = _to_int(state.get("yield_per_page"), DEFAULT_YIELD_PER_PAGE)
            if cur_yield and abs(cur_yield - rated_capacity) > LEARNED_DEVIATION_LIMIT * rated_capacity:
                log.warning(
                    "  [%s/%s] ظرفیت یادگرفته‌ی ناسازگار (%s) با مرجع (%s) — snap به مرجع و reset یادگیری",
                    ip, color, cur_yield, rated_capacity)
                state.update({
                    "yield_per_page": rated_capacity,
                    "yield_source": "device_capacity" if device_capacity else "catalog",
                    "confidence": (catalog_match or {}).get("confidence") or "medium",
                    "sample_count": 0,
                    "total_weight": 0.0,
                })

        if state.get("cycle_start_counter") is None and level_i is not None and usage_counter is not None:
            state.update({
                "cycle_start_counter": usage_counter,
                "cycle_start_level": level_i,
                "cycle_start_timestamp": timestamp,
                "cycle_status": "active",
            })

        if level_i is None or usage_counter is None:
            # ─── تشخیص تعویض مبتنی بر counter (برای پرینترهایی که سطح تونر گزارش نمی‌دهند) ───
            if level_i is None and usage_counter is not None:
                last_counter = _to_int(state.get("last_counter"))
                if last_counter is not None and usage_counter > last_counter:
                    pages_delta = usage_counter - last_counter
                    cycle_status = state.get("cycle_status") or "active"
                    # اگر سطح همیشه 0/None بوده و تعداد صفحات چاپ‌شده زیاد شده،
                    # احتمال تعویض کارتریج وجود دارد (چون کارتریج جدید هم 0/None گزارش می‌دهد)
                    if pages_delta >= 100 and cycle_status == "zero_plateau":
                        # بررسی تغییر نام کارتریج (اگر نام تغییر کرده باشد = تعویض قطعی)
                        name_changed = False
                        old_name = state.get("last_cartridge_name") or state.get("cartridge_name")
                        if old_name and cartridge_name and _norm_text(old_name) != _norm_text(cartridge_name):
                            name_changed = True
                        
                        if name_changed:
                            log.info(
                                "  [%s/%s] 🔄 تشخیص تعویض (تغییر نام): %s → %s (counter: %d → %d)",
                                ip, color, old_name, cartridge_name, last_counter, usage_counter)
                            state.update({
                                "current_level": None,
                                "last_valid_level": None,
                                "last_cartridge_name": state.get("cartridge_name"),
                                "cartridge_name": cartridge_name,
                                "last_counter": usage_counter,
                                "anchor_counter": usage_counter,
                                "anchor_level": None,
                                "anchor_timestamp": timestamp,
                                "cycle_start_counter": usage_counter,
                                "cycle_start_level": None,
                                "cycle_start_timestamp": timestamp,
                                "cycle_status": "active",
                                "yield_per_page": catalog_capacity or DEFAULT_YIELD_PER_PAGE,
                                "yield_source": "catalog" if catalog_capacity else "default",
                                "confidence": ((catalog_match or {}).get("confidence") or "medium") if catalog_capacity else "low",
                                "sample_count": 0,
                                "total_weight": 0.0,
                                "last_refill_at": timestamp,
                                "last_refill_counter": usage_counter,
                                "replacement_detected_at": timestamp,
                                "updated_at": now,
                            })
                            try:
                                from core.database import add_event
                                add_event(ip, "CARTRIDGE_REPLACED", {
                                    "color": color,
                                    "old_name": old_name,
                                    "new_name": cartridge_name,
                                    "counter": usage_counter,
                                    "pages_since_last": pages_delta,
                                    "detection_method": "name_change_with_zero_level",
                                })
                            except Exception:
                                pass
                            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                                             timestamp, counters, usage_counter, None, raw_level, source,
                                             valid=False, cartridge_serial=cartridge_serial)
                            _upsert_state(conn, state)
                            return _state_public_dict(state, profile)
                
                # همیشه counter را به‌روز نگه دار (حتی اگر تشخیص ندادیم)
                state["last_counter"] = usage_counter
                state["updated_at"] = now
            
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source,
                             valid=False, reject_reason="missing_level_or_counter")
            state["updated_at"] = now
            _upsert_state(conn, state)
            return _state_public_dict(state, profile)

        # اگر counter عقب‌گرد کرد، احتمال reset/reboot/counter change است؛ anchor را reset می‌کنیم و یاد نمی‌گیریم.
        last_counter = _to_int(state.get("last_counter"))
        if last_counter is not None and usage_counter < last_counter:
            _record_sample(conn, ip=ip, color=color, printer_model=printer_model,
                           cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                           start_counter=last_counter, end_counter=usage_counter,
                           start_level=state.get("last_valid_level"), end_level=level_i,
                           pages_delta=usage_counter - last_counter, toner_drop=0,
                           estimated_yield=None, counter_basis=counter_basis, weight=0,
                           accepted=False, reject_reason="counter_decreased", source=source)
            state.update({
                "current_level": level_i,
                "last_valid_level": level_i,
                "last_counter": usage_counter,
                "anchor_counter": usage_counter,
                "anchor_level": level_i,
                "anchor_timestamp": timestamp,
                "pending_refill_level": None,
                "pending_refill_counter": None,
                "pending_refill_timestamp": None,
                "updated_at": now,
            })
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source,
                             valid=False, reject_reason="counter_decreased_anchor_reset")
            _upsert_state(conn, state)
            return _state_public_dict(state, profile)

        # ─── تعریف متغیرهای مشترک ───
        prev_level = _to_int(state.get("last_valid_level"))
        anchor_level = _to_int(state.get("anchor_level"))
        anchor_counter = _to_int(state.get("anchor_counter"))
        cycle_status = state.get("cycle_status") or "active"

        # ─── تشخیص تعویض در حالت zero_plateau (پرینترهایی که سطح 0 باقی می‌مانند) ───
        # برای پرینترهایی مثل Canon MF220 که SNMP سطح 0 گزارش می‌دهند
        # و هیچ‌وقت سطح تغییر نمی‌کند.
        if level_i == 0 and cycle_status == "zero_plateau" and prev_level is not None and prev_level == 0:
            last_counter_val = _to_int(state.get("last_counter"))
            pages_since_anchor = usage_counter - (anchor_counter or usage_counter) if anchor_counter is not None else 0
            
            # تشخیص تغییر نام (قوی‌ترین سیگنال)
            old_name_for_zp = state.get("last_cartridge_name") or state.get("cartridge_name")
            name_changed = (old_name_for_zp and cartridge_name and 
                          _norm_text(old_name_for_zp) != _norm_text(cartridge_name))
            
            if name_changed and pages_since_anchor >= 10:
                log.info(
                    "  [%s/%s] 🔄 تعویض تشخیص داده شد (zero_plateau + تغییر نام): %s → %s (counter: %d)",
                    ip, color, old_name_for_zp, cartridge_name, usage_counter)
                state.update({
                    "current_level": 0,
                    "last_valid_level": 0,
                    "last_cartridge_name": state.get("cartridge_name"),
                    "cartridge_name": cartridge_name,
                    "last_counter": usage_counter,
                    "anchor_counter": usage_counter,
                    "anchor_level": 0,
                    "anchor_timestamp": timestamp,
                    "cycle_start_counter": usage_counter,
                    "cycle_start_level": 0,
                    "cycle_start_timestamp": timestamp,
                    "zero_reached_counter": usage_counter,
                    "zero_reached_timestamp": timestamp,
                    "pages_after_zero": 0,
                    "cycle_status": "active",
                    "yield_per_page": catalog_capacity or DEFAULT_YIELD_PER_PAGE,
                    "yield_source": "catalog" if catalog_capacity else "default",
                    "confidence": ((catalog_match or {}).get("confidence") or "medium") if catalog_capacity else "low",
                    "sample_count": 0,
                    "total_weight": 0.0,
                    "last_refill_at": timestamp,
                    "last_refill_counter": usage_counter,
                    "replacement_detected_at": timestamp,
                    "updated_at": now,
                })
                try:
                    from core.database import add_event
                    add_event(ip, "CARTRIDGE_REPLACED", {
                        "color": color,
                        "old_name": old_name_for_zp,
                        "new_name": cartridge_name,
                        "counter": usage_counter,
                        "pages_since_anchor": pages_since_anchor,
                        "detection_method": "zero_plateau_name_change",
                    })
                except Exception:
                    pass
                _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                                 timestamp, counters, usage_counter, level_i, raw_level, source,
                                 valid=True, cartridge_serial=cartridge_serial)
                _upsert_state(conn, state)
                return _state_public_dict(state, profile)
            
            # حتی بدون تغییر نام، اگر صفحات زیاد شده و هیچ جایی counter را چک نکرده‌ایم
            if last_counter_val is None and pages_since_anchor >= 50:
                # اولین poll پس از راه‌اندازی مجدد — counter را تنظیم کن
                state["last_counter"] = usage_counter
                state["updated_at"] = now
        
        # ─── دروازه‌ی ضد-نویز سطح (blip تک‌پالی) ─────────────────────────
        # ✅ داده‌ی واقعی (TOSHIBA 0.40/0.41): سطح پایدار ۱۰۰٪ + pollهای تنهای ۸٪.
        # هر blip بدون این دروازه: anchor به ۸ می‌نشست → بازگشت به ۱۰۰ «شارژ»
        # تشخیص داده می‌شد → cycle_learn با yield مسموم ۳۲۰-۴۰۰ ثبت می‌شد.
        # افت شدید + صفحه‌ی تقریباً صفر = نویز خواندن: anchor و last_valid_level
        # جابه‌جا نمی‌شوند ولی last_counter جلو می‌رود تا صفحاتش محفوظ بماند.
        if (prev_level is not None and level_i is not None
                and prev_level - level_i > LEVEL_NOISE_DROP_PERCENT
                and usage_counter is not None and last_counter is not None
                and 0 <= usage_counter - last_counter < LEVEL_NOISE_MAX_PAGES):
            log.info("  [%s/%s] خواندن نویزی سطح رد شد: %s%% → %s%% با %s صفحه (بدون تغییر در anchor)",
                     ip, color, prev_level, level_i, usage_counter - last_counter)
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source,
                             valid=False, reject_reason="level_read_noise")
            state.update({"current_level": level_i, "last_counter": usage_counter, "updated_at": now})
            _upsert_state(conn, state)
            return _state_public_dict(state, profile)

        # ─── تأیید خودکار تعویض برای پرینترهای zero-plateau ───
        # وقتی اسکرپر HTTP لحظه‌ای سطح بالا (مثلاً 100٪) دید ولی سطح SNMP
        # همیشه 0 است (مثل Canon MF220)، آن لحظه خودش سیگنال تعویض کارتریج است.
        pending_level = _to_int(state.get("pending_refill_level"))
        pending_counter = _to_int(state.get("pending_refill_counter"))
        if (pending_level is not None and pending_level >= 80
                and level_i is not None and level_i < pending_level
                and cycle_status in ("zero_plateau", "refill_pending")
                and usage_counter is not None
                and pending_counter is not None and usage_counter > pending_counter):
            log.info(
                "  [%s/%s] 🔄 تأیید خودکار تعویض (zero-plateau): اسکرپر %s%% دید → ریست به 100%% (counter: %d → %d)",
                ip, color, pending_level, pending_counter, usage_counter)
            try:
                from core.database import add_event
                add_event(ip, "CARTRIDGE_REPLACED", {
                    "color": color,
                    "old_level": prev_level,
                    "new_level": 100,
                    "old_name": state.get("last_cartridge_name") or state.get("cartridge_name"),
                    "new_name": cartridge_name,
                    "counter": usage_counter,
                    "detection_method": "zero_plateau_scraper_spike",
                    "scraper_saw_level": pending_level,
                })
            except Exception:
                pass
            state.update({
                "current_level": 100,
                "last_valid_level": 100,
                "anchor_level": 100,
                "anchor_counter": usage_counter,
                "anchor_timestamp": timestamp,
                "cycle_start_counter": usage_counter,
                "cycle_start_level": 100,
                "cycle_start_timestamp": timestamp,
                "cycle_status": "active",
                "yield_per_page": rated_capacity or DEFAULT_YIELD_PER_PAGE,
                "yield_source": "catalog" if catalog_capacity else "default",
                "confidence": ((catalog_match or {}).get("confidence") or "medium") if catalog_capacity else "low",
                "sample_count": 0,
                "total_weight": 0.0,
                "cartridge_serial": cartridge_serial,
                "last_refill_at": timestamp,
                "last_refill_counter": usage_counter,
                "pending_refill_level": None,
                "pending_refill_counter": None,
                "pending_refill_timestamp": None,
                "zero_reached_counter": None,
                "zero_reached_timestamp": None,
                "pages_after_zero": 0,
                "last_counter": usage_counter,
                "updated_at": now,
            })
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, 100, raw_level, source,
                             valid=True, cartridge_serial=cartridge_serial)
            _upsert_state(conn, state)
            return _state_public_dict(state, profile)

        # ─── تشخیص تعویض کارتریج ─────────────────────────────
        if prev_level is not None and level_i is not None and level_i - prev_level >= REFILL_JUMP_PERCENT:
            is_replacement = detect_cartridge_replacement(
                state, level_i, cartridge_name, usage_counter, cartridge_serial)

            if is_replacement:
                log.info(
                    "  [%s/%s] 🔄 تعویض کارتریج تشخیص داده شد: %s%% → %s%% (name: %s)",
                    ip, color, prev_level, level_i, cartridge_name)
                # ثبت رویداد
                try:
                    from core.database import add_event
                    add_event(ip, "CARTRIDGE_REPLACED", {
                        "color": color,
                        "old_level": prev_level,
                        "new_level": level_i,
                        "old_name": state.get("last_cartridge_name") or state.get("cartridge_name"),
                        "new_name": cartridge_name,
                        "old_serial": state.get("cartridge_serial"),
                        "new_serial": cartridge_serial,
                        "counter": usage_counter,
                    })
                except Exception:
                    pass

                # ریست state برای کارتریج جدید
                state.update({
                    "current_level": 100,
                    "last_valid_level": 100,
                    "anchor_level": 100,
                    "anchor_counter": usage_counter,
                    "anchor_timestamp": timestamp,
                    "cycle_start_counter": usage_counter,
                    "cycle_start_level": 100,
                    "cycle_start_timestamp": timestamp,
                    "cycle_status": "active",
                    "yield_per_page": rated_capacity or DEFAULT_YIELD_PER_PAGE,
                    "yield_source": "catalog" if catalog_capacity else "default",
                    "confidence": ((catalog_match or {}).get("confidence") or "medium") if catalog_capacity else "low",
                    "sample_count": 0,
                    "total_weight": 0.0,
                    "cartridge_serial": cartridge_serial,
                    "last_refill_at": timestamp,
                    "last_refill_counter": usage_counter,
                    "pending_refill_level": None,
                    "pending_refill_counter": None,
                    "pending_refill_timestamp": None,
                    "zero_reached_counter": None,
                    "zero_reached_timestamp": None,
                    "pages_after_zero": 0,
                    "last_counter": usage_counter,
                    "updated_at": now,
                })
                _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                                 timestamp, counters, usage_counter, level_i, raw_level, source,
                                 valid=True, cartridge_serial=cartridge_serial)
                _upsert_state(conn, state)
                return _state_public_dict(state, profile)

        # افزایش زیاد تونر: refill candidate و سپس confirm در poll بعدی.
            pending_level = _to_int(state.get("pending_refill_level"))
            pending_counter = _to_int(state.get("pending_refill_counter"))
            if pending_level is not None and level_i >= pending_level - 1 and usage_counter >= (pending_counter or usage_counter):
                cycle_update = _maybe_close_cycle(
                    conn, state, ip=ip, color=color, printer_model=printer_model,
                    cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                    usage_counter=usage_counter, timestamp=timestamp,
                    counter_basis=counter_basis, source="cycle_auto_refill",
                    rated_capacity=rated_capacity,
                ) or {}
                state.update(cycle_update)
                state.update({
                    "last_refill_at": timestamp,
                    "last_refill_counter": usage_counter,
                    "anchor_counter": usage_counter,
                    "anchor_level": level_i,
                    "anchor_timestamp": timestamp,
                    "cycle_start_counter": usage_counter,
                    "cycle_start_level": level_i,
                    "cycle_start_timestamp": timestamp,
                    "zero_reached_counter": None,
                    "zero_reached_timestamp": None,
                    "pages_after_zero": 0,
                    "cycle_status": "active",
                    "pending_refill_level": None,
                    "pending_refill_counter": None,
                    "pending_refill_timestamp": None,
                })
            else:
                state.update({
                    "pending_refill_level": level_i,
                    "pending_refill_counter": usage_counter,
                    "pending_refill_timestamp": timestamp,
                    # از نظر UI این دیگر zero plateau فعال نیست؛ فقط منتظر تأیید poll بعدی هستیم.
                    "cycle_status": "refill_pending",
                })
            state.update({"current_level": level_i, "last_valid_level": level_i, "last_counter": usage_counter, "updated_at": now})
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source,
                             valid=True, reject_reason="refill_candidate_or_confirmed")
            _upsert_state(conn, state)
            return _state_public_dict(state, profile)

        # افزایش کوچک تونر معمولاً bounce/noise است؛ یادگیری نکن ولی آخرین مقدار را ثبت کن.
        if prev_level is not None and level_i > prev_level and level_i - prev_level <= SMALL_LEVEL_BOUNCE_PERCENT:
            state.update({"current_level": level_i, "last_valid_level": level_i, "last_counter": usage_counter, "updated_at": now})
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source,
                             valid=True, reject_reason="small_level_bounce")
            _upsert_state(conn, state)
            return _state_public_dict(state, profile)

        if level_i == 0:
            zero_counter = _to_int(state.get("zero_reached_counter"))
            if zero_counter is None:
                state["zero_reached_counter"] = usage_counter
                state["zero_reached_timestamp"] = timestamp
                state["pages_after_zero"] = 0
            else:
                state["pages_after_zero"] = max(0, usage_counter - zero_counter)
            state["cycle_status"] = "zero_plateau"

        if anchor_level is None or anchor_counter is None:
            state.update({
                "current_level": level_i,
                "last_valid_level": level_i,
                "last_counter": usage_counter,
                "anchor_counter": usage_counter,
                "anchor_level": level_i,
                "anchor_timestamp": timestamp,
                "updated_at": now,
            })
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source, valid=True)
            _upsert_state(conn, state)
            return _state_public_dict(state, profile)

        toner_drop = anchor_level - level_i
        pages_delta = usage_counter - anchor_counter

        # سطح ثابت: anchor را نگه می‌داریم تا پرینترهای کم‌مصرف هم تجمعی محاسبه شوند.
        if toner_drop <= 0:
            state.update({"current_level": level_i, "last_valid_level": level_i, "last_counter": usage_counter, "updated_at": now})
            _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                             timestamp, counters, usage_counter, level_i, raw_level, source, valid=True)
            _upsert_state(conn, state)
            return _state_public_dict(state, profile)

        estimated_yield = int(round(pages_delta * 100.0 / toner_drop)) if toner_drop > 0 else None
        reject_reason = None
        accepted = True
        if pages_delta < MIN_PAGES_FOR_SAMPLE:
            accepted = False
            reject_reason = "not_enough_pages_yet"
        elif toner_drop == 1 and pages_delta < QUANT_MIN_PAGES_1PCT:
            # نمونه‌ی «۱٪یِ کوانتیزه با صفحات کم» خطای نسبی بزرگی دارد (مثال واقعی:
            # ۱۱ صفحه → تخمین ۱۱۰۰ برای Cartridge 737 با ریتینگ ۲۴۰۰). صبر کن تا
            # صفحات خوب جمع شوند یا افت به ۲٪+ برسد؛ آن‌وقت نسبت قابل‌اتکاست.
            accepted = False
            reject_reason = "quantization_1pct_noise"
        elif estimated_yield < MIN_ESTIMATED_YIELD or estimated_yield > MAX_ESTIMATED_YIELD:
            accepted = False
            reject_reason = "estimated_yield_out_of_range"

        weight = _sample_weight(pages_delta, toner_drop, counter_quality) if accepted else 0.0
        _record_sample(conn, ip=ip, color=color, printer_model=printer_model,
                       cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                       start_counter=anchor_counter, end_counter=usage_counter,
                       start_level=anchor_level, end_level=level_i,
                       pages_delta=pages_delta, toner_drop=toner_drop,
                       estimated_yield=estimated_yield, counter_basis=counter_basis,
                       weight=weight, accepted=accepted, reject_reason=reject_reason, source=source)

        if accepted:
            old_yield = _to_int(state.get("yield_per_page"), DEFAULT_YIELD_PER_PAGE)
            old_source = state.get("yield_source") or "default"
            old_confidence = state.get("confidence") or "low"
            old_weight = float(state.get("total_weight") or 0.0)
            new_weight = old_weight + weight
            sample_count = int(state.get("sample_count") or 0) + 1
            # ✅ میانه‌ی وزنی نمونه‌های اخیر (مقاوم به outlier) به‌جای میانگین متحرک
            _rb, _rb_w = _robust_learned_yield(conn, ip, color, cartridge_key, rated=rated_capacity)
            new_yield = _rb if _rb else _blend_yield(old_yield, old_weight, estimated_yield, weight)
            confidence = _derive_confidence(sample_count, new_weight, counter_basis)

            # یک sample با confidence پایین، به‌خصوص برای رنگی‌ها یا تونرهایی که تازه به 0/1٪
            # رسیده‌اند، می‌تواند جهش غیرواقعی بسازد. بنابراین اگر baseline بهتری مثل
            # catalog/device_capacity/shared_profile داریم، auto_learn با اعتماد کم نباید
            # ظرفیت نمایشی را override کند. sample ثبت می‌شود و از sampleهای بعدی استفاده خواهد شد.
            keep_existing_capacity = (
                (old_source in ("catalog", "device_capacity") and confidence == "low") or
                (old_source == "shared_profile" and confidence != "high")
            )
            # ✅ قانون سازگاری با مرجع: وقتی ظرفیت مرجع (کاتالوگ/دستگاه) موجود است،
            # مقدار یادگرفته فقط وقتی نمایش داده می‌شود که در باند ۴۰٪± مرجع باشد؛
            # بیرون از آن نشانه‌ی نویز/کارتریج نامتعارف است و مرجع نمایش می‌ماند.
            if (rated_capacity and new_yield
                    and abs(new_yield - rated_capacity) > LEARNED_DEVIATION_LIMIT * rated_capacity
                    and old_source not in ("manual_reset", "forced_estimate", "shared_profile")):
                keep_existing_capacity = True
            update_payload = {
                "sample_count": sample_count,
                "total_weight": new_weight,
                "learned_at": timestamp,
                # بعد از sample معتبر، anchor را به سطح فعلی منتقل می‌کنیم.
                "anchor_counter": usage_counter,
                "anchor_level": level_i,
                "anchor_timestamp": timestamp,
            }
            if keep_existing_capacity:
                update_payload.update({
                    "yield_per_page": old_yield,
                    "yield_source": old_source,
                    "confidence": old_confidence,
                })
            else:
                update_payload.update({
                    "yield_per_page": new_yield,
                    "yield_source": "auto_learn",
                    "confidence": confidence,
                })
            state.update(update_payload)
        else:
            # برای not_enough_pages_yet عمداً anchor را عوض نمی‌کنیم تا drop بعدی تجمعی شود.
            if reject_reason != "not_enough_pages_yet":
                state.update({"anchor_counter": usage_counter, "anchor_level": level_i, "anchor_timestamp": timestamp})

        state.update({"current_level": level_i, "last_valid_level": level_i, "last_counter": usage_counter, "updated_at": now})
        _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                         timestamp, counters, usage_counter, level_i, raw_level, source,
                         valid=accepted, reject_reason=reject_reason)
        _upsert_state(conn, state)
        _upsert_profile_if_high(conn, state)
        profile = _load_profile(conn, cartridge_key)
        return _state_public_dict(state, profile)


def process_printer_yield_snapshot(*, ip: str, printer_model: str, counters: dict,
                                   toners: Dict[str, dict], device_type: str,
                                   timestamp: str = None, source: str = "poll") -> Dict[str, dict]:
    """پردازش همه تونرهای یک پرینتر و برگرداندن metadata per-color."""
    results = {}
    if not isinstance(toners, dict):
        return results
    ensure_yield_tables()
    for color in sorted(toners.keys(), key=lambda c: _COLOR_ORDER.index(c) if c in _COLOR_ORDER else 99):
        toner = toners.get(color) or {}
        if not isinstance(toner, dict):
            continue
        raw_level = toner.get("raw_level", toner.get("level"))
        level = raw_level if raw_level is not None else toner.get("level")
        name = toner.get("name") or color
        device_capacity_pages = toner.get("device_capacity_pages")
        try:
            results[color] = process_cartridge_snapshot(
                ip=ip,
                color=color,
                printer_model=printer_model,
                cartridge_name=name,
                level=level,
                raw_level=raw_level,
                counters=counters,
                device_type=device_type,
                timestamp=timestamp,
                source=source,
                device_capacity_pages=device_capacity_pages,
                cartridge_serial=toner.get("serial"),
                _ensure_tables=False,
            )
        except Exception as exc:
            log.exception("Yield engine failed for %s/%s: %s", ip, color, exc)
    return results


def register_manual_refill(*, ip: str, color: str, printer_model: str, cartridge_name: str,
                           new_level: int, counters: dict, device_type: str = "unknown",
                           timestamp: str = None) -> dict:
    """ثبت reset/شارژ دستی به‌عنوان anchor معتبر برای Yield Engine."""
    ensure_yield_tables()
    timestamp = timestamp or _now()
    color = (color or "black").lower()
    printer_model = printer_model or "Unknown"
    cartridge_name = cartridge_name or color
    cartridge_key = build_cartridge_key(printer_model, cartridge_name, color)
    level_i = _valid_level(new_level)
    if level_i is None:
        level_i = 100
    usage_counter, counter_basis, counter_quality = _compute_usage_counter(color, counters or {}, device_type)
    catalog_match = _catalog_lookup(printer_model, cartridge_name, color)
    catalog_capacity = _valid_device_capacity((catalog_match or {}).get("yield_per_page"))

    with db_connection(commit=True) as conn:
        state = _load_state(conn, ip, color) or {}

        # اگر نام override توسط کاربر تنظیم شده، از آن استفاده کن
        existing_override = state.get("cartridge_name_override")
        if existing_override:
            cartridge_name = existing_override
            cartridge_key = build_cartridge_key(printer_model, existing_override, color)

        profile = _load_profile(conn, cartridge_key)
        inherited_yield = _to_int(state.get("yield_per_page"), DEFAULT_YIELD_PER_PAGE)
        inherited_source = state.get("yield_source") or "default"
        inherited_conf = state.get("confidence") or "low"
        if inherited_yield == DEFAULT_YIELD_PER_PAGE and inherited_source == "default" and catalog_capacity:
            inherited_yield = catalog_capacity
            inherited_source = "catalog"
            inherited_conf = (catalog_match or {}).get("confidence") or "medium"
        if inherited_yield == DEFAULT_YIELD_PER_PAGE and profile and profile.get("confidence") == "high":
            inherited_yield = int(profile.get("yield_per_page") or DEFAULT_YIELD_PER_PAGE)
            inherited_source = "shared_profile"
            inherited_conf = "high"

        cycle_update = {}
        if state and usage_counter is not None:
            _rated_manual = (lambda cm: (cm or {}).get("yield_per_page"))(
                _catalog_lookup(printer_model, cartridge_name, color))
            cycle_update = _maybe_close_cycle(
                conn, state, ip=ip, color=color, printer_model=printer_model,
                cartridge_name=cartridge_name, cartridge_key=cartridge_key,
                usage_counter=usage_counter, timestamp=timestamp,
                counter_basis=counter_basis, source="cycle_manual_refill",
                rated_capacity=_valid_device_capacity(_rated_manual),
            ) or {}
            if cycle_update:
                inherited_yield = cycle_update.get("yield_per_page", inherited_yield)
                inherited_source = cycle_update.get("yield_source", inherited_source)
                inherited_conf = cycle_update.get("confidence", inherited_conf)

        state.update({
            "printer_ip": ip,
            "color": color,
            "printer_model": printer_model,
            "cartridge_name": cartridge_name,
            "cartridge_key": cartridge_key,
            "current_level": level_i,
            "last_valid_level": level_i,
            "last_counter": usage_counter,
            "anchor_counter": usage_counter,
            "anchor_level": level_i,
            "anchor_timestamp": timestamp,
            "yield_per_page": inherited_yield,
            "yield_source": inherited_source,
            "confidence": inherited_conf,
            "sample_count": int(state.get("sample_count") or 0),
            "total_weight": float(state.get("total_weight") or 0.0),
            "last_refill_at": timestamp,
            "last_refill_counter": usage_counter,
            "pending_refill_level": None,
            "pending_refill_counter": None,
            "pending_refill_timestamp": None,
            "cycle_start_counter": usage_counter,
            "cycle_start_level": level_i,
            "cycle_start_timestamp": timestamp,
            "zero_reached_counter": None,
            "zero_reached_timestamp": None,
            "pages_after_zero": 0,
            "cycle_status": "active",
            "updated_at": _now(),
        })
        _record_snapshot(conn, ip, color, printer_model, cartridge_name, cartridge_key,
                         timestamp, counters or {}, usage_counter, level_i, level_i,
                         "manual_reset", valid=usage_counter is not None,
                         reject_reason=None if usage_counter is not None else "missing_counter_manual_reset")
        _upsert_state(conn, state)

        # ثبت رویداد CARTRIDGE_REPLACED (مستقیم با conn برای جلوگیری از database lock)
        old_name_for_event = state.get("last_cartridge_name") or state.get("cartridge_name")
        old_level_for_event = state.get("last_valid_level")
        try:
            printer_name_ev = None
            try:
                from core import store
                with store.printers_lock:
                    for p in store.PRINTERS:
                        if p.get("ip") == ip:
                            printer_name_ev = p.get("name")
                            break
            except Exception:
                pass
            conn.execute(
                "INSERT INTO logs (printer_ip, printer_name, timestamp, type, message, pages, color, code, severity, paper_size, username, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ip, printer_name_ev, _now(), "CARTRIDGE_REPLACED", "", None, color, None, "info", None, None,
                 json.dumps({"color": color, "old_level": old_level_for_event, "new_level": level_i,
                             "old_name": old_name_for_event, "new_name": cartridge_name,
                             "counter": usage_counter, "detection_method": "manual_refill"}, ensure_ascii=False)),
            )
        except Exception:
            log.exception("Failed to log CARTRIDGE_REPLACED event for %s", ip)

        return _state_public_dict(state, profile)


def set_cartridge_name_override(ip: str, color: str, override_name: str) -> dict:
    """تنظیم نام دستی کارتریج برای پرینترهایی که SNMP نام اشتباه برمیگردانند.

    Canon MF220 همیشه 'Cartridge 137' برمیگرداند حتی با کارتریج 737.
    این تابع اجازه می‌دهد نام واقعی ذخیره شود.
    """
    ensure_yield_tables()
    color = (color or "black").lower()
    override_name = (override_name or "").strip()
    with db_connection(commit=True) as conn:
        state = _load_state(conn, ip, color)
        if state is None:
            return {"error": "cartridge state not found"}
        state["cartridge_name_override"] = override_name or None
        # همچنین cartridge_name و cartridge_key را آپدیت کن
        if override_name:
            state["cartridge_name"] = override_name
            state["cartridge_key"] = build_cartridge_key(
                state.get("printer_model", ""), override_name, color)
            state["last_cartridge_name"] = override_name
        state["updated_at"] = _now()
        _upsert_state(conn, state)
        profile = _load_profile(conn, state.get("cartridge_key"))
        return _state_public_dict(state, profile)


def get_yield_status(ip: str = None) -> list:
    ensure_yield_tables()
    params = []
    where = ""
    if ip:
        where = "WHERE printer_ip = ?"
        params.append(ip)
    with db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT printer_ip, color, printer_model, cartridge_name, cartridge_key,
                   current_level, yield_per_page, yield_source, confidence,
                   sample_count, total_weight, anchor_level, anchor_counter,
                   last_refill_at, learned_at, cycle_start_counter, cycle_start_level,
                   zero_reached_counter, pages_after_zero, cycle_status, updated_at
            FROM cartridge_state
            {where}
            ORDER BY printer_ip, color
            """,
            params,
        ).fetchall()
    return [
        {
            "printer_ip": r[0],
            "color": r[1],
            "printer_model": r[2],
            "cartridge_name": r[3],
            "cartridge_key": r[4],
            "current_level": r[5],
            "yield_per_page": r[6],
            "yield_source": r[7],
            "confidence": r[8],
            "sample_count": r[9],
            "total_weight": r[10],
            "anchor_level": r[11],
            "anchor_counter": r[12],
            "last_refill_at": r[13],
            "learned_at": r[14],
            "cycle_start_counter": r[15],
            "cycle_start_level": r[16],
            "zero_reached_counter": r[17],
            "pages_after_zero": r[18],
            "cycle_status": r[19],
            "updated_at": r[20],
        }
        for r in rows
    ]


def write_yield_status_report(path: str = "yield_status_report.txt") -> None:
    rows = get_yield_status()
    lines = []
    for r in rows:
        lines.append(
            f"{r['printer_ip']} {r['color']} yield_per_page={r['yield_per_page']} "
            f"source={r['yield_source']} confidence={r['confidence']} "
            f"samples={r['sample_count']} model={json.dumps(r['printer_model'] or '', ensure_ascii=False)} "
            f"cartridge={json.dumps(r['cartridge_name'] or '', ensure_ascii=False)}"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        if lines:
            f.write("\n")
