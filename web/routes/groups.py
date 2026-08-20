import logging
from flask import Blueprint, jsonify, request

from core import store
from core.groups import load_groups, create_group, rename_group, delete_group
from flask_login import current_user
from config import settings
import os
import json

log = logging.getLogger("PrinterMonitor")
bp = Blueprint("groups", __name__)


def _clear_group_from_printers(group_id: str) -> int:
    """گروه را از همه پرینترهایی که آن id را دارند پاک می‌کند."""
    changed = 0
    with store.printers_lock:
        for p in store.PRINTERS:
            if str(p.get("group", "")).strip() == group_id:
                p["group"] = ""
                changed += 1
        if changed:
            store.save_printers(store.PRINTERS)
    return changed


@bp.route("/api/groups", methods=["GET"])
def api_groups():
    """لیست گروه‌های سفارشی (گروه‌های پیش‌فرض دفاتر frontend-side هستند)."""
    return jsonify({"groups": load_groups()})


@bp.route("/api/groups", methods=["POST"])
def api_create_group():
    body = request.get_json(silent=True) or {}
    try:
        group = create_group(
            name=body.get("name"),
            icon=body.get("icon"),
            color=body.get("color"),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": "created", "group": group}), 201


@bp.route("/api/groups/<path:gid>/rename", methods=["POST"])
def api_rename_group(gid):
    body = request.get_json(silent=True) or {}
    try:
        ok = rename_group(gid, body.get("name"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not ok:
        return jsonify({"error": "گروه پیدا نشد"}), 404
    return jsonify({"status": "renamed", "id": gid, "name": body.get("name")})


@bp.route("/api/groups/<path:gid>", methods=["DELETE"])
def api_delete_group(gid):
    if not delete_group(gid):
        return jsonify({"error": "گروه پیدا نشد (گروه‌های پیش‌فرض دفاتر قابل حذف نیستند)"}), 404
    cleared = _clear_group_from_printers(gid)
    return jsonify({"status": "deleted", "id": gid, "printers_ungrouped": cleared})


@bp.route('/api/admin/office_subnets', methods=['GET'])
def api_get_office_subnets():
    # only admin
    if not getattr(current_user, 'is_authenticated', False) or getattr(current_user, 'role', '') != 'admin':
        return jsonify({'error': 'forbidden'}), 403
    return jsonify({'subnets': settings.OFFICE_SUBNETS})


@bp.route('/api/admin/office_subnets', methods=['POST'])
def api_save_office_subnets():
    if not getattr(current_user, 'is_authenticated', False) or getattr(current_user, 'role', '') != 'admin':
        return jsonify({'error': 'forbidden'}), 403
    body = request.get_json(silent=True) or {}
    subnets = body.get('subnets') or {}
    if not isinstance(subnets, dict):
        return jsonify({'error': 'invalid payload'}), 400

    # fully dynamic keys: accept provided ids and values (including null)
    out = {}
    for k, v in subnets.items():
        key = str(k or '').strip()
        if not key:
            continue
        val = v.strip() if isinstance(v, str) else v
        out[key] = val if val else None

    # write to file next to settings.py
    try:
        settings_dir = os.path.dirname(os.path.abspath(settings.__file__))
        target = os.path.join(settings_dir, 'office_subnets.json')
        with open(target, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        # Apply immediately in-memory so runtime picks up new subnets without restart
        try:
            settings.OFFICE_SUBNETS = out
        except Exception:
            log.exception('Failed to update settings.OFFICE_SUBNETS in-memory')
    except Exception as e:
        log.exception('Failed to save office_subnets.json')
        return jsonify({'error': 'failed to save'}), 500

    return jsonify({'status': 'saved', 'subnets': out})


@bp.route('/api/admin/office_subnets/<path:gid>', methods=['DELETE'])
def api_delete_office_group(gid):
    if not getattr(current_user, 'is_authenticated', False) or getattr(current_user, 'role', '') != 'admin':
        return jsonify({'error': 'forbidden'}), 403

    gid = (gid or '').strip()
    if not gid:
        return jsonify({'error': 'invalid group id'}), 400

    current = dict(getattr(settings, 'OFFICE_SUBNETS', {}) or {})
    if gid not in current:
        return jsonify({'error': 'group not found'}), 404

    current.pop(gid, None)

    try:
        settings_dir = os.path.dirname(os.path.abspath(settings.__file__))
        target = os.path.join(settings_dir, 'office_subnets.json')
        with open(target, 'w', encoding='utf-8') as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        settings.OFFICE_SUBNETS = current
    except Exception:
        log.exception('Failed to delete office group from office_subnets.json')
        return jsonify({'error': 'failed to save'}), 500

    cleared = _clear_group_from_printers(gid)
    return jsonify({'status': 'deleted', 'id': gid, 'subnets': current, 'printers_ungrouped': cleared})
