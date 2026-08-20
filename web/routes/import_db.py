import os
import shutil
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from flask import Blueprint, jsonify, request, session
from web.auth import admin_required
from core.database import db_connection, init_db
from config.settings import DB_PATH

log = logging.getLogger("PrinterMonitor")
bp = Blueprint("import_db", __name__)

TEMP_UPLOAD_PATH = "temp_import.db"


def _temp_upload_path() -> str:
    path = session.get("import_temp_path")
    return path if isinstance(path, str) and path else TEMP_UPLOAD_PATH

# جدول‌هایی که import آن‌ها مجاز است. عمداً users/security_events وارد نمی‌شوند.
ALLOWED_IMPORT_TABLES = {
    "logs": {
        "label": "لاگ‌ها و رویدادها",
        "ip_col": "printer_ip",
        "ts_col": "timestamp",
        "skip_id": True,
        "dedupe_cols": ("printer_ip", "timestamp", "type", "message", "pages", "color", "code"),
    },
    "printer_counters": {
        "label": "آخرین شمارنده‌ها / baseline",
        "ip_col": "ip",
        "ts_col": "updated_at",
        "replaceable": True,
        "dedupe_cols": ("ip",),
    },
    "toner_history": {
        "label": "تاریخچه تونر legacy",
        "ip_col": "printer_ip",
        "ts_col": "timestamp",
        "skip_id": True,
        "dedupe_cols": ("printer_ip", "timestamp", "print_total", "toner_level", "source"),
    },
    "sensor_readings": {
        "label": "داده‌های سنسور",
        "ip_col": "printer_ip",
        "ts_col": "timestamp",
        "skip_id": True,
        "dedupe_cols": ("printer_ip", "timestamp", "port", "kind"),
    },
    "cartridge_state": {
        "label": "Yield Engine: وضعیت کارتریج‌ها",
        "ip_col": "printer_ip",
        "ts_col": "updated_at",
        "replaceable": True,
        "dedupe_cols": ("printer_ip", "color"),
    },
    "yield_samples": {
        "label": "Yield Engine: نمونه‌های یادگیری",
        "ip_col": "printer_ip",
        "ts_col": "created_at",
        "skip_id": True,
        "dedupe_cols": ("printer_ip", "color", "created_at", "start_counter", "end_counter", "start_level", "end_level"),
    },
    "toner_snapshots_v2": {
        "label": "Yield Engine: snapshotهای تونر",
        "ip_col": "printer_ip",
        "ts_col": "timestamp",
        "skip_id": True,
        "dedupe_cols": ("printer_ip", "color", "timestamp", "usage_counter", "toner_level"),
    },
    "cartridge_yield_profiles": {
        "label": "Yield Engine: پروفایل‌های مشترک",
        "ip_col": "source_printer_ip",
        "ts_col": "updated_at",
        "replaceable": True,
        "dedupe_cols": ("cartridge_key",),
    },
}

YIELD_TABLES = ("cartridge_state", "yield_samples", "toner_snapshots_v2", "cartridge_yield_profiles")


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _tables(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _columns(conn, table: str) -> list:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({_q(table)})").fetchall()]


def _table_count(conn, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {_q(table)}").fetchone()[0] or 0)
    except Exception:
        return 0


def _table_range(conn, table: str, ts_col: str):
    cols = _columns(conn, table)
    if ts_col not in cols:
        return {"start": None, "end": None}
    row = conn.execute(f"SELECT MIN({_q(ts_col)}), MAX({_q(ts_col)}) FROM {_q(table)}").fetchone()
    return {"start": row[0], "end": row[1]}


def _table_printers(conn, table: str, ip_col: str) -> list:
    cols = _columns(conn, table)
    if ip_col not in cols:
        return []
    name_expr = "NULL"
    if "printer_name" in cols:
        name_expr = "printer_name"
    elif "name" in cols:
        name_expr = "name"
    rows = conn.execute(
        f"SELECT DISTINCT {_q(ip_col)}, {name_expr} FROM {_q(table)} WHERE {_q(ip_col)} IS NOT NULL ORDER BY {_q(ip_col)}"
    ).fetchall()
    return [{"ip": r[0], "name": r[1]} for r in rows if r[0]]


def _make_backup() -> str:
    if not os.path.exists(DB_PATH):
        return ""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{DB_PATH}.backup-{ts}"
    shutil.copy2(DB_PATH, backup_path)
    return backup_path


def _build_where(cols: list, meta: dict, filters: dict):
    where = ["1=1"]
    params = []
    ip_col = meta.get("ip_col")
    ts_col = meta.get("ts_col")
    ips = [str(x).strip() for x in (filters.get("ips") or []) if str(x).strip()]
    if ips and ip_col in cols:
        where.append(f"{_q(ip_col)} IN ({','.join(['?'] * len(ips))})")
        params.extend(ips)
    if filters.get("start_date") and ts_col in cols:
        where.append(f"{_q(ts_col)} >= ?")
        params.append(filters["start_date"])
    if filters.get("end_date") and ts_col in cols:
        where.append(f"{_q(ts_col)} <= ?")
        params.append(filters["end_date"])
    return " AND ".join(where), params


def _exists_by_cols(conn, table: str, cols: tuple, row_map: dict) -> bool:
    usable = [c for c in cols if c in row_map]
    if not usable:
        return False
    where = " AND ".join([f"({_q(c)} IS ? OR {_q(c)} = ?)" for c in usable])
    params = []
    for c in usable:
        params.extend([row_map.get(c), row_map.get(c)])
    return conn.execute(f"SELECT 1 FROM {_q(table)} WHERE {where} LIMIT 1", params).fetchone() is not None


def _import_table(source_conn, target_conn, table: str, filters: dict, duplicate_mode: str):
    meta = ALLOWED_IMPORT_TABLES[table]
    src_cols_all = _columns(source_conn, table)
    dst_cols_all = _columns(target_conn, table)
    if not src_cols_all or not dst_cols_all:
        return {"imported": 0, "skipped": 0, "reason": "ستون مشترک وجود ندارد"}

    common_cols = [c for c in src_cols_all if c in dst_cols_all]
    if meta.get("skip_id") and "id" in common_cols:
        common_cols.remove("id")
    if not common_cols:
        return {"imported": 0, "skipped": 0, "reason": "ستون مشترک وجود ندارد"}

    where, params = _build_where(src_cols_all, meta, filters)
    select_sql = f"SELECT {','.join(_q(c) for c in common_cols)} FROM {_q(table)} WHERE {where}"
    rows = source_conn.execute(select_sql, params).fetchall()

    placeholders = ",".join(["?"] * len(common_cols))
    cols_sql = ",".join(_q(c) for c in common_cols)
    verb = "INSERT"
    if duplicate_mode == "replace" and meta.get("replaceable"):
        verb = "INSERT OR REPLACE"
    insert_sql = f"{verb} INTO {_q(table)} ({cols_sql}) VALUES ({placeholders})"

    imported = 0
    skipped = 0
    dedupe_cols = meta.get("dedupe_cols") or ()
    for row in rows:
        row_map = dict(zip(common_cols, row))
        if duplicate_mode == "skip" and _exists_by_cols(target_conn, table, dedupe_cols, row_map):
            skipped += 1
            continue
        try:
            target_conn.execute(insert_sql, row)
            imported += 1
        except sqlite3.IntegrityError:
            if duplicate_mode == "replace" and not meta.get("replaceable"):
                skipped += 1
            else:
                skipped += 1
    return {"imported": imported, "skipped": skipped, "rows_seen": len(rows)}


@bp.route('/api/import/analyze', methods=['POST'])
@admin_required
def analyze_db():
    if 'file' not in request.files:
        return jsonify({"error": "فایلی ارسال نشده است"}), 400
    file = request.files['file']
    if not file.filename.lower().endswith('.db'):
        return jsonify({"error": "فرمت فایل باید .db باشد"}), 400

    old_path = session.get("import_temp_path")
    if old_path and old_path != TEMP_UPLOAD_PATH:
        try:
            if os.path.exists(old_path):
                os.remove(old_path)
        except OSError:
            log.warning("Could not remove previous import upload: %s", old_path)
    temp_path = f"temp_import_{os.getpid()}_{int(datetime.now().timestamp() * 1000)}.db"
    session["import_temp_path"] = temp_path
    file.save(temp_path)
    try:
        with sqlite3.connect(temp_path) as conn:
            existing_tables = _tables(conn)
            importable = [t for t in ALLOWED_IMPORT_TABLES if t in existing_tables]
            summary = {"tables": {}, "printers_in_logs": [], "all_printers": []}
            printer_map = {}
            for table in importable:
                meta = ALLOWED_IMPORT_TABLES[table]
                info = {
                    "label": meta["label"],
                    "count": _table_count(conn, table),
                    "range": _table_range(conn, table, meta.get("ts_col")),
                }
                printers = _table_printers(conn, table, meta.get("ip_col"))
                info["printers"] = printers
                summary["tables"][table] = info
                for p in printers:
                    printer_map.setdefault(p["ip"], p.get("name"))

            # سازگاری با UI قبلی
            logs_info = summary["tables"].get("logs", {})
            summary["logs_count"] = logs_info.get("count", 0)
            summary["logs_range"] = logs_info.get("range", {"start": None, "end": None})
            summary["printers_in_logs"] = summary["tables"].get("logs", {}).get("printers", [])
            summary["all_printers"] = [{"ip": ip, "name": name} for ip, name in sorted(printer_map.items())]
            summary["importable_tables"] = importable
            return jsonify({"status": "ok", "summary": summary})
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        session.pop("import_temp_path", None)
        log.exception("Import analyze failed")
        return jsonify({"error": "خطا در تحلیل دیتابیس"}), 500


@bp.route('/api/import/confirm', methods=['POST'])
@admin_required
def confirm_import():
    temp_path = _temp_upload_path()
    if not os.path.exists(temp_path):
        return jsonify({"error": "فایل موقت یافت نشد. مجدداً آپلود کنید."}), 400
    data = request.get_json() or {}
    filters = data.get("filters", {})
    options = data.get("options", {})
    selected_tables = data.get("tables") or ["logs"]
    selected_tables = [t for t in selected_tables if t in ALLOWED_IMPORT_TABLES]
    if not selected_tables:
        return jsonify({"error": "هیچ بخشی برای import انتخاب نشده است"}), 400

    duplicate_mode = options.get("duplicate_mode") or "skip"  # skip | allow | replace
    if duplicate_mode not in ("skip", "allow", "replace"):
        duplicate_mode = "skip"

    backup_path = ""
    try:
        init_db()
        if options.get("backup", True):
            backup_path = _make_backup()

        source_conn = sqlite3.connect(temp_path)
        source_tables = _tables(source_conn)
        results = {}
        with db_connection(commit=True) as target_conn:
            target_tables = _tables(target_conn)
            for table in selected_tables:
                if table not in source_tables:
                    results[table] = {"imported": 0, "skipped": 0, "reason": "در فایل مبدا وجود ندارد"}
                    continue
                if table not in target_tables:
                    results[table] = {"imported": 0, "skipped": 0, "reason": "در دیتابیس مقصد وجود ندارد"}
                    continue
                results[table] = _import_table(source_conn, target_conn, table, filters, duplicate_mode)
        source_conn.close()
        os.remove(temp_path)
        session.pop("import_temp_path", None)
        total_imported = sum(r.get("imported", 0) for r in results.values())
        total_skipped = sum(r.get("skipped", 0) for r in results.values())
        return jsonify({
            "status": "success",
            "imported": total_imported,
            "skipped": total_skipped,
            "results": results,
            "backup": backup_path,
        })
    except Exception as e:
        log.exception("Import failed")
        return jsonify({"error": "خطای داخلی هنگام import رخ داد", "backup": backup_path}), 500
