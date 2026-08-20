"""
توابع کار با SQLite:
- init_db: ایجاد جدول logs (با فیلد paper_size) و جدول printer_counters
- add_event: ثبت رویداد (با پشتیبانی از paper_size)
- get_log: دریافت لاگ یک پرینتر
- get_all_logs: دریافت همه لاگ‌ها با فیلتر
- clear_logs: پاک کردن لاگ‌های غیر از PRINT، SERVICE و REFILL
- prune_old_print_logs: پاکسازی خودکار لاگ‌های PRINT قدیمی
- load_printer_counters, save_printer_counters: ذخیره و بازیابی مقادیر قبلی شمارنده‌ها
"""

import sqlite3
import json
import logging
import os
import re
import secrets
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional

from config.settings import DB_PATH
# NOTE: from core import store حذف شده است (import پویا درون تابع add_event)

log = logging.getLogger("PrinterMonitor")

_missing_yield_lock = threading.Lock()

# ─── Context manager برای اتصال امن به SQLite ─────────────────
@contextmanager
def db_connection(timeout: float = 10.0, commit: bool = False):
    """
    Context manager برای اتصال امن به دیتابیس.
    تضمین می‌کند که اتصال در همه شرایط (موفق یا خطا) بسته می‌شود.

    استفاده:
        # فقط خواندن:
        with db_connection() as conn:
            row = conn.execute("SELECT ... WHERE id=?", (uid,)).fetchone()
            return _row_to_dict(row)

        # نوشتن (با commit خودکار):
        with db_connection(commit=True) as conn:
            conn.execute("UPDATE users SET ... WHERE id=?", (uid,))
    """
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    try:
        yield conn
        if commit:
            conn.commit()
    except Exception:
        # rollback خودکار در صورت خطا (اگر در حال نوشتن بودیم)
        if commit:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass

# فیلدهای top-level که مستقیم در ستون‌های جدول ذخیره می‌شوند (بقیه به details JSON می‌روند)
_LOG_TOP_LEVEL_FIELDS = frozenset(
    ("timestamp", "message", "pages", "color", "code", "severity", "paper_size", "username")
)

MISSING_YIELD_FILE = "missing_yield_printers.txt"

_USER_JSON_FIELDS = frozenset(("allowed_offices", "allowed_modules"))


def update_missing_yield_list(ip: str, current_yield: int, source: str = None):
    """ثبت yield_per_page هر پرینتر در فایل legacy `missing_yield_printers.txt`.

    نکته: منبع اصلی Yield Engine جدول‌های دیتابیس است. این فایل فقط برای
    سازگاری و گزارش سریع نگه داشته شده است.

    فرمت هر خط:
        <ip> yield_per_page=<value> status=<default|catalog|device_capacity|shared_profile|auto_learn|learned>

    اگر current_yield == -1 باشد، IP از فایل حذف می‌شود.
    خطوط قدیمی که فقط IP بودند هم هنگام نوشتن به فرمت جدید تبدیل می‌شوند.
    """
    try:
        with _missing_yield_lock:
            rows = {}
            if os.path.exists(MISSING_YIELD_FILE):
                with open(MISSING_YIELD_FILE, 'r', encoding='utf-8') as f:
                    for raw_line in f:
                        line = raw_line.strip()
                        if not line or line.startswith('#'):
                            continue
                        parts = line.split()
                        row_ip = parts[0]
                        row_yield = 2000
                        row_source = 'default'
                        for part in parts[1:]:
                            if part.startswith('yield_per_page='):
                                try:
                                    row_yield = int(part.split('=', 1)[1])
                                except (TypeError, ValueError):
                                    row_yield = 2000
                            elif part.startswith('status='):
                                row_source = part.split('=', 1)[1] or row_source
                        rows[row_ip] = {"yield": row_yield, "source": row_source}

            if int(current_yield or 0) == -1:
                rows.pop(ip, None)
            else:
                y = int(current_yield or 2000)
                status = (source or '').strip() or ('default' if y == 2000 else 'learned')
                rows[ip] = {"yield": y, "source": status}

            lines = []
            for row_ip in sorted(rows):
                row = rows[row_ip]
                y = int(row.get("yield") or 2000)
                status = row.get("source") or ('default' if y == 2000 else 'learned')
                lines.append(f"{row_ip} yield_per_page={y} status={status}")

            with open(MISSING_YIELD_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))
                if lines:
                    f.write('\n')
    except Exception as e:
        log.exception(f"Error updating missing yield list: {e}")


USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


def _dump_json_list(value) -> str:
    if not value:
        return "[]"
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                value = parsed
            else:
                value = [parsed]
        except Exception:
            value = [value]
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    cleaned = []
    for item in value:
        text = str(item).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return json.dumps(cleaned, ensure_ascii=False)


def _load_json_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    try:
        parsed = json.loads(value)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    c = conn.cursor()

    c.execute("PRAGMA busy_timeout=30000")
    # تغییر journal mode روی share شبکه‌ای ممکن است موقتاً قفل شود؛
    # حالت فعلی را حفظ کن و اجازه بده startup ادامه پیدا کند.
    try:
        c.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        log.warning("Database is temporarily locked; keeping current journal mode: %s", exc)
    try:
        c.execute("PRAGMA synchronous=FULL")
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        log.warning("Could not set SQLite synchronous mode during startup: %s", exc)
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_ip TEXT NOT NULL,
            printer_name TEXT,
            timestamp TEXT NOT NULL,
            type TEXT,
            message TEXT,
            pages INTEGER,
            color TEXT,
            code TEXT,
            severity TEXT,
            paper_size TEXT,
            username TEXT,
            details TEXT
        )
    ''')
    try:
        c.execute("ALTER TABLE logs ADD COLUMN paper_size TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE logs ADD COLUMN username TEXT")
    except sqlite3.OperationalError:
        pass

    c.execute('CREATE INDEX IF NOT EXISTS idx_printer_ip ON logs(printer_ip)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON logs(timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_type ON logs(type)')

    c.execute('''
        CREATE TABLE IF NOT EXISTS printer_counters (
            ip TEXT PRIMARY KEY,
            device_type TEXT,
            print_total INTEGER,
            full_color INTEGER,
            black_white INTEGER,
            toner_level INTEGER,
            manual_override INTEGER DEFAULT 0,
            override_color TEXT,
            override_base_level INTEGER,
            override_start_total INTEGER,
            override_start_toner INTEGER,
            yield_per_page INTEGER DEFAULT 2000,
            force_estimate INTEGER DEFAULT 0,
            yield_learning_failures INTEGER DEFAULT 0,
            last_alert_codes TEXT,
            a3_total INTEGER,
            a4_total INTEGER,
            alert_codes TEXT,
            updated_at TEXT
        )
    ''')
    # اضافه کردن ستون device_type به جداول قدیمی
    try:
        c.execute("ALTER TABLE printer_counters ADD COLUMN device_type TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE printer_counters ADD COLUMN toner_level INTEGER DEFAULT NULL")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE printer_counters ADD COLUMN manual_override INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE printer_counters ADD COLUMN override_color TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE printer_counters ADD COLUMN override_base_level INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE printer_counters ADD COLUMN override_start_total INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE printer_counters ADD COLUMN override_start_toner INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE printer_counters ADD COLUMN yield_per_page INTEGER DEFAULT 2000")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE printer_counters ADD COLUMN force_estimate INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE printer_counters ADD COLUMN yield_learning_failures INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE printer_counters ADD COLUMN last_alert_codes TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass

    c.execute('''
        CREATE TABLE IF NOT EXISTS toner_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_ip TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            print_total INTEGER,
            toner_level INTEGER,
            yield_per_page INTEGER,
            source TEXT DEFAULT 'poll'
        )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_toner_history_ip_ts ON toner_history(printer_ip, timestamp)')

    c.execute('''
        CREATE TABLE IF NOT EXISTS sensor_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_ip TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            port INTEGER NOT NULL,
            kind TEXT NOT NULL,
            value REAL,
            unit TEXT,
            status TEXT
        )
    ''')
    # اگر نسخه‌ای از index در یک توقف ناقص خراب شده باشد، آن را اتمیک بازسازی کن.
    c.execute('DROP INDEX IF EXISTS idx_sensor_readings_kind')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sensor_readings_ip_ts ON sensor_readings(printer_ip, timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sensor_readings_kind ON sensor_readings(kind)')

    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT,
            google_id TEXT UNIQUE,
            reset_token_hash TEXT,
            reset_token_expires TEXT,
            role TEXT NOT NULL DEFAULT 'viewer',
            is_verified INTEGER NOT NULL DEFAULT 0,
            email_verified INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT,
            allowed_offices TEXT NOT NULL DEFAULT '[]',
            allowed_modules TEXT NOT NULL DEFAULT '[]'
        )
    ''')
    for column_sql in (
        "ALTER TABLE users ADD COLUMN password_hash TEXT",
        "ALTER TABLE users ADD COLUMN google_id TEXT",
        "ALTER TABLE users ADD COLUMN reset_token_hash TEXT",
        "ALTER TABLE users ADD COLUMN reset_token_expires TEXT",
        "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'viewer'",
        "ALTER TABLE users ADD COLUMN is_verified INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE users ADD COLUMN created_at TEXT",
        "ALTER TABLE users ADD COLUMN updated_at TEXT",
        "ALTER TABLE users ADD COLUMN last_login_at TEXT",
        "ALTER TABLE users ADD COLUMN allowed_offices TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE users ADD COLUMN allowed_modules TEXT NOT NULL DEFAULT '[]'",
    ):
        try:
            c.execute(column_sql)
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()

    # ─── راه‌اندازی جدول security audit ──────────────────────────
    try:
        from core.security_audit import init_security_audit
        init_security_audit()
    except Exception as e:
        log.exception("Failed to init security_audit: %s", e)

    # ─── راه‌اندازی جدول‌های Yield Engine ─────────────────────────
    # این import عمداً داخل تابع است تا circular import ایجاد نشود.
    try:
        from core.yield_engine import ensure_yield_tables
        ensure_yield_tables()
    except Exception as e:
        log.exception("Failed to init yield_engine tables: %s", e)


def _user_row_to_dict(row) -> Optional[dict]:
    if not row:
        return None
    role = row[7] or "viewer"
    is_verified = bool(row[8])
    return {
        "id": row[0],
        "username": row[1],
        "email": row[2],
        "password_hash": row[3],
        "google_id": row[4],
        "reset_token_hash": row[5],
        "reset_token_expires": row[6],
        "role": role,
        "is_verified": is_verified,
        "email_verified": is_verified,
        "is_active": bool(row[9]),
        "created_at": row[10],
        "updated_at": row[11],
        "last_login_at": row[12],
        "allowed_offices": _load_json_list(row[13] if len(row) > 13 else None),
        "allowed_modules": _load_json_list(row[14] if len(row) > 14 else None),
    }


def count_users() -> int:
    try:
        with db_connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
            return int(row[0] or 0)
    except Exception as e:
        log.exception(f"Error counting users: {e}")
        return 0


# SELECT مشترک برای جدول users (برای جلوگیری از تکرار)
_USER_SELECT_COLS = '''
    id, username, email, password_hash, google_id, reset_token_hash,
    reset_token_expires, role, is_verified, is_active, created_at,
    updated_at, last_login_at, allowed_offices, allowed_modules
'''


def _fetch_user(where_clause: str, params: tuple) -> Optional[dict]:
    """تابع helper: اجرای SELECT روی users با WHERE دلخواه."""
    try:
        with db_connection() as conn:
            row = conn.execute(
                f"SELECT {_USER_SELECT_COLS} FROM users WHERE {where_clause}",
                params,
            ).fetchone()
            return _user_row_to_dict(row)
    except Exception as e:
        log.exception(f"Error fetching user with {where_clause!r}: {e}")
        return None


def _execute_user_update(sql: str, params: tuple, action_desc: str = "user") -> bool:
    """تابع helper: اجرای UPDATE/DELETE روی users و برگرداندن True اگر >0 row متأثر شد."""
    try:
        with db_connection(commit=True) as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount > 0
    except Exception as e:
        log.exception(f"Error updating {action_desc}: {e}")
        return False


def get_user_by_id(user_id: int) -> Optional[dict]:
    return _fetch_user("id = ?", (user_id,))


def get_user_by_identifier(identifier: str) -> Optional[dict]:
    identifier = (identifier or "").strip().lower()
    if not identifier:
        return None
    return _fetch_user("lower(username) = ? OR lower(email) = ?", (identifier, identifier))


def get_user_by_email(email: str) -> Optional[dict]:
    email = (email or "").strip().lower()
    if not email:
        return None
    return _fetch_user("lower(email) = ?", (email,))


def get_user_by_google_id(google_id: str) -> Optional[dict]:
    google_id = (google_id or "").strip()
    if not google_id:
        return None
    return _fetch_user("google_id = ?", (google_id,))


def create_user(username: str, email: str, password_hash: str = None, google_id: str = None,
                role: str = "viewer", is_verified: bool = False,
                allowed_offices=None, allowed_modules=None) -> Optional[dict]:
    now = datetime.now().isoformat()
    username = (username or "").strip()
    if not USERNAME_RE.fullmatch(username):
        log.warning("Rejecting invalid username during user creation: %s", username)
        return None
    role = role if role in ("admin", "manager", "viewer") else "viewer"
    try:
        with db_connection(commit=True) as conn:
            cur = conn.execute(
                '''
                INSERT INTO users (username, email, password_hash, google_id, role,
                                   is_verified, email_verified, is_active, created_at, updated_at,
                                   allowed_offices, allowed_modules)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                ''',
                (username, email.strip().lower(), password_hash, google_id,
                 role, 1 if is_verified else 0, 1 if is_verified else 0, now, now,
                 _dump_json_list(allowed_offices), _dump_json_list(allowed_modules)),
            )
            user_id = cur.lastrowid
        return get_user_by_id(user_id)
    except Exception as e:
        log.exception(f"Error creating user {username}: {e}")
        return None


def update_user_password(user_id: int, password_hash: str) -> bool:
    return _execute_user_update(
        'UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?',
        (password_hash, datetime.now().isoformat(), user_id),
        f"password for user {user_id}",
    )


def set_user_google_id(user_id: int, google_id: str) -> bool:
    return _execute_user_update(
        'UPDATE users SET google_id = ?, updated_at = ? WHERE id = ?',
        (google_id, datetime.now().isoformat(), user_id),
        f"google_id for user {user_id}",
    )


def touch_user_login(user_id: int) -> None:
    now = datetime.now().isoformat()
    _execute_user_update(
        'UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?',
        (now, now, user_id),
        f"login timestamp for user {user_id}",
    )


def list_users() -> list:
    try:
        with db_connection() as conn:
            rows = conn.execute(
                f'''
                SELECT {_USER_SELECT_COLS}
                FROM users
                ORDER BY
                    CASE role
                        WHEN 'admin' THEN 0
                        WHEN 'manager' THEN 1
                        ELSE 2
                    END,
                    username COLLATE NOCASE ASC
                '''
            ).fetchall()
            return [_user_row_to_dict(r) for r in rows]
    except Exception as e:
        log.exception(f"Error listing users: {e}")
        return []


def update_user_role(user_id: int, role: str) -> bool:
    role = role if role in ("admin", "manager", "viewer") else None
    if not role:
        return False
    return _execute_user_update(
        'UPDATE users SET role = ?, updated_at = ? WHERE id = ?',
        (role, datetime.now().isoformat(), user_id),
        f"role for user {user_id}",
    )


def update_user_verified(user_id: int, is_verified: bool) -> bool:
    verified_int = 1 if is_verified else 0
    return _execute_user_update(
        'UPDATE users SET is_verified = ?, email_verified = ?, updated_at = ? WHERE id = ?',
        (verified_int, verified_int, datetime.now().isoformat(), user_id),
        f"verification for user {user_id}",
    )


def update_user_access(user_id: int, allowed_offices=None, allowed_modules=None) -> bool:
    return _execute_user_update(
        '''
        UPDATE users
        SET allowed_offices = ?, allowed_modules = ?, updated_at = ?
        WHERE id = ?
        ''',
        (_dump_json_list(allowed_offices), _dump_json_list(allowed_modules),
         datetime.now().isoformat(), user_id),
        f"access for user {user_id}",
    )


def delete_user(user_id: int) -> bool:
    return _execute_user_update(
        'DELETE FROM users WHERE id = ?',
        (user_id,),
        f"delete user {user_id}",
    )


def set_password_reset_token(user_id: int, token_hash: str, expires_at: str) -> bool:
    return _execute_user_update(
        '''
        UPDATE users
        SET reset_token_hash = ?, reset_token_expires = ?, updated_at = ?
        WHERE id = ?
        ''',
        (token_hash, expires_at, datetime.now().isoformat(), user_id),
        f"reset token for user {user_id}",
    )


def get_user_by_reset_token_hash(token_hash: str) -> Optional[dict]:
    if not token_hash:
        return None
    return _fetch_user("reset_token_hash = ?", (token_hash,))


def clear_password_reset_token(user_id: int) -> bool:
    return _execute_user_update(
        '''
        UPDATE users
        SET reset_token_hash = NULL, reset_token_expires = NULL, updated_at = ?
        WHERE id = ?
        ''',
        (datetime.now().isoformat(), user_id),
        f"clear reset token for user {user_id}",
    )


def add_event(ip: str, etype: str, details: dict):
    try:
        from core import store
        timestamp = details.get("timestamp", datetime.now().isoformat())
        message = details.get("message", "")
        pages = details.get("pages")
        color = details.get("color")
        code = details.get("code")
        severity = details.get("severity", "info")
        paper_size = details.get("paper_size")
        username = details.get("username")
        other = {k: v for k, v in details.items() if k not in _LOG_TOP_LEVEL_FIELDS}
        printer_name = None
        with store.printers_lock:
            for p in store.PRINTERS:
                if p["ip"] == ip:
                    printer_name = p["name"]
                    break
        # ----- Debounce noisy/duplicate events -----
        try:
            # configurable via env var `DB_DEBOUNCE_WINDOW_SECS` (seconds)
            # default: 60s (short window). Set to 600 for 10 minutes if desired.
            DEBOUNCE_WINDOW_SECS = int(os.getenv("DB_DEBOUNCE_WINDOW_SECS", "60"))
            raw_types = os.getenv(
                "DB_DEBOUNCE_TYPES",
                "SNMP_COUNTER_READ_ERROR,SNMP_ERROR,COUNTER_ANOMALY,PRINT_OVERFLOW,PRINT_GAP,SNMP_TIMEOUT,SNMP_RETRY,TONER_ESTIMATE"
            ) or ""
            DEBOUNCE_TYPES = set([s.strip() for s in raw_types.split(",") if s.strip()])

            if etype in DEBOUNCE_TYPES:
                cutoff = (datetime.now() - timedelta(seconds=DEBOUNCE_WINDOW_SECS)).isoformat()
                with db_connection() as conn:
                    # prefer code match when available (faster indexable comparison)
                    if code is not None:
                        cur = conn.execute(
                            "SELECT COUNT(1) FROM logs WHERE printer_ip=? AND type=? AND code=? AND timestamp>=?",
                            (ip, etype, str(code), cutoff),
                        ).fetchone()
                        if cur and cur[0] > 0:
                            log.debug(f"Debounced event {etype} for {ip} (code={code})")
                            return
                    # fallback to exact message match
                    if message:
                        cur = conn.execute(
                            "SELECT COUNT(1) FROM logs WHERE printer_ip=? AND type=? AND message=? AND timestamp>=?",
                            (ip, etype, message, cutoff),
                        ).fetchone()
                        if cur and cur[0] > 0:
                            log.debug(f"Debounced event {etype} for {ip} (same message)")
                            return
        except Exception:
            # on any error in debounce logic, proceed with inserting the event
            log.debug("debounce check failed, proceeding to add event")

        with db_connection(commit=True) as conn:
            if etype == "PRINT":
                prev_total = details.get("prev_total")
                current_total = details.get("current_total")
                if prev_total is not None and current_total is not None:
                    cutoff = (datetime.now() - timedelta(seconds=300)).isoformat()
                    duplicate = conn.execute(
                        """
                        SELECT 1 FROM logs
                        WHERE printer_ip = ? AND type = 'PRINT' AND timestamp >= ?
                          AND json_extract(details, '$.prev_total') = ?
                          AND json_extract(details, '$.current_total') = ?
                        LIMIT 1
                        """,
                        (ip, cutoff, prev_total, current_total),
                    ).fetchone()
                    if duplicate:
                        log.debug(
                            "Skipped duplicate PRINT for %s (%s -> %s)",
                            ip, prev_total, current_total,
                        )
                        return
                    overlap = conn.execute(
                        """
                        SELECT 1 FROM logs
                        WHERE printer_ip = ? AND type = 'PRINT' AND timestamp >= ?
                          AND json_extract(details, '$.prev_total') IS NOT NULL
                          AND json_extract(details, '$.current_total') IS NOT NULL
                          AND CAST(json_extract(details, '$.prev_total') AS INTEGER) < ?
                          AND CAST(json_extract(details, '$.current_total') AS INTEGER) > ?
                        LIMIT 1
                        """,
                        (ip, cutoff, current_total, prev_total),
                    ).fetchone()
                    if overlap:
                        log.warning(
                            "Skipped overlapping PRINT for %s (%s -> %s); recent transition already covers this range",
                            ip, prev_total, current_total,
                        )
                        return
            conn.execute('''
                INSERT INTO logs (printer_ip, printer_name, timestamp, type, message,
                                  pages, color, code, severity, paper_size, username, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (ip, printer_name, timestamp, etype, message,
                  pages, color, code, severity, paper_size, username,
                  json.dumps(other, ensure_ascii=False)))
    except Exception as e:
        log.exception(f"Error adding event to DB: {e}")


def _row_to_dict(row) -> dict:
    return {
        "printer_ip":   row[0],
        "printer_name": row[1],
        "timestamp":    row[2],
        "type":         row[3],
        "message":      row[4],
        "pages":        row[5],
        "color":        row[6],
        "code":         row[7],
        "severity":     row[8],
        "paper_size":   row[9],
        "username":     row[10],
        **json.loads(row[11] or "{}"),
    }


def get_log(ip: str, limit: int = 500, ips=None) -> list:
    try:
        params = []
        if ips:
            ips = [str(item).strip() for item in ips if str(item).strip()]
            if not ips:
                return []
            placeholders = ",".join(["?"] * len(ips))
            where = f"WHERE printer_ip IN ({placeholders})"
            params.extend(ips)
        else:
            where = "WHERE printer_ip = ?"
            params.append(ip)
        with db_connection() as conn:
            rows = conn.execute(
                f'''
                SELECT printer_ip, printer_name, timestamp, type, message,
                       pages, color, code, severity, paper_size, username, details
                FROM logs {where}
                ORDER BY timestamp DESC LIMIT ?
                ''',
                params + [limit],
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
    except Exception as e:
        log.exception(f"Error reading logs from DB: {e}")
        return []


def get_all_logs(start=None, end=None, limit: int = 1000, ip=None, ips=None) -> list:
    try:
        params = []
        conditions = []
        if ips:
            ips = [str(item).strip() for item in ips if str(item).strip()]
            if not ips:
                return []
            placeholders = ",".join(["?"] * len(ips))
            conditions.append(f"printer_ip IN ({placeholders})")
            params.extend(ips)
        elif ip:
            conditions.append("printer_ip = ?")
            params.append(ip)
        if start and end:
            conditions.append("timestamp BETWEEN ? AND ?")
            params.extend([start, end])
        elif start:
            conditions.append("timestamp >= ?")
            params.append(start)
        elif end:
            conditions.append("timestamp <= ?")
            params.append(end)

        query = '''
            SELECT printer_ip, printer_name, timestamp, type, message,
                   pages, color, code, severity, paper_size, username, details
            FROM logs
        '''
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with db_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [_row_to_dict(r) for r in rows]
    except Exception as e:
        log.exception(f"Error reading all logs: {e}")
        return []


def clear_logs(ip=None, ips=None, types=None) -> int:
    """
    پاک کردن رویدادها.
    اگر types مشخص شده باشد، فقط همان نوع‌ها پاک می‌شوند.
    در غیر این صورت، رویدادهای غیر از PRINT، SERVICE و REFILL پاک می‌شوند.
    """
    try:
        params = []
        where_clauses = []

        if ips:
            ips = [str(item).strip() for item in ips if str(item).strip()]
            if not ips:
                return 0
            ip_placeholders = ','.join(['?'] * len(ips))
            where_clauses.append(f"printer_ip IN ({ip_placeholders})")
            params.extend(ips)
        elif ip:
            where_clauses.append("printer_ip = ?")
            params.append(ip)

        if types:
            # اگر نوع‌های خاصی انتخاب شده باشند، دقیقاً همان‌ها را پاک کن
            type_placeholders = ','.join(['?'] * len(types))
            where_clauses.append(f"type IN ({type_placeholders})")
            params.extend(types)
        else:
            # رفتار قبلی: پاک کردن همه به جز موارد حیاتی
            keep_types = ('PRINT', 'SERVICE', 'REFILL')
            type_placeholders = ','.join(['?'] * len(keep_types))
            where_clauses.append(f"type NOT IN ({type_placeholders})")
            params.extend(keep_types)

        sql = "DELETE FROM logs"
        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)

        with db_connection(commit=True) as conn:
            cur = conn.execute(sql, params)
            deleted = cur.rowcount
        
        log.info(f"clear_logs: {deleted} رویداد پاک شد. فیلتر نوع: {types if types else 'auto'}")
        return deleted
    except Exception as e:
        log.exception(f"Error clearing logs: {e}")
        return 0


def ensure_printer_counters_columns():
    column_alters = (
        "ALTER TABLE printer_counters ADD COLUMN device_type TEXT",
        "ALTER TABLE printer_counters ADD COLUMN toner_level INTEGER DEFAULT NULL",
        "ALTER TABLE printer_counters ADD COLUMN manual_override INTEGER DEFAULT 0",
        "ALTER TABLE printer_counters ADD COLUMN override_color TEXT",
        "ALTER TABLE printer_counters ADD COLUMN override_base_level INTEGER",
        "ALTER TABLE printer_counters ADD COLUMN override_start_total INTEGER",
        "ALTER TABLE printer_counters ADD COLUMN override_start_toner INTEGER",
        "ALTER TABLE printer_counters ADD COLUMN yield_per_page INTEGER DEFAULT 2000",
        "ALTER TABLE printer_counters ADD COLUMN force_estimate INTEGER DEFAULT 0",
        "ALTER TABLE printer_counters ADD COLUMN yield_learning_failures INTEGER DEFAULT 0",
        "ALTER TABLE printer_counters ADD COLUMN last_alert_codes TEXT DEFAULT NULL",
    )
    try:
        with db_connection(commit=True) as conn:
            for column_sql in column_alters:
                try:
                    conn.execute(column_sql)
                except sqlite3.OperationalError:
                    pass  # ستون قبلاً وجود دارد
    except Exception as e:
        log.exception(f"Error ensuring printer_counters columns: {e}")


def prune_old_print_logs(days=90) -> int:
    """حذف خودکار لاگ‌های نوع PRINT که قدیمی‌تر از days روز هستند."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    try:
        with db_connection(commit=True) as conn:
            cur = conn.execute("DELETE FROM logs WHERE type='PRINT' AND timestamp < ?", (cutoff,))
            deleted = cur.rowcount
        if deleted:
            log.info(f"prune_old_print_logs: {deleted} رکورد PRINT قدیمی پاک شد")
        return deleted
    except Exception as e:
        log.exception(f"Error pruning old logs: {e}")
        return 0


# نام ستون‌های printer_counters (برای جلوگیری از تکرار)
_COUNTERS_COLS = (
    "device_type, print_total, full_color, black_white, toner_level, manual_override, "
    "override_color, override_base_level, override_start_total, override_start_toner, "
    "yield_per_page, force_estimate, yield_learning_failures, last_alert_codes, "
    "a3_total, a4_total, alert_codes, updated_at"
)


def load_printer_counters(ip: str) -> Optional[dict]:
    """بارگذاری آخرین مقادیر شمارنده از دیتابیس"""
    try:
        with db_connection() as conn:
            row = conn.execute(
                f"SELECT {_COUNTERS_COLS} FROM printer_counters WHERE ip = ?",
                (ip,),
            ).fetchone()
        if not row:
            return None
        return {
            "device_type": row[0],
            "print_total": row[1],
            "full_color": row[2],
            "black_white": row[3],
            "toner_level": row[4],
            "manual_override": row[5] or 0,
            "override_color": row[6],
            "override_base_level": row[7],
            "override_start_total": row[8],
            "override_start_toner": row[9],
            "yield_per_page": row[10] if row[10] is not None else 2000,
            "force_estimate": row[11] or 0,
            "yield_learning_failures": row[12] or 0,
            "last_alert_codes": _load_json_list(row[13]),
            "a3_total": row[14],
            "a4_total": row[15],
            "alert_codes": _load_json_list(row[16]),
            "updated_at": row[17],
        }
    except Exception as e:
        log.exception(f"Error loading counters for {ip}: {e}")
        return None


def save_printer_counters(ip: str, data: dict):
    """ذخیره مقادیر شمارنده در دیتابیس"""
    ensure_printer_counters_columns()
    updated_at = data.get("updated_at") or datetime.now().isoformat()
    try:
        with db_connection(commit=True) as conn:
            conn.execute('''
                INSERT OR REPLACE INTO printer_counters
                (ip, device_type, print_total, full_color, black_white, toner_level, manual_override,
                 override_color, override_base_level, override_start_total, override_start_toner,
                 yield_per_page, force_estimate, yield_learning_failures,
                 last_alert_codes, a3_total, a4_total, alert_codes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                ip,
                data.get("device_type"),
                data.get("print_total"),
                data.get("full_color"),
                data.get("black_white"),
                data.get("toner_level"),
                data.get("manual_override", 0),
                data.get("override_color"),
                data.get("override_base_level"),
                data.get("override_start_total"),
                data.get("override_start_toner"),
                data.get("yield_per_page", 2000),
                data.get("force_estimate", 0),
                data.get("yield_learning_failures", 0),
                json.dumps(data.get("last_alert_codes", [])),
                data.get("a3_total"),
                data.get("a4_total"),
                json.dumps(data.get("alert_codes", [])),
                updated_at,
            ))
    except Exception as e:
        log.exception(f"Error saving counters for {ip}: {e}")


def delete_printer_counters(ip: str) -> bool:
    """حذف baseline/counters یک پرینتر؛ برای حذف دستگاه یا جلوگیری از reuse شدن IP."""
    try:
        with db_connection(commit=True) as conn:
            conn.execute("DELETE FROM printer_counters WHERE ip = ?", (ip,))
        return True
    except Exception as e:
        log.exception(f"Error deleting counters for {ip}: {e}")
        return False


def record_sensor_readings(ip: str, readings: list, timestamp: str = None):
    """ذخیره خوانش‌های سنسور برای نمودار میانگین روزانه.

    readings: [{"port": 1, "kind": "temperature"|"humidity", "value": 25.3, "unit": "°C", "status": "active"}]
    """
    if not readings:
        return
    ts = timestamp or datetime.now().isoformat()
    rows = []
    for item in readings:
        try:
            value = item.get("value")
            if value is None:
                continue
            rows.append((
                ip,
                ts,
                int(item.get("port") or 0),
                str(item.get("kind") or ""),
                float(value),
                item.get("unit"),
                item.get("status"),
            ))
        except (TypeError, ValueError):
            continue
    if not rows:
        return
    try:
        with db_connection(commit=True) as conn:
            conn.executemany(
                """
                INSERT INTO sensor_readings (printer_ip, timestamp, port, kind, value, unit, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
    except Exception as e:
        log.exception(f"Error recording sensor readings for {ip}: {e}")


def record_toner_snapshot(ip: str, print_total: int = None, toner_level: int = None,
                         yield_per_page: int = None, timestamp: str = None,
                         source: str = "poll"):
    """ذخیره snapshot از شمارنده/تونر برای یادگیری تاریخی yield."""
    if print_total is None and toner_level is None:
        return
    try:
        with db_connection(commit=True) as conn:
            conn.execute(
                """
                INSERT INTO toner_history (printer_ip, timestamp, print_total, toner_level, yield_per_page, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    ip,
                    timestamp or datetime.now().isoformat(),
                    print_total,
                    toner_level,
                    yield_per_page,
                    source,
                ),
            )
    except Exception as e:
        log.exception(f"Error recording toner snapshot for {ip}: {e}")


def estimate_yield_from_history(ip: str, days: int = 7, min_points: int = 3, min_pages: int = 500):
    """تخمین yield_per_page از روی snapshotهای تاریخی toner_history."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    try:
        with db_connection() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, print_total, toner_level
                FROM toner_history
                WHERE printer_ip = ? AND timestamp >= ?
                ORDER BY timestamp ASC
                """,
                (ip, cutoff),
            ).fetchall()
        if len(rows) < 2:
            return None

        total_pages = 0
        total_drop = 0
        sample_points = 0
        days_seen = set()
        prev_total = None
        prev_toner = None
        for ts, print_total, toner_level in rows:
            if ts:
                days_seen.add(str(ts)[:10])
            if print_total is None or toner_level is None:
                prev_total, prev_toner = print_total, toner_level
                continue
            if prev_total is None or prev_toner is None:
                prev_total, prev_toner = print_total, toner_level
                continue
            delta_pages = int(print_total) - int(prev_total)
            toner_drop = int(prev_toner) - int(toner_level)
            if delta_pages > 0 and toner_drop > 0:
                total_pages += delta_pages
                total_drop += toner_drop
                sample_points += 1
            prev_total, prev_toner = print_total, toner_level

        if total_drop <= 0:
            return None
        # برای جلوگیری از yield اشتباه، هم تعداد نمونه و هم تعداد صفحات باید کافی باشد.
        if sample_points < min_points or total_pages < min_pages:
            return None

        estimated_yield = int(round(total_pages * 100.0 / total_drop))
        if not 300 <= estimated_yield <= 20000:
            return None
        return {
            "yield_per_page": estimated_yield,
            "sample_points": sample_points,
            "total_pages": total_pages,
            "total_drop": total_drop,
            "days_seen": len(days_seen),
        }
    except Exception as e:
        log.exception(f"Error estimating historical yield for {ip}: {e}")
        return None


def get_reset_history(ip=None, limit: int = 1000, ips=None, current_totals=None) -> list:
    """دریافت تاریخچه تنظیم مجدد کارتریج‌ها برای گزارش‌گیری Excel."""
    try:
        params = ['REFILL']
        conditions = ["type = ?"]
        if ips:
            ips = [str(item).strip() for item in ips if str(item).strip()]
            if not ips:
                return []
            placeholders = ",".join(["?"] * len(ips))
            conditions.append(f"printer_ip IN ({placeholders})")
            params.extend(ips)
        elif ip:
            conditions.append("printer_ip = ?")
            params.append(ip)

        query = f'''
            SELECT printer_ip, printer_name, timestamp, type, message,
                   pages, color, code, severity, paper_size, username, details
            FROM logs
            WHERE {' AND '.join(conditions)}
            ORDER BY printer_ip ASC, timestamp ASC
        '''
        with db_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        events = [_row_to_dict(r) for r in rows]
        events = [e for e in events if e.get('manual_reset')]
        if not events:
            return []

        current_totals = current_totals or {}
        grouped = {}
        for ev in events:
            grouped.setdefault(ev.get('printer_ip'), []).append(ev)

        history = []
        for printer_ip, printer_events in grouped.items():
            printer_events.sort(key=lambda ev: ev.get('timestamp') or '')
            for idx, event in enumerate(printer_events):
                total_at_reset = event.get('total_at_reset')
                next_total = None
                if idx + 1 < len(printer_events):
                    next_total = printer_events[idx + 1].get('total_at_reset')
                else:
                    next_total = current_totals.get(printer_ip)
                    if next_total is None:
                        counters = load_printer_counters(printer_ip) or {}
                        next_total = counters.get('print_total')
                try:
                    printed_after = int(next_total) - int(total_at_reset) if next_total is not None and total_at_reset is not None else None
                except (TypeError, ValueError):
                    printed_after = None
                if printed_after is not None and printed_after < 0:
                    printed_after = 0
                history.append({
                    'timestamp': event.get('timestamp'),
                    'printer_ip': printer_ip,
                    'printer_name': event.get('printer_name'),
                    'color': event.get('reset_color') or event.get('color') or '',
                    'set_level': event.get('set_level'),
                    'total_pages_at_reset': total_at_reset,
                    'pages_printed_after_reset': printed_after,
                    'pages_per_1pct': event.get('yield_per_page_at_reset') or event.get('yield_per_page'),
                })

        history.sort(key=lambda item: item.get('timestamp') or '', reverse=True)
        return history[:limit]
    except Exception as e:
        log.exception(f"Error building reset history: {e}")
        return []



