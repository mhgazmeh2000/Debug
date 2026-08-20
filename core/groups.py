"""
مدیریت گروه‌های سفارشی پرینترها (groups.json).

مدل داده:
    [
      {"id": "g_management", "name": "مدیریت", "icon": "🏢", "color": "cyan",
       "created_at": "...", "updated_at": "..."}
    ]

نکته مهم: گروه‌های پیش‌فرض دفاتر (imamat/soroush/...) که بر اساس subnet هستند،
در frontend هاردکد شده‌اند و اینجا نیستند. این ماژول فقط گروه‌های «سفارشی»
کاربر را نگه می‌دارد تا:
  ۱) گروه حتی بدون هیچ پرینتری هم وجود داشته باشد،
  ۲) تغییر نام/حذف گروه ممکن شود،
  ۳) id گروه (کلید پایدار برای ذخیره روی پرینتر) با نام نمایشی جدا باشد.
"""

import json
import os
import re
import threading
import logging
from datetime import datetime

log = logging.getLogger("PrinterMonitor")

GROUPS_FILE = "groups.json"
_groups_lock = threading.Lock()

_GROUP_ICONS = ["🏢", "🏦", "🏛️", "🏥", "🏫", "🏭", "🏬", "🏗️", "💼", "📦", "🖨️", "🏪"]
_GROUP_COLORS = ["cyan", "green", "yellow", "magenta", "orange", "red", "blue"]

_slug_re = re.compile(r"[^a-z0-9_]+")


def _slugify(name: str) -> str:
    """ساخت id پایدار از نام گروه. برای نام‌های فارسی، کلید زمانی یکتا می‌سازیم."""
    base = (name or "").strip().lower()
    base = _slug_re.sub("", base.replace(" ", "_").replace("-", "_")).strip("_")
    if base and base[0].isdigit():
        base = "g_" + base
    if not base:
        # نام تماماً فارسی/غیرلاتین است → کلید یکتا از زمان بساز
        base = datetime.now().strftime("%H%M%S%f")[:12]
    return base if base.startswith("g_") else "g_" + base


def load_groups() -> list:
    try:
        if os.path.exists(GROUPS_FILE):
            with open(GROUPS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [g for g in data if isinstance(g, dict) and g.get("id") and g.get("name")]
    except Exception:
        log.exception("خطا در خواندن groups.json")
    return []


def save_groups(groups: list) -> list:
    with _groups_lock:
        clean = []
        seen = set()
        for g in groups:
            gid = str(g.get("id", "")).strip()
            name = str(g.get("name", "")).strip()
            if not gid or not name or gid in seen:
                continue
            seen.add(gid)
            clean.append({
                "id": gid,
                "name": name,
                
                "created_at": g.get("created_at"),
                "updated_at": g.get("updated_at"),
            })
        with open(GROUPS_FILE, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
    return clean


def create_group(name: str, icon: str = None, color: str = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("نام گروه الزامی است")
    if len(name) > 60:
        raise ValueError("نام گروه نباید بیش از ۶۰ کاراکتر باشد")

    groups = load_groups()
    if any(g["name"].strip().lower() == name.lower() for g in groups):
        raise ValueError("گروهی با این نام قبلاً وجود دارد")

    gid = _slugify(name)
    existing = {g["id"] for g in groups}
    suffix = 2
    candidate = gid
    while candidate in existing:
        candidate = f"{gid}_{suffix}"
        suffix += 1

    now = datetime.now().isoformat()
    group = {
        "id": candidate,
        "name": name,

        "created_at": now,
        "updated_at": now,
    }
    groups.append(group)
    save_groups(groups)
    log.info("گروه جدید ساخته شد: %s (%s)", name, candidate)
    return group


def rename_group(group_id: str, new_name: str) -> bool:
    new_name = (new_name or "").strip()
    if not new_name or len(new_name) > 60:
        raise ValueError("نام گروه نامعتبر است")
    groups = load_groups()
    if any(g["name"].strip().lower() == new_name.lower() and g["id"] != group_id for g in groups):
        raise ValueError("گروهی با این نام قبلاً وجود دارد")
    for g in groups:
        if g["id"] == group_id:
            g["name"] = new_name
            g["updated_at"] = datetime.now().isoformat()
            save_groups(groups)
            return True
    return False


def delete_group(group_id: str) -> bool:
    """حذف گروه؛ تخصیص گروه از پرینترها باید توسط caller پاک شود."""
    groups = load_groups()
    remaining = [g for g in groups if g["id"] != group_id]
    if len(remaining) == len(groups):
        return False
    save_groups(remaining)
    log.info("گروه %s حذف شد", group_id)
    return True


