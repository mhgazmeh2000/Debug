import threading
import logging
from flask import Blueprint, jsonify, request
from core.oid.scanner import scan_printer_oids, startup_scan_all, _load_oid_profiles
from core.oid.catalog import OID_CATALOG
from core.oid.validator import validate_oid_value
from core import store

bp = Blueprint("scan", __name__)
log = logging.getLogger("PrinterMonitor")


@bp.route('/api/scan/oids', methods=['POST'])
def api_scan_oids():
    data      = request.json or {}
    ip        = data.get("ip", "").strip()
    community = data.get("community", "public")
    force     = data.get("force", False)
    if not ip:
        return jsonify({"error": "ip الزامی است"}), 400
    try:
        profile = scan_printer_oids(ip, community, force=force)
        return jsonify({
            "status":      "ok",
            "ip":          ip,
            "summary":     profile["summary"],
            "brand":       profile["brand"],
            "oid_active":  profile["oid_active"],
            "oid_total":   profile["oid_total"],
            "scan_ms":     profile["scan_ms"],
            "active_oids": {k: v for k, v in profile.get("oids", {}).items()
                            if v.get("active")},
        })
    except Exception:
        log.exception("Error in API scan oids for %s", ip)
        return jsonify({"error": "خطای داخلی سرور رخ داد"}), 500


@bp.route('/api/scan/oids/<ip>', methods=['GET'])
def api_get_oid_profile(ip):
    profiles = _load_oid_profiles()
    if ip not in profiles:
        return jsonify({"error": f"پروفایل {ip} یافت نشد — ابتدا scan کنید"}), 404
    p         = profiles[ip]
    oids_data = p.get("oids", {})
    if not oids_data and "active_oids" in p:
        for key, oid in p["active_oids"].items():
            cat = OID_CATALOG.get(key, (oid, "unknown", "unknown", key))
            oids_data[key] = {"oid": oid, "type": cat[1], "category": cat[2],
                              "description": cat[3], "active": True,
                              "last_value": p.get("current_vals", {}).get(key)}
    return jsonify({
        "ip":           ip,
        "brand":        p["brand"],
        "scanned_at":   p["scanned_at"],
        "scan_ms":      p["scan_ms"],
        "summary":      p["summary"],
        "oid_active":   p.get("oid_active", 0),
        "oid_inactive": p.get("oid_inactive", 0),
        "oid_total":    p["oid_total"],
        "oids":         dict(sorted(oids_data.items(),
                              key=lambda x: x[1].get("category", ""))),
    })


@bp.route('/api/scan/all', methods=['POST'])
def api_scan_all():
    force = (request.json or {}).get("force", False)
    with store.printers_lock:
        printers_copy = list(store.PRINTERS)
    threading.Thread(
        target=lambda: startup_scan_all(printers_copy, force=force),
        daemon=True,
    ).start()
    return jsonify({
        "status":   "scanning",
        "message":  f"Scan {len(printers_copy)} پرینتر شروع شد",
        "printers": [p["ip"] for p in printers_copy],
    })


@bp.route('/api/scan/profiles', methods=['GET'])
def api_all_oid_profiles():
    profiles = _load_oid_profiles()
    return jsonify({
        ip: {
            "brand":      p.get("brand"),
            "scanned_at": p.get("scanned_at"),
            "scan_ms":    p.get("scan_ms"),
            "oid_active": p.get("oid_active"),
            "summary":    p.get("summary", {}),
        }
        for ip, p in profiles.items()
    })
