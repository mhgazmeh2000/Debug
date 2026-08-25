import os
import re
import threading
import logging
from flask import Blueprint, jsonify, request
from flask_login import current_user
from core import store
from core.database import add_event
from core.poller import collect, poll_one
from core.oid.scanner import scan_printer_oids
from config.settings import POLL_INTERVAL, TONER_ALERT_THRESHOLDS
from web.auth import user_can_access_office, allowed_printer_ips, user_allowed_offices, admin_required

log = logging.getLogger("PrinterMonitor")

bp = Blueprint("printers", __name__)

_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _is_valid_ip(ip: str) -> bool:
    """اعتبارسنجی IPv4 — جلوگیری از ورود رشته‌های نامعتبر/مخرب به printers.json"""
    if not ip or len(ip) > 15 or not _IP_RE.match(ip):
        return False
    try:
        return all(0 <= int(part) <= 255 for part in ip.split("."))
    except (TypeError, ValueError):
        return False


def _debug_endpoints_enabled() -> bool:
    """🔒 endpointهای دیباگ فقط با ENABLE_DEBUG_ENDPOINTS=1 فعال می‌شوند."""
    return os.getenv("ENABLE_DEBUG_ENDPOINTS", "0") == "1"


def _debug_forbidden():
    return jsonify({"error": "not found"}), 404


def _attach_yield_metadata(printer_data: dict) -> dict:
    """اضافه کردن yield/capacity به خروجی API حتی اگر snapshot فعلی قدیمی باشد."""
    if not isinstance(printer_data, dict):
        return printer_data
    ip = printer_data.get("ip")
    if not ip:
        return printer_data
    prev = store._prev.get(ip) or {}
    try:
        ypp = int(prev.get("yield_per_page", 2000) or 2000)
    except (TypeError, ValueError):
        ypp = 2000
    ysrc = "default" if ypp == 2000 else "learned"
    if prev.get("force_estimate"):
        ysrc = "forced_estimate"

    counters = printer_data.setdefault("counters", {})
    if isinstance(counters, dict):
        counters.setdefault("yield_per_page", ypp)
        counters.setdefault("yield_source", ysrc)
        counters.setdefault("force_estimate", prev.get("force_estimate", 0))
        counters.setdefault("yield_learning_failures", prev.get("yield_learning_failures", 0))

    toners = printer_data.get("toners") or {}
    if isinstance(toners, dict):
        for color, toner in toners.items():
            if isinstance(toner, dict):
                toner.setdefault("capacity_pages", ypp)
                toner.setdefault("capacity_source", ysrc if color == "black" else f"{ysrc}_global")
                toner.setdefault("yield_per_page", ypp)
    return printer_data


@bp.route('/api/printers')
def api_printers():
    allowed_offices = user_allowed_offices(current_user)
    allowed_ips = allowed_printer_ips(current_user)
    with store.data_lock:
        snap = [dict(item) for item in store.printer_data.values()]
    with store.printers_lock:
        cfg = list(store.PRINTERS)
    cfg_by_ip = {p.get("ip"): p for p in cfg if p.get("ip")}
    # admin دسترسی کامل دارد؛ allowed_offices برای admin نباید با لیست خالی
    # باعث حذف همه پرینترها شود. محدودسازی فقط برای کاربران غیرadmin است.
    if allowed_offices and getattr(current_user, "role", None) != "admin":
        snap = [d for d in snap if d.get("ip") in allowed_ips]
        cfg = [p for p in cfg if p.get("ip") in allowed_ips]
        cfg_by_ip = {p.get("ip"): p for p in cfg if p.get("ip")}
    for data in snap:
        configured = cfg_by_ip.get(data.get("ip")) or {}
        for key in ("device_type", "brand", "group", "community"):
            if configured.get(key) and not data.get(key):
                data[key] = configured[key]
    seen = {d["ip"] for d in snap}
    for p in cfg:
        if p["ip"] not in seen:
            snap.append({
                "ip": p["ip"], "name": p["name"], "nickname": p.get("nickname", ""),
                "device_type": p.get("device_type", "unknown"),
                "brand": p.get("brand", "unknown"),
                "group": p.get("group", ""),
                "community": p.get("community", "public"),
                "online": None, "last_poll": None
            })
    snap = [_attach_yield_metadata(d) for d in snap]
    return jsonify({"printers": snap, "meta": {
        "total": len(cfg),
        "online": sum(1 for d in snap if d.get("online")),
        "offline": sum(1 for d in snap if d.get("online") is False),
        "poll_count": store.poll_stats["count"],
        "last_poll": store.poll_stats["last"],
        "poll_interval": POLL_INTERVAL,
    }})


@bp.route('/api/printer/<path:ip>')
def api_printer(ip):
    if not user_can_access_office(current_user, ip):
        return jsonify({"error": "forbidden"}), 403
    with store.data_lock:
        d = dict(store.printer_data.get(ip) or {}) if store.printer_data.get(ip) else None
    return jsonify(_attach_yield_metadata(d)) if d else (jsonify({"error": "not found"}), 404)


@bp.route('/api/debug/printer/<path:ip>')
def debug_printer(ip):
    if not _debug_endpoints_enabled():
        return _debug_forbidden()
    if not user_can_access_office(current_user, ip):
        return jsonify({"error": "forbidden"}), 403
    with store.data_lock:
        data = store.printer_data.get(ip)
    if data is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@bp.route('/api/debug/brother-toner/<path:ip>')
@admin_required
def debug_brother_toner(ip):
    """Endpoint موقت برای بررسی raw OIDهای تونر Brother."""
    if not _debug_endpoints_enabled():
        return _debug_forbidden()
    if not user_can_access_office(current_user, ip):
        return jsonify({"error": "forbidden"}), 403

    from core.enhanced_collector import (
        detect_snmp_version,
        walk_supplies_table,
        BROTHER_TONER_OID,
        BROTHER_DRUM_OID,
    )
    from core.snmp.protocol import snmp_get_with_fallback

    community = "public"
    printer_name = ip
    with store.printers_lock:
        for printer in store.PRINTERS:
            if printer.get("ip") == ip:
                community = printer.get("community", "public") or "public"
                printer_name = printer.get("name") or ip
                break

    snmp_version = detect_snmp_version(ip, community, timeout=2.0)

    raw_standard = []
    for idx in range(1, 9):
        name_oid = f"1.3.6.1.2.1.43.11.1.1.6.1.{idx}"
        type_oid = f"1.3.6.1.2.1.43.11.1.1.5.1.{idx}"
        max_oid = f"1.3.6.1.2.1.43.11.1.1.8.1.{idx}"
        rem_oid = f"1.3.6.1.2.1.43.11.1.1.9.1.{idx}"
        name_val = snmp_get_with_fallback(ip, name_oid, community, version=snmp_version, timeout=2.0)
        type_val = snmp_get_with_fallback(ip, type_oid, community, version=snmp_version, timeout=2.0)
        max_val = snmp_get_with_fallback(ip, max_oid, community, version=snmp_version, timeout=2.0)
        rem_val = snmp_get_with_fallback(ip, rem_oid, community, version=snmp_version, timeout=2.0)
        if all(v is None for v in (name_val, type_val, max_val, rem_val)):
            continue
        raw_standard.append({
            "index": idx,
            "name_oid": name_oid,
            "name": name_val,
            "type_oid": type_oid,
            "type": type_val,
            "max_oid": max_oid,
            "max": max_val,
            "remaining_oid": rem_oid,
            "remaining": rem_val,
        })

    interpreted = walk_supplies_table(
        ip,
        community,
        brand="brother",
        snmp_version=snmp_version,
        timeout=2.0,
    )

    with store.data_lock:
        current_data = store.printer_data.get(ip, {}) or {}

    return jsonify({
        "ip": ip,
        "name": printer_name,
        "community": community,
        "snmp_version": snmp_version,
        "raw_oids": {
            "brother_toner_oid": BROTHER_TONER_OID,
            "brother_toner_value": snmp_get_with_fallback(ip, BROTHER_TONER_OID, community, version=snmp_version, timeout=2.0),
            "brother_drum_oid": BROTHER_DRUM_OID,
            "brother_drum_value": snmp_get_with_fallback(ip, BROTHER_DRUM_OID, community, version=snmp_version, timeout=2.0),
            "standard_supplies": raw_standard,
        },
        "interpreted_supplies": interpreted,
        "current_dashboard_toners": (current_data.get("toners") or {}),
    })


@bp.route('/api/debug/toshiba-snmp/<path:ip>')
@admin_required
def debug_toshiba_snmp(ip):
    """Endpoint موقت برای بررسی raw probeهای SNMP و OIDهای Toshiba."""
    if not _debug_endpoints_enabled():
        return _debug_forbidden()
    if not user_can_access_office(current_user, ip):
        return jsonify({"error": "forbidden"}), 403

    from core.snmp.protocol import snmp_debug_get, _detect_snmp_version
    from core.snmp.oid_map import OIDS as TOSHIBA_OIDS

    community = "public"
    printer_name = ip
    with store.printers_lock:
        for printer in store.PRINTERS:
            if printer.get("ip") == ip:
                community = printer.get("community", "public") or "public"
                printer_name = printer.get("name") or ip
                break

    detected_version = _detect_snmp_version(ip, community, probe_timeout=2.0)

    health_oids = [
        ("sysDescr", "1.3.6.1.2.1.1.1.0"),
        ("sysUpTime", "1.3.6.1.2.1.1.3.0"),
        ("sysName", "1.3.6.1.2.1.1.5.0"),
        ("printerModel", "1.3.6.1.2.1.43.5.1.1.16.1"),
        ("toshibaModel", TOSHIBA_OIDS.get("model")),
        ("toshibaUptime", TOSHIBA_OIDS.get("uptime")),
    ]
    vendor_oids = [
        ("print_total", TOSHIBA_OIDS.get("print_total")),
        ("print_fc", TOSHIBA_OIDS.get("print_fc")),
        ("print_bw", TOSHIBA_OIDS.get("print_bw")),
        ("print_copy_fc", TOSHIBA_OIDS.get("print_copy_fc")),
        ("print_copy_bw", TOSHIBA_OIDS.get("print_copy_bw")),
        ("print_printer_fc", TOSHIBA_OIDS.get("print_printer_fc")),
        ("print_printer_bw", TOSHIBA_OIDS.get("print_printer_bw")),
        ("print_fax", TOSHIBA_OIDS.get("print_fax")),
        ("print_list", TOSHIBA_OIDS.get("print_list")),
        ("a3_total", TOSHIBA_OIDS.get("a3_total")),
        ("a4_total", TOSHIBA_OIDS.get("a4_total")),
    ]

    diagnostics = {"health": [], "vendor": []}
    request_id = 1
    for label, oid in health_oids + vendor_oids:
        if not oid:
            continue
        bucket = "health" if (label, oid) in health_oids else "vendor"
        row = {
            "label": label,
            "oid": oid,
            "v1": snmp_debug_get(ip, oid, community, timeout=2.0, request_id=request_id, version=1),
            "v2c": snmp_debug_get(ip, oid, community, timeout=2.0, request_id=request_id + 100, version=2),
        }
        diagnostics[bucket].append(row)
        request_id += 1

    with store.data_lock:
        current_data = store.printer_data.get(ip, {}) or {}

    return jsonify({
        "ip": ip,
        "name": printer_name,
        "community": community,
        "detected_snmp_version": detected_version,
        "diagnostics": diagnostics,
        "current_dashboard_data": current_data,
    })


@bp.route('/api/printers/add', methods=['POST'])
def api_add():
    body = request.get_json() or {}
    ip = (body.get("ip") or "").strip()
    name = (body.get("name") or f"Printer {ip}").strip()
    community = (body.get("community") or "public").strip()
    nickname = (body.get("nickname") or "").strip()
    group = (body.get("group") or "").strip()
    if not ip:
        return jsonify({"error": "IP required"}), 400
    if not _is_valid_ip(ip):
        return jsonify({"error": "IP نامعتبر است"}), 400
    with store.printers_lock:
        if any(p["ip"] == ip for p in store.PRINTERS):
            return jsonify({"error": "already exists"}), 409
        new_p = {"ip": ip, "name": name, "community": community, "nickname": nickname, "group": group}
        store.PRINTERS.append(new_p)
        store.save_printers(store.PRINTERS)
    threading.Thread(
        target=lambda: poll_one(new_p),
        daemon=True
    ).start()
    return jsonify({"status": "added", "ip": ip, "name": name})


@bp.route('/api/printers/bulk-add', methods=['POST'])
def api_bulk_add():
    body = request.get_json() or {}
    items = body.get("printers", [])
    do_scan = body.get("scan", True)
    skip_exist = body.get("skip_existing", True)

    if not items or not isinstance(items, list):
        return jsonify({"error": "آرایه printers الزامی است"}), 400
    if len(items) > 50:
        return jsonify({"error": "حداکثر ۵۰ پرینتر در هر درخواست"}), 400

    from core.store import _normalize_printer
    added, skipped, failed = [], [], []

    for raw in items:
        if not isinstance(raw, dict):
            failed.append({"item": raw, "reason": "فرمت نامعتبر"})
            continue
        p = _normalize_printer(raw)
        ip = p["ip"]
        if not ip:
            failed.append({"item": raw, "reason": "IP خالی است"})
            continue
        if not _is_valid_ip(ip):
            failed.append({"item": raw, "reason": "IP نامعتبر است"})
            continue
        with store.printers_lock:
            exists = any(x["ip"] == ip for x in store.PRINTERS)
        if exists:
            (skipped if skip_exist else failed).append({"ip": ip, "reason": "قبلاً وجود دارد"})
            continue
        with store.printers_lock:
            store.PRINTERS.append(p)
            store.save_printers(store.PRINTERS)
        added.append(p)

    if added:
        def _bg_init(new_printers):
            for p in new_printers:
                try:
                    if do_scan:
                        profile = scan_printer_oids(p["ip"], p.get("community", "public"))
                        if not p.get("brand") and profile and profile.get("brand", "unknown") != "unknown":
                            p["brand"] = profile["brand"]
                            with store.printers_lock:
                                for i, x in enumerate(store.PRINTERS):
                                    if x["ip"] == p["ip"]:
                                        store.PRINTERS[i] = _normalize_printer(p)
                                store.save_printers(store.PRINTERS)
                            # بعد از به‌روزرسانی برند در PRINTERS، بلافاصله poll_one بگیر تا printer_data بروز شود
                            poll_one(p)
                        else:
                            # اگر برند قبلاً مشخص بود یا اسکن نشد، باز هم یک poll اولیه انجام بده
                            result = collect(p)
                            with store.data_lock:
                                store.printer_data[p["ip"]] = result
                    else:
                        result = collect(p)
                        with store.data_lock:
                            store.printer_data[p["ip"]] = result
                except Exception as e:
                    log.exception("bulk-add init %s failed", p['ip'])
        threading.Thread(target=_bg_init, args=(list(added),), daemon=True).start()

    return jsonify({
        "total_added": len(added),
        "added": added,
        "skipped": skipped,
        "failed": failed,
    }), (200 if added or skipped else 400)


@bp.route('/api/printers/remove', methods=['POST'])
def api_remove():
    ip = (request.get_json() or {}).get("ip", "").strip()
    with store.printers_lock:
        before = len(store.PRINTERS)
        store.PRINTERS[:] = [p for p in store.PRINTERS if p["ip"] != ip]
        if len(store.PRINTERS) == before:
            return jsonify({"error": "not found"}), 404
        store.save_printers(store.PRINTERS)
    with store.data_lock:
        store.printer_data.pop(ip, None)
    # حذف baseline قبلی تا اگر همین IP بعداً به دستگاه دیگری اختصاص یافت، لاگ دروغین ساخته نشود.
    store._prev.delete(ip)
    return jsonify({"status": "removed", "ip": ip})


@bp.route('/api/discover/auto-add', methods=['POST'])
def api_auto_add_printer():
    data = request.json or {}
    ip = data.get("ip", "").strip()
    community = data.get("community", "public")
    custom_name = data.get("name", "").strip()
    if not ip:
        return jsonify({"error": "ip الزامی است"}), 400
    if not _is_valid_ip(ip):
        return jsonify({"error": "IP نامعتبر است"}), 400
    with store.printers_lock:
        if any(p["ip"] == ip for p in store.PRINTERS):
            return jsonify({"error": f"{ip} قبلاً اضافه شده"}), 409
    try:
        profile = scan_printer_oids(ip, community, force=False)
        if not profile:
            return jsonify({"error": f"پرینتر {ip} پاسخ نداد"}), 404
        s = profile["summary"]
        name = custom_name or f"{s['brand'].upper()} {s['model']}"
        new_printer = {
            "ip": ip,
            "name": name,
            "community": community,
            "brand": s["brand"],
            "device_type": s.get("device_type", "sensor" if s.get("brand") == "sensor" else "unknown"),
            "nickname": "",
        }
        with store.printers_lock:
            store.PRINTERS.append(new_printer)
            store.save_printers(store.PRINTERS)

        def _poll_new():
            result = collect(new_printer)
            with store.data_lock:
                store.printer_data[new_printer["ip"]] = result
        threading.Thread(target=_poll_new, daemon=True).start()
        return jsonify({"status": "added", "printer": new_printer, "summary": s})
    except Exception:
        log.exception("auto-add printer %s failed", ip)
        return jsonify({"error": "خطای داخلی سرور رخ داد"}), 500


@bp.route('/api/printer/<path:ip>/update', methods=['POST'])
def api_update_printer(ip):
    if not user_can_access_office(current_user, ip):
        return jsonify({"error": "forbidden"}), 403
        
    data = request.get_json() or {}
    new_name = data.get("name", "").strip()
    new_nickname = data.get("nickname", "").strip()
    new_group = data.get("group", "").strip()
    
    with store.printers_lock:
        found = False
        for p in store.PRINTERS:
            if p["ip"] == ip:
                if new_name:
                    p["name"] = new_name
                p["nickname"] = new_nickname
                p["group"] = new_group
                found = True
                break
        
        if not found:
            return jsonify({"error": "printer not found"}), 404
            
        store.save_printers(store.PRINTERS)
        
    # به‌روزرسانی داده‌های لحظه‌ای در حافظه
    with store.data_lock:
        if ip in store.printer_data:
            if new_name:
                store.printer_data[ip]["name"] = new_name
            store.printer_data[ip]["nickname"] = new_nickname
            store.printer_data[ip]["group"] = new_group
            
    return jsonify({"status": "ok", "ip": ip})


@bp.route('/api/printer/<path:ip>/toner_reset', methods=['POST'])
def toner_reset(ip):
    try:
        if not user_can_access_office(current_user, ip):
            return jsonify({"error": "forbidden"}), 403

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "invalid JSON body"}), 400

        color = (data.get('color') or '').strip().lower()
        if not color:
            return jsonify({"error": "color required"}), 400

        if color not in ('black', 'cyan', 'magenta', 'yellow'):
            return jsonify({"error": "invalid color"}), 400

        try:
            new_level = int(data.get('new_level', 100))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid new_level"}), 400

        if new_level < 0 or new_level > 100:
            return jsonify({"error": "new_level must be between 0 and 100"}), 400

        warning_threshold = TONER_ALERT_THRESHOLDS.get('warning', 15)
        critical_threshold = TONER_ALERT_THRESHOLDS.get('critical', 5)
        toner_alert_codes = set()

        with store.data_lock:
            printer = store.printer_data.get(ip)
            if printer is None:
                return jsonify({"error": "printer not found"}), 404
            toners = printer.get('toners')
            if not isinstance(toners, dict) or color not in toners:
                return jsonify({"error": "color not available"}), 400
            toners[color]['level'] = new_level
            if new_level == 0:
                status = 'empty'
            elif new_level <= critical_threshold:
                status = 'critical'
            elif new_level <= warning_threshold:
                status = 'low'
            else:
                status = 'ok'
            toners[color]['status'] = status

            if isinstance(printer.get('counters'), dict):
                printer['counters']['pages_since_last_reset'] = 0

            if new_level > warning_threshold:
                # هشدارهای مرتبط با تونر را بلافاصله از وضعیت فعال حذف کن.
                toner_alert_codes = {
                    ((toner_data.get('index') if isinstance(toner_data, dict) else None) or toner_color)
                    for toner_color, toner_data in toners.items()
                }
                toner_alert_code_strs = {str(code) for code in toner_alert_codes}
                printer['alerts'] = [
                    alert for alert in (printer.get('alerts') or [])
                    if str(alert.get('code')) not in toner_alert_code_strs
                ]

        prev = store._prev.get(ip) or {}
        counters = printer.get('counters', {}) if isinstance(printer.get('counters'), dict) else {}
        current_total = counters.get('total', 0)
        current_full_color = counters.get('full_color')
        current_black_white = counters.get('black_white')

        learned_yield = prev.get('yield_per_page', 2000)
        if prev.get('force_estimate') and prev.get('override_start_total') is not None:
            try:
                pages_since_last_override = int(current_total) - int(prev.get('override_start_total'))
                consumed_pct = int(prev.get('override_start_toner', 100) or 100) - int(prev.get('toner_level') or 0)
                if pages_since_last_override > 0 and consumed_pct > 0:
                    estimated_yield = int(round(pages_since_last_override * 100.0 / consumed_pct))
                    if 300 <= estimated_yield <= 20000:
                        current_yield = int(prev.get('yield_per_page', 2000) or 2000)
                        if current_yield == 2000:
                            learned_yield = estimated_yield
                        else:
                            learned_yield = int(round((current_yield * 0.7) + (estimated_yield * 0.3)))
                        log.info(
                            "manual toner reset yield update for %s: %s -> %s (pages=%s, consumed_pct=%s)",
                            ip, current_yield, learned_yield, pages_since_last_override, consumed_pct,
                        )
            except Exception as exc:
                log.exception("manual toner reset yield learning failed for %s: %s", ip, exc)

        toner_alert_code_strs = {str(code) for code in toner_alert_codes}
        if new_level > warning_threshold:
            cleared_alert_codes = [
                code for code in (prev.get('alert_codes', []) or [])
                if str(code) not in toner_alert_code_strs
            ]
        else:
            cleared_alert_codes = prev.get('alert_codes', [])
        new_prev = {
            # reset دستی باید baseline شمارنده را با snapshot فعلی sync کند؛
            # وگرنه poll بعدی ممکن است PRINT یا COUNTER_RESET اشتباه بسازد.
            'print_total': current_total,
            'full_color': current_full_color if current_full_color is not None else prev.get('full_color'),
            'black_white': current_black_white if current_black_white is not None else prev.get('black_white'),
            'toner_level': new_level,
            'manual_override': 1,
            'override_color': color,
            'override_base_level': new_level,
            'override_start_total': current_total,
            'override_start_toner': new_level,
            'yield_per_page': learned_yield,
            'force_estimate': prev.get('force_estimate', 0),
            'yield_learning_failures': prev.get('yield_learning_failures', 0),
            'alert_codes': cleared_alert_codes,
            'last_alert_codes': cleared_alert_codes,
            'uptime': prev.get('uptime'),
        }
        store._prev.set(ip, new_prev)

        # Yield Engine جدید هم باید reset دستی را به‌عنوان anchor معتبر per-cartridge ثبت کند؛
        # وگرنه اگر دستگاه raw toner ندهد، لایه جدید از reset کاربر بی‌خبر می‌ماند.
        try:
            from core.yield_engine import register_manual_refill
            device_info = printer.get('device') if isinstance(printer.get('device'), dict) else {}
            toner_info = toners.get(color) if isinstance(toners, dict) else {}
            register_manual_refill(
                ip=ip,
                color=color,
                printer_model=device_info.get('model') or 'Unknown',
                cartridge_name=(toner_info or {}).get('name') or color,
                new_level=new_level,
                counters={
                    'total': current_total,
                    'full_color': current_full_color,
                    'black_white': current_black_white,
                },
                device_type=printer.get('device_type', 'unknown'),
            )
        except Exception as exc:
            log.exception('yield_engine manual refill registration failed for %s/%s: %s', ip, color, exc)

        username = current_user.username if current_user.is_authenticated else 'سیستم'
        add_event(ip, 'REFILL', {
            'message': f'تنظیم دستی: کارتریج {color} به {new_level}% تنظیم شد',
            'severity': 'info',
            'username': username,
            'manual_reset': True,
            'auto_detected': False,
            'reset_color': color,
            'set_level': new_level,
            'total_at_reset': current_total,
            'yield_per_page_at_reset': learned_yield,
        })

        return jsonify({'status': 'ok'})
    except Exception as e:
        log.exception('toner_reset failed for %s', ip)
        return jsonify({"error": "internal server error"}), 500


@bp.route('/api/printer/<path:ip>/cartridge_name_override', methods=['POST'])
def cartridge_name_override(ip):
    """تنظیم نام دستی کارتریج برای پرینترهایی که SNMP نام اشتباه برمیگردانند.
    مثلاً Canon MF220 همیشه 'Cartridge 137' برمیگرداند حتی با کارتریج 737.
    """
    try:
        if not user_can_access_office(current_user, ip):
            return jsonify({"error": "forbidden"}), 403

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "invalid JSON body"}), 400

        color = (data.get('color') or '').strip().lower()
        if not color:
            return jsonify({"error": "color required"}), 400

        override_name = (data.get('name') or '').strip()

        from core.yield_engine import set_cartridge_name_override
        result = set_cartridge_name_override(ip, color, override_name)
        if 'error' in result:
            return jsonify(result), 400

        return jsonify({'status': 'ok', 'cartridge_name': result.get('last_cartridge_name', override_name)})
    except Exception as e:
        log.exception('cartridge_name_override failed for %s', ip)
        return jsonify({"error": "internal server error"}), 500