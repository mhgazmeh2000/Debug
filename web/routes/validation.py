from flask import Blueprint, jsonify
from core import store
from core.collectors.base import validate_counter_consistency
from core.oid.scanner import _load_oid_profiles
from core.oid.validator import validate_oid_value

bp = Blueprint("validation", __name__)


@bp.route('/api/validate/counters', methods=['GET'])
def api_validate_counters():
    with store.data_lock:
        snap = dict(store.printer_data)

    results = {}
    for ip, d in snap.items():
        if not d.get("online"): continue
        counters = d.get("counters", {})
        brand    = d.get("brand", "")
        warns    = validate_counter_consistency(counters, brand)
        results[ip] = {
            "name":     d.get("name"),
            "brand":    brand,
            "counters": {k: v for k, v in counters.items() if not k.startswith("_")},
            "warnings": warns,
            "status":   "⚠ هشدار" if warns else "✅ سالم",
        }

    total_warn = sum(len(r["warnings"]) for r in results.values())
    return jsonify({
        "checked":        len(results),
        "total_warnings": total_warn,
        "overall":        "⚠ نیاز به بررسی" if total_warn else "✅ همه سالم",
        "printers":       results,
    })


@bp.route('/api/validate/oids/<ip>', methods=['GET'])
def api_validate_oid_profile(ip):
    profiles = _load_oid_profiles()
    if ip not in profiles:
        return jsonify({"error": f"پروفایل {ip} یافت نشد"}), 404

    p     = profiles[ip]
    brand = p.get("brand", "")

    revalidated = {}
    for key, oid_data in p.get("oids", {}).items():
        if not oid_data.get("active"): continue
        raw_val = oid_data.get("last_value")
        cat     = oid_data.get("category", "")
        try:    val = int(raw_val) if cat in ("counter","supply","tray","status") else raw_val
        except: val = raw_val
        ok, reason = validate_oid_value(key, val, cat)
        revalidated[key] = {**oid_data, "validation": "✅" if ok else "⚠",
                            "validation_reason": reason}

    with store.data_lock:
        current = store.printer_data.get(ip, {})
    warns = validate_counter_consistency(current.get("counters", {}), brand)

    return jsonify({
        "ip":                   ip,
        "brand":                brand,
        "scanned_at":           p.get("scanned_at"),
        "oid_active":           p.get("oid_active", 0),
        "oid_rejected":         p.get("oid_rejected", 0),
        "consistency_warnings": warns,
        "consistency_status":   "⚠ هشدار" if warns else "✅ سالم",
        "oids":                 revalidated,
        "rejected_oids":        p.get("rejected_oids", {}),
    })
