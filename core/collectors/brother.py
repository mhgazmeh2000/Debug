"""جمع‌آوری داده از Brother (مونو و رنگی) با fallback کامل"""
import re, time, logging
from datetime import datetime
from core.collectors.base import si, ss, _g, _counters_event, fetch_first_web_page
from core import store

log = logging.getLogger("PrinterMonitor")


def _scrape_brother_toners(ip: str, timeout: float = 4.0):
    """استخراج درصد تونر/درام از پنل وب Brother (EWS) — fallback برای وقتی SNMP
    درصد نمی‌دهد (مثل خانواده‌ی NC-8300h که در جدول مصرفی‌ها level=-3 برمی‌گرداند).

    خروجی: دیکشنری {"black": 0-100، اختیاری "drum": 0-100} یا None.
    """
    urls = [
        f"http://{ip}/general/information.html",
        f"http://{ip}/general/status.html",
        f"http://{ip}/",
        f"https://{ip}/general/information.html",
        f"https://{ip}/",
    ]
    used, page = fetch_first_web_page(ip, urls, timeout)
    if not page:
        log.debug(f"Brother scrape {ip}: web panel unreachable")
        return None
    log.debug(f"Brother scrape {ip}: fetched {used} ({len(page)} chars)")

    # entityها را باز کن — صفحات واقعی Brother (مثل MFC-8510DN با فرم‌ور NC-8300h)
    # فاصله‌ها را به‌صورت &#32; می‌نویسند: «Remaining&#32;Life»، «Drum&#32;Unit*»
    import html as _html
    text = _html.unescape(page)

    result = {}

    # ۱) پارس ساخت‌یافته‌ی جفت‌های <dt>نام</dt><dd>مقدار</dd> در بلوک «Remaining
    #    Life» (تأییدشده روی MFC-8510DN واقعی). در این مدل‌ها تونر اصلاً در لیست
    #    نیست (SNMP هم level=-3 می‌دهد) — پس اگر پیدا نشد هیچ عددی جعل نمی‌کنیم.
    #    بعضی مدل‌ها درصد را در جفت بعدی می‌گذارند: <dt>(% of Life Remaining)</dt>
    pairs = re.findall(r"<dt>\s*([^<]{1,60}?)\s*</dt>\s*<dd>([\s\S]{0,120}?)</dd>", text)
    last_name = None
    for dt_text, dd_text in pairs:
        name = dt_text.strip()
        m = re.search(r"\(?\s*(\d{1,3}(?:\.\d+)?)\s*%", dd_text)
        is_ratio_label = re.search(r"%\s*of\s*life", name, re.IGNORECASE) is not None
        if is_ratio_label and last_name:
            name = last_name  # درصدِ جفت قبلی: <dt>(% of Life Remaining)</dt>
        if not m:
            if not is_ratio_label:
                last_name = name
            continue
        val = float(m.group(1))
        if not (0.0 <= val <= 100.0):
            last_name = name
            continue
        key = None
        nname = name.lower()
        if "toner" in nname:
            key = "black"
        elif "drum" in nname:
            key = "drum"
        if key and key not in result:
            result[key] = int(round(val))
        last_name = name

    # ۲) پترن‌های متنی رایج پنل Brother (قدیمی و جدید) — [\s\S] به‌جای . چون
    # بین برچسب و عدد معمولاً تگ‌های <td>/<img> قرار می‌گیرد
    if "black" not in result:
        for pat in (
            r"Toner\s*Remaining[\s\S]{0,80}?(\d{1,3})\s*%",
            r"(\d{1,3})\s*%[\s\S]{0,40}?Toner\s*Remaining",
            r"toner[^%\d]{0,80}?(\d{1,3})\s*%",
        ):
            m = re.search(pat, text, re.IGNORECASE)
            if m and 0 <= int(m.group(1)) <= 100:
                result["black"] = int(m.group(1))
                break
    if "drum" not in result:
        for pat in (
            r"Drum[\s\S]{0,80}?(\d{1,3})\s*%",
            r"(\d{1,3})\s*%[\s\S]{0,40}?[Dd]rum",
        ):
            m = re.search(pat, text, re.IGNORECASE)
            if m and 0 <= int(m.group(1)) <= 100:
                result["drum"] = int(m.group(1))
                break

    # ۳) صفحات قدیمی: نوار تونر به‌صورت width یا نام فایل تصویر
    if "black" not in result:
        m = re.search(r"[Tt]oner[\s\S]{0,150}?width\s*[:=]\s*[\"']?\s*(\d{1,3})\s*%", text)
        if m and 0 <= int(m.group(1)) <= 100:
            result["black"] = int(m.group(1))
    if "black" not in result:
        m = re.search(r"(?:toner|ink|tnr)[_\-\./]?[a-z]{0,4}_?(\d{1,3})\.(?:gif|png|jpg)", text, re.IGNORECASE)
        if m and 0 <= int(m.group(1)) <= 100:
            result["black"] = int(m.group(1))

    if result:
        log.info(f"Brother {ip} toner scrape result: {result} (source: {used})")
    else:
        log.debug(f"Brother scrape {ip}: no percent found in page")
    return result if result else None


_BRO_TONER_COLOR_MAP = {
    "black": "black", "bk": "black",
    "cyan":  "cyan",  "c": "cyan",
    "magenta": "magenta", "m": "magenta",
    "yellow": "yellow",   "y": "yellow",
    "drum": "drum",
}

def _bro_toner_key(name: str) -> str:
    n = name.lower()
    for kw, key in _BRO_TONER_COLOR_MAP.items():
        if kw in n:
            return key
    return None

def collect_brother(ip: str, name: str, community: str, start: float) -> dict:
    try:
        # افزایش تایم‌اوت کلی به ۱۵ ثانیه
        def g(oid, timeout=1.5):
            if time.time() - start > 15.0: return None
            return _g(ip, oid, community, timeout=timeout)

        ut = si(g("1.3.6.1.2.1.1.3.0", timeout=2.0)); us = ut // 100
        uptime_str = f"{us//86400}d {(us%86400)//3600:02d}:{(us%3600)//60:02d}"

        model  = ss(g("1.3.6.1.4.1.2435.2.4.3.99.3.1.6.1.2.1"), "N/A")
        if model == "N/A": model = ss(g("1.3.6.1.2.1.43.5.1.1.16.1"), "N/A")
        if model == "N/A": model = ss(g("1.3.6.1.2.1.1.5.0"), "Brother")

        serial = ss(g("1.3.6.1.4.1.2435.2.4.3.99.3.1.6.1.2.3"), "N/A")
        if serial == "N/A": serial = ss(g("1.3.6.1.2.1.43.5.1.1.17.1"), "N/A")

        fw = ss(g("1.3.6.1.4.1.2435.2.4.3.1240.6.5.0"), "N/A")
        if fw == "N/A": fw = ss(g("1.3.6.1.4.1.2435.2.3.9.1.1.1.1.3.0"), "N/A")

        total = si(g("1.3.6.1.2.1.43.10.2.1.4.1.1"))
        
        # ═══ Retry برای total=0 مشکوک ═══
        prev_data = store._prev.get(ip) or {}
        prev_total = prev_data.get("print_total") if prev_data else None
        if total == 0 and prev_total is not None and prev_total > 1000:
            log.warning(f"Brother {ip}: total=0 but prev={prev_total}, retrying...")
            retry_val = _g(ip, "1.3.6.1.2.1.43.10.2.1.4.1.1", community, timeout=3.0)
            retry_total = si(retry_val)
            if retry_total > 0:
                log.warning(f"Brother {ip}: retry successful → total={retry_total}")
                total = retry_total
            else:
                # ✅ باگ: ثبت خطا و نگهداری total=0 (نه prev_total)
                log.error(f"Brother {ip}: retry failed, recording SNMP error")
                from core.database import add_event
                add_event(ip, "SNMP_ERROR", {
                    "message": f"SNMP total=0 and retry failed for {ip}",
                    "severity": "error",
                    "prev_total": prev_total,
                })

        color_raw = si(g("1.3.6.1.2.1.43.10.2.1.4.1.2"), -1)

        toners = {}
        has_color_toner = False

        # ابتدا OID اختصاصی Brother را برای تونر مشکی امتحان کن.
        brother_black = si(g("1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.1"), -1)
        if 0 <= brother_black <= 100:
            toners["black"] = {
                "level": brother_black,
                "status": ("empty" if brother_black == 0 else "critical" if brother_black <= 10 else "low" if brother_black <= 25 else "ok"),
                "name": "Black Toner",
                "remaining": brother_black,
                "max": 100,
            }

        for idx in range(1, 6):
            t_name = ss(g(f"1.3.6.1.2.1.43.11.1.1.6.1.{idx}"), "")
            t_max  = si(g(f"1.3.6.1.2.1.43.11.1.1.8.1.{idx}"), -1)
            t_rem  = si(g(f"1.3.6.1.2.1.43.11.1.1.9.1.{idx}"), -3)
            if not t_name and t_max == -1 and t_rem == -3:
                break
            if not t_name:
                if idx == 1: t_name = "Toner"
                elif idx == 2: t_name = "Drum"
                else: t_name = f"Supply {idx}"
            toner_key = _bro_toner_key(t_name) or (f"supply_{idx}")
            if toner_key in ("cyan", "magenta", "yellow"):
                has_color_toner = True
            if t_rem < 0 or t_max <= 0:
                toner_pct, toner_st = None, "unknown"
            else:
                toner_pct = round(t_rem / t_max * 100)
                toner_st  = ("empty"    if toner_pct == 0 else
                             "critical" if toner_pct <= 10 else
                             "low"      if toner_pct <= 25 else "ok")
            toners[toner_key] = {"level": toner_pct, "status": toner_st,
                                 "name": t_name, "remaining": t_rem, "max": t_max}

        if not toners:
            toners["black"] = {"level": None, "status": "unknown",
                               "name": "Toner", "remaining": -1, "max": -1}

        if has_color_toner and 0 <= color_raw < total:
            full_color = color_raw
            bw = max(0, total - color_raw)
            is_color = True
        else:
            full_color = None
            bw = total
            is_color = False

        trays = []
        for idx, label in [(1,"MP Tray"),(2,"Tray 1")]:
            cap = si(g(f"1.3.6.1.2.1.43.8.2.1.9.1.{idx}"),  0)
            lvl = si(g(f"1.3.6.1.2.1.43.8.2.1.10.1.{idx}"), -9)
            nm  = ss(g(f"1.3.6.1.2.1.43.8.2.1.13.1.{idx}"), label)
            if cap == 0 and lvl == -9: continue
            if lvl == -2:  st = "no_sensor"
            elif lvl <= 0: st = "empty"
            elif cap > 0:
                pct = round(lvl / cap * 100)
                st  = "low" if pct <= 25 else ("medium" if pct <= 75 else "ok")
            else: st = "unknown"
            trays.append({"name": nm, "level": lvl, "capacity": cap, "status": st})

        alerts = []
        cover = si(g("1.3.6.1.2.1.43.6.1.1.3.1.1"), 4)
        if cover != 4:
            alerts.append({"message": "درب پرینتر باز است", "code": cover})

        elapsed = int((time.time() - start) * 1000)

        prev = store._prev.get(ip) or {}
        black_level = None
        if toners.get("black", {}).get("level") is not None:
            black_level = toners["black"]["level"]
        else:
            for t in toners.values():
                if t.get("level") is not None:
                    black_level = t["level"]
                    break
        prev_toner = prev.get("toner_level")
        _counters_event(ip, total, prev, alerts, [a["code"] for a in alerts],
                full_color=full_color, black_white=bw, paper_size=None,
                current_toner_level=black_level, prev_toner_level=prev_toner,
                uptime=ut, poll_timestamp=datetime.fromtimestamp(start).isoformat())

        type_tag = "brother-color" if is_color else "brother"
        toner_pct_log = next(iter(toners.values()), {}).get("level")
        log.info(f"  ✓ {name} [{type_tag}] total={total:,} bw={bw:,} "
                 f"toner={toner_pct_log}% {elapsed}ms")

        return {
            "ip": ip, "name": name, "brand": "brother",
            "online": True, "last_poll": datetime.now().isoformat(), "poll_ms": elapsed,
            "device": {"model": model, "serial": serial, "firmware": fw, "uptime_str": uptime_str},
            "counters": {
                "total":       total,
                "full_color":  full_color,
                "black_white": bw,
                "printer":     None,
                "copy":        None,
                "fax":         None,
            },
            "paper_sizes": {}, "trays": trays, "toners": toners, "alerts": alerts,
        }
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        log.exception(f"  ✗ {name} [brother] error: {e} {elapsed}ms")
        return {
            "ip": ip, "name": name, "brand": "brother",
            "online": True, "last_poll": datetime.now().isoformat(), "poll_ms": elapsed,
            "device": {"model": "Unknown", "serial": "N/A", "firmware": "N/A", "uptime_str": "N/A"},
            "counters": {"total": 0, "full_color": None, "black_white": 0,
                         "printer": None, "copy": None, "fax": None},
            "paper_sizes": {}, "trays": [],
            "toners": {"black": {"level": None, "status": "unknown"}},
            "alerts": [{"message": f"Collection error: {e}", "code": 9999}],
        }