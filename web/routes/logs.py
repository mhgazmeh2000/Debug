from flask import Blueprint, jsonify, request
from flask_login import current_user
from core.database import get_log, get_all_logs, clear_logs, add_event
from core import store
from web.auth import allowed_printer_ips, user_can_access_office, user_allowed_offices

bp = Blueprint("logs", __name__)


@bp.route('/api/printer/<path:ip>/log')
def api_printer_log(ip):
    limit = request.args.get('limit', default=500, type=int)
    if not user_can_access_office(current_user, ip):
        return jsonify({"error": "forbidden"}), 403
    logs = get_log(ip, limit)
    return jsonify({"ip": ip, "events": logs})


@bp.route('/api/logs/all')
def api_all_logs():
    start = request.args.get('start')
    end = request.args.get('end')
    limit = request.args.get('limit', default=1000, type=int)
    ip = request.args.get('ip')
    allowed_offices = user_allowed_offices(current_user)
    allowed_ips = allowed_printer_ips(current_user)
    if allowed_offices and getattr(current_user, "role", None) != "admin":
        if ip and ip not in allowed_ips:
            return jsonify({"error": "forbidden"}), 403
        logs = get_all_logs(start, end, limit, ips=allowed_ips, ip=ip if ip in allowed_ips else None)
    else:
        logs = get_all_logs(start, end, limit, ip)
    return jsonify({"events": logs, "total": len(logs)})


@bp.route('/api/logs/clear', methods=['POST'])
def api_clear_logs():
    data = request.json or {}
    ip = data.get("ip")
    types = data.get("types") # لیستی از نوع‌های انتخابی برای حذف
    
    if types and not isinstance(types, list):
        return jsonify({"error": "فرمت types نامعتبر است"}), 400

    allowed_offices = user_allowed_offices(current_user)
    allowed_ips = allowed_printer_ips(current_user)
    
    if allowed_offices and getattr(current_user, "role", None) != "admin":
        if ip:
            if ip not in allowed_ips:
                return jsonify({"error": "forbidden"}), 403
            deleted = clear_logs(ip, types=types)
        else:
            deleted = clear_logs(ips=allowed_ips, types=types)
    else:
        deleted = clear_logs(ip, types=types)
        
    return jsonify({
        "status": "cleared", 
        "deleted": deleted,
        "note": "رویدادها با موفقیت پاکسازی شدند"
    })


@bp.route('/api/events/manual', methods=['POST'])
def api_manual_event():
    """
    ثبت دستی رویداد سرویس یا شارژ کارتریج
    POST {"ip":"...", "type":"SERVICE"|"REFILL", "notes":"...", "technician":"..."}
    """
    import logging
    log = logging.getLogger("PrinterMonitor")

    data = request.get_json() or {}
    ip = data.get("ip", "").strip()
    etype = data.get("type", "").strip().upper()
    notes = data.get("notes", "").strip()
    technician = data.get("technician", "").strip()

    if not ip:
        return jsonify({"error": "ip الزامی است"}), 400
    if etype not in ("SERVICE", "REFILL"):
        return jsonify({"error": "نوع رویداد باید SERVICE یا REFILL باشد"}), 400
    if not notes:
        return jsonify({"error": "توضیحات الزامی است"}), 400

    with store.printers_lock:
        known = any(p["ip"] == ip for p in store.PRINTERS)
    if not known:
        return jsonify({"error": f"پرینتر {ip} یافت نشد"}), 404

    label = "سرویس دستگاه" if etype == "SERVICE" else "شارژ کارتریج"
    message = f"{label}: {notes}"
    if technician:
        message += f" — تکنسین: {technician}"

    add_event(ip, etype, {
        "message": message,
        "severity": "info",
        "notes": notes,
        "technician": technician,
        "manual": True,
    })
    log.info(f"[manual] {etype} ثبت شد برای {ip}: {notes[:60]}")
    return jsonify({"status": "ok", "type": etype, "ip": ip, "message": message})