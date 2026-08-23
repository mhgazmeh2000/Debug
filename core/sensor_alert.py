"""ارسال ایمیل هشدار سنسور — تنظیمات سراسری."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime

log = logging.getLogger("PrinterMonitor")

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "sensor_alert_config.json",
)

_last_sent: dict[str, float] = {}


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("Could not load sensor alert config: %s", e)
        return {"enabled": False}


def save_config(config: dict):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.exception("Error saving sensor alert config: %s", e)


def _in_time_range(start_hour: int, end_hour: int) -> bool:
    now = datetime.now().hour
    if start_hour <= end_hour:
        return start_hour <= now < end_hour
    else:
        return now >= start_hour or now < end_hour


def _check_cooldown(key: str, cooldown_minutes: int) -> bool:
    last = _last_sent.get(key, 0)
    return (time.time() - last) >= cooldown_minutes * 60


def _mark_sent(key: str):
    _last_sent[key] = time.time()


NL = "\n"


def check_and_send_sensor_alert(ip: str, sensor_name: str, readings: list):
    from utils import send_email

    config = load_config()
    if not config.get("enabled"):
        return

    emails = list(config.get("recipient_emails") or [])
    user_ids = config.get("user_ids") or []
    if user_ids:
        from models import User
        for uid in user_ids:
            try:
                u = User.get(int(uid))
                if u and u.email and u.email not in emails:
                    emails.append(u.email)
            except Exception:
                pass
    if not emails:
        return

    temp_warning = config.get("temp_warning", 30)
    temp_critical = config.get("temp_critical", 35)
    hum_warning_high = config.get("hum_warning_high", 70)
    hum_critical_high = config.get("hum_critical_high", 80)
    hum_warning_low = config.get("hum_warning_low", 20)
    cooldown = config.get("cooldown_minutes", 30)
    start_hour = config.get("start_hour", 0)
    end_hour = config.get("end_hour", 24)

    if not _in_time_range(start_hour, end_hour):
        return

    triggered = []
    for r in readings:
        if r.get("status") != "active" or r.get("value") is None:
            continue
        kind = r.get("kind")
        value = float(r["value"])
        port = r.get("port", 1)
        unit = r.get("unit", "\u00b0C" if kind == "temperature" else "%")
        if kind == "temperature":
            if value >= temp_critical:
                triggered.append(("critical", "\u062f\u0645\u0627\u06cc \u0628\u062d\u0631\u0627\u0646\u06cc", value, unit, port))
            elif value >= temp_warning:
                triggered.append(("warning", "\u062f\u0645\u0627\u06cc \u0628\u0627\u0644\u0627", value, unit, port))
        elif kind == "humidity":
            if value >= hum_critical_high:
                triggered.append(("critical", "\u0631\u0637\u0648\u0628\u062a \u0628\u062d\u0631\u0627\u0646\u06cc \u0628\u0627\u0644\u0627", value, unit, port))
            elif value >= hum_warning_high:
                triggered.append(("warning", "\u0631\u0637\u0648\u0628\u062a \u0628\u0627\u0644\u0627", value, unit, port))
            elif value <= hum_warning_low:
                triggered.append(("warning", "\u0631\u0637\u0648\u0628\u062a \u067e\u0627\u06cc\u06cc\u0646", value, unit, port))

    if not triggered:
        return

    cooldown_key = "sensor_%s" % ip
    if not _check_cooldown(cooldown_key, cooldown):
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    severity = "critical" if any(t[0] == "critical" for t in triggered) else "warning"

    fa_parts = []
    for sev, label, value, unit, port in triggered:
        fa_parts.append("%s: %s%s (port %s)" % (label, value, unit, port))
    alerts_text = NL.join("  " + a for a in fa_parts)

    subject = "\U0001f6a8 \u0647\u0634\u062f\u0627\u0631 \u0633\u0646\u0633\u0648\u200c %s (%s)" % (sensor_name, ip)

    text_body = NL.join([
        "\u0647\u0634\u062f\u0627\u0631 \u0633\u0646\u0633\u0648\u0631",
        "",
        "\u062f\u0633\u062a\u06af\u0627\u0647: %s (%s)" % (sensor_name, ip),
        "\u0632\u0645\u0627\u0646: %s" % now_str,
        "",
        "\u0647\u0634\u062f\u0627\u0631\u0647\u0627:",
        alerts_text,
        "",
        "\u0627\u06cc\u0646 \u0627\u06cc\u0645\u06cc\u0644 \u0628\u0647 \u0635\u0648\u0631\u062a \u062e\u0648\u062f\u06a9\u0627\u0631 \u0627\u0631\u0633\u0627\u0644 \u0634\u062f\u0647 \u0627\u0633\u062a.",
    ])

    icon_color = "#dc2626" if severity == "critical" else "#f59e0b"

    alert_rows = ""
    for sev, label, value, unit, port in triggered:
        color = "#dc2626" if sev == "critical" else "#f59e0b"
        row = '<tr><td style="padding:8px;border:1px solid #ddd;color:%s;">%s: %s%s (port %s)</td></tr>' + NL
        alert_rows += row % (color, label, value, unit, port)

    html_parts = [
        '<div style="font-family:Arial,sans-serif;direction:rtl;text-align:right;max-width:600px;margin:0 auto">',
        '<h2 style="color:%s">\U0001f6a8 \u0647\u0634\u062f\u0627\u0631 \u0633\u0646\u0633\u0648\u0631</h2>' % icon_color,
        '<table style="width:100%;border-collapse:collapse;margin:16px 0">',
        '<tr style="background:#f8f9fa"><td style="padding:8px;border:1px solid #ddd"><strong>\u062f\u0633\u062a\u06af\u0627\u0647</strong></td><td style="padding:8px;border:1px solid #ddd">%s (%s)</td></tr>' % (sensor_name, ip),
        '<tr><td style="padding:8px;border:1px solid #ddd"><strong>\u0632\u0645\u0627\u0646</strong></td><td style="padding:8px;border:1px solid #ddd">%s</td></tr>' % now_str,
        '</table>',
        '<h3 style="color:#333">\u0647\u0634\u062f\u0627\u0631\u0647\u0627:</h3>',
        '<table style="width:100%;border-collapse:collapse">%s</table>' % alert_rows,
        '<hr style="margin:20px 0;border-color:#eee">',
        '<p style="color:#999;font-size:11px">\u0627\u06cc\u0646 \u0627\u06cc\u0645\u06cc\u0644 \u0628\u0647 \u0635\u0648\u0631\u062a \u062e\u0648\u062f\u06a9\u0627\u0631 \u0627\u0631\u0633\u0627\u0644 \u0634\u062f\u0647 \u0627\u0633\u062a.</p>',
        '</div>',
    ]
    html_body = NL.join(html_parts)

    try:
        for email in emails:
            email_sent = send_email(subject, email, text_body, html_body)
            if email_sent:
                log.info("Sensor alert email sent to %s: %s (%s)", email, sensor_name, ip)
            else:
                log.warning("Failed to send sensor alert email to %s", email)
        _mark_sent(cooldown_key)
    except Exception as e:
        log.exception("Error sending sensor alert emails: %s", e)
