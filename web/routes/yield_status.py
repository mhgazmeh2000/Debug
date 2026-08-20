from flask import Blueprint, jsonify, request

from core.yield_engine import get_yield_status, write_yield_status_report
from web.auth import admin_required

bp = Blueprint("yield_status", __name__)


@bp.route("/api/yield/status")
@admin_required
def api_yield_status():
    """گزارش وضعیت Yield Engine به‌صورت per-cartridge/per-color."""
    ip = (request.args.get("ip") or "").strip() or None
    rows = get_yield_status(ip=ip)
    summary = {
        "total_cartridges": len(rows),
        "high": sum(1 for r in rows if r.get("confidence") == "high"),
        "medium": sum(1 for r in rows if r.get("confidence") == "medium"),
        "low": sum(1 for r in rows if r.get("confidence") == "low"),
        "default": sum(1 for r in rows if r.get("yield_source") == "default"),
        "shared_profile": sum(1 for r in rows if r.get("yield_source") == "shared_profile"),
        "auto_learn": sum(1 for r in rows if r.get("yield_source") == "auto_learn"),
    }
    return jsonify({"summary": summary, "items": rows})


@bp.route("/api/yield/report", methods=["POST"])
@admin_required
def api_yield_report():
    """بازسازی فایل runtime `yield_status_report.txt`."""
    write_yield_status_report()
    return jsonify({"status": "ok", "file": "yield_status_report.txt"})
