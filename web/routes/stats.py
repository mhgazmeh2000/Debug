import sqlite3
import logging
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request
from config.settings import DB_PATH
from core import store

bp = Blueprint("stats", __name__)
log = logging.getLogger("PrinterMonitor")


@bp.route('/api/stats/daily')
def api_daily_stats():
    ip = request.args.get('ip')
    days = min(request.args.get('days', default=30, type=int), 365)
    start_date = (datetime.now() - timedelta(days=days)).date().isoformat()

    try:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        c = conn.cursor()

        # استفاده از substr به جای strftime برای اطمینان از کار با timestamp TEXT با فرمت ISO
        if ip:
            c.execute('''
                SELECT substr(timestamp, 1, 10) as day, COALESCE(SUM(pages), 0) as total
                FROM logs
                WHERE printer_ip = ? AND type = 'PRINT' AND timestamp >= ?
                  AND pages IS NOT NULL AND pages > 0
                GROUP BY day ORDER BY day
            ''', (ip, start_date))
        else:
            c.execute('''
                SELECT substr(timestamp, 1, 10) as day, COALESCE(SUM(pages), 0) as total
                FROM logs
                WHERE type = 'PRINT' AND timestamp >= ?
                  AND pages IS NOT NULL AND pages > 0
                GROUP BY day ORDER BY day
            ''', (start_date,))

        rows = c.fetchall()
        conn.close()

        date_dict = {row[0]: row[1] for row in rows}
        start_dt = datetime.now().date() - timedelta(days=days)
        all_dates = []
        all_totals = []
        for i in range(days + 1):
            d = (start_dt + timedelta(days=i)).isoformat()
            all_dates.append(d)
            all_totals.append(date_dict.get(d, 0))

        return jsonify({
            "dates": all_dates,
            "totals": all_totals,
            "printer_ip": ip,
            "days": days
        })
    except Exception as e:
        log.error(f"Stats API error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@bp.route('/api/stats/sensor/daily')
def api_sensor_daily_stats():
    """میانگین روزانه دما و رطوبت سنسور از جدول sensor_readings."""
    ip = request.args.get('ip')
    days = min(request.args.get('days', default=30, type=int), 365)
    start_date = (datetime.now() - timedelta(days=days)).date().isoformat()
    if not ip:
        return jsonify({"error": "ip required"}), 400

    try:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        c = conn.cursor()
        c.execute('''
            SELECT substr(timestamp, 1, 10) as day, kind, AVG(value) as avg_value
            FROM sensor_readings
            WHERE printer_ip = ? AND timestamp >= ? AND value IS NOT NULL
              AND kind IN ('temperature', 'humidity')
            GROUP BY day, kind
            ORDER BY day
        ''', (ip, start_date))
        rows = c.fetchall()
        conn.close()

        temp_by_day = {}
        hum_by_day = {}
        for day, kind, avg_value in rows:
            if kind == 'temperature':
                temp_by_day[day] = round(float(avg_value), 1)
            elif kind == 'humidity':
                hum_by_day[day] = round(float(avg_value), 1)

        # اگر هنوز جدول sensor_readings داده کافی ندارد، از snapshot زنده سنسور برای امروز fallback بگیر.
        # این باعث می‌شود نمودار بلافاصله بعد از اولین poll هم چیزی برای نمایش داشته باشد.
        today = datetime.now().date().isoformat()
        if today not in temp_by_day or today not in hum_by_day:
            with store.data_lock:
                live = dict(store.printer_data.get(ip) or {})
            live_readings = live.get('sensor_readings') or []
            temps = [float(r.get('value')) for r in live_readings if r.get('kind') == 'temperature' and r.get('value') is not None]
            hums = [float(r.get('value')) for r in live_readings if r.get('kind') == 'humidity' and r.get('value') is not None]
            if temps and today not in temp_by_day:
                temp_by_day[today] = round(sum(temps) / len(temps), 1)
            if hums and today not in hum_by_day:
                hum_by_day[today] = round(sum(hums) / len(hums), 1)

        start_dt = datetime.now().date() - timedelta(days=days)
        dates = []
        avg_temp = []
        avg_humidity = []
        for i in range(days + 1):
            d = (start_dt + timedelta(days=i)).isoformat()
            dates.append(d)
            avg_temp.append(temp_by_day.get(d))
            avg_humidity.append(hum_by_day.get(d))

        return jsonify({
            "dates": dates,
            "avg_temperature": avg_temp,
            "avg_humidity": avg_humidity,
            "printer_ip": ip,
            "days": days,
        })
    except sqlite3.OperationalError as e:
        # اگر DB قدیمی هنوز init نشده باشد.
        log.error(f"Sensor stats API DB error: {e}", exc_info=True)
        return jsonify({"dates": [], "avg_temperature": [], "avg_humidity": [], "printer_ip": ip, "days": days})
    except sqlite3.DatabaseError as e:
        # خطاهای ساختاری SQLite نباید نمودار کل داشبورد را با HTTP 500 متوقف کنند.
        log.error(f"Sensor stats API database error: {e}", exc_info=True)
        return jsonify({"dates": [], "avg_temperature": [], "avg_humidity": [], "printer_ip": ip, "days": days})
    except Exception as e:
        log.error(f"Sensor stats API error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500