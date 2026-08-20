#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ابزار تشخیص پرینتر — برای گزارش مشکل تونر/کارتریج یک مدل خاص.

این اسکریپت همه‌ی داده‌های خام مربوط به مصرفی‌ها را از دستگاه می‌خواند و یک
گزارش Markdown می‌سازد که با ارسال آن، نگاشت دقیق آن مدل به پروژه اضافه می‌شود.

اجرا (از ریشه‌ی پروژه):
    python tools/diagnose_printer.py 172.16.25.10
    python tools/diagnose_printer.py 172.16.25.10 --community private --timeout 3

خروجی: فایل diagnose_<ip>.md در پوشه‌ی جاری.
"""
import argparse
import datetime
import os
import re
import sys

# امکان اجرا از هر پوشه‌ای: ریشه پروژه را به مسیر اضافه کن
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.snmp.protocol import snmp_get_with_fallback  # noqa: E402

MIB = "1.3.6.1.2.1"
PRT_SUPPLIES = f"{MIB}.43.11.1.1"


def g(ip, community, oid, timeout, version=None):
    return snmp_get_with_fallback(ip, oid, community, version=version, timeout=timeout)


def probe(ip, community, timeout, snmp_version=None):
    """جمع‌آوری خام همه‌ی فیلدهای مهم."""
    data = {"ip": ip, "time": datetime.datetime.now().isoformat(timespec="seconds")}

    data["sysDescr"] = g(ip, community, f"{MIB}.1.1.0", timeout, snmp_version)
    data["sysObjectID"] = g(ip, community, f"{MIB}.1.2.0", timeout, snmp_version)
    data["sysUpTime"] = g(ip, community, f"{MIB}.1.3.0", timeout, snmp_version)
    data["model_prtMIB"] = g(ip, community, f"{MIB}.43.5.1.1.16.1", timeout, snmp_version)
    data["serial_prtMIB"] = g(ip, community, f"{MIB}.43.5.1.1.17.1", timeout, snmp_version)
    data["life_count"] = g(ip, community, f"{MIB}.43.10.2.1.4.1.1", timeout, snmp_version)

    # جدول مصرفی‌ها — ۱۰ ردیف اول، همه‌ی ستون‌های کلیدی
    # .3=class .4=colorantIndex .5=type .6=description .7=unit .8=max .9=level
    rows = []
    for idx in range(1, 11):
        name = g(ip, community, f"{PRT_SUPPLIES}.6.1.{idx}", timeout, snmp_version)
        if name is None and idx > 4:
            break
        row = {
            "idx": idx,
            "name": name,
            "class": g(ip, community, f"{PRT_SUPPLIES}.3.1.{idx}", timeout, snmp_version),
            "type": g(ip, community, f"{PRT_SUPPLIES}.5.1.{idx}", timeout, snmp_version),
            "unit": g(ip, community, f"{PRT_SUPPLIES}.7.1.{idx}", timeout, snmp_version),
            "max": g(ip, community, f"{PRT_SUPPLIES}.8.1.{idx}", timeout, snmp_version),
            "level": g(ip, community, f"{PRT_SUPPLIES}.9.1.{idx}", timeout, snmp_version),
        }
        if all(v is None for k, v in row.items() if k != "idx"):
            if idx > 2:
                break
            continue
        rows.append(row)
    data["supplies"] = rows

    # رنگ‌ها (prtMarkerColorantValue)
    colors = []
    for idx in range(1, 7):
        v = g(ip, community, f"{MIB}.43.12.1.1.4.1.{idx}", timeout, snmp_version)
        if v is not None:
            colors.append((idx, v))
    data["colorants"] = colors

    # OIDهای خصوصی HP (JetDirect/NPCL) و Canon (NETEYE 1602)
    hp_oids = [
        "1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.2.4.1.2.1.5.5.1.1",
        "1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.2.1.5.5.1.1",
        "1.3.6.1.4.1.11.2.3.9.1.1.7.0",
        "1.3.6.1.4.1.11.2.3.9.1.1.3.1.1.1.1.2.0",   # model
        "1.3.6.1.4.1.11.2.3.9.1.1.3.1.1.1.1.3.0",   # serial
    ]
    canon_oids = [
        "1.3.6.1.4.1.1602.1.1.1.1.0",               # model
        "1.3.6.1.4.1.1602.1.2.1.4.0",               # serial
        "1.3.6.1.4.1.1602.1.2.1.1.1.1.1",
        "1.3.6.1.4.1.1602.1.2.1.1.1.2.1",
    ]
    data["hp_private"] = {o: g(ip, community, o, timeout, snmp_version) for o in hp_oids}
    data["canon_private"] = {o: g(ip, community, o, timeout, snmp_version) for o in canon_oids}

    # OIDهای اختصاصی Brother — خانواده‌ی NC-8xxx/MFC-85xx تونر را در جدول
    # استاندارد با level=-3 (مقداری هست) پنهان می‌کند. این کاندیداها از MIBهای
    # عمومی Brother (brInfo/brToner) برداشته شده‌اند؛ هرکدام پاسخ بدهد نگاشت
    # دقیقش را در کالکتور می‌گذاریم.
    brother_oids = [
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.8.0",     # toner remaining (بعضی مدل‌ها)
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.10.0",    # drum/counter candidate
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.10.0",  # brInfo status candidate
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.11.0",
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.12.0",
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.13.0",
        "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.14.0",
        "1.3.6.1.4.1.2435.2.4.3.99.3.1.6.1.2.1",     # model (بسیاری NC-boardها)
        "1.3.6.1.4.1.2435.2.4.3.99.3.1.6.1.2.3",     # serial
    ]
    desc_l = str(data.get("sysDescr") or "").lower()
    if "brother" in desc_l:
        data["brother_private"] = {o: g(ip, community, o, timeout, snmp_version) for o in brother_oids}
    else:
        data["brother_private"] = {}

    # PJL روی پورت 9100 — پاسخ @PJL INFO STATUS گاهی وضعیت مصرفی‌ها را متنی
    # می‌دهد. اگر پورت بسته باشد بی‌صدا رد می‌شویم.
    data["pjl_status"] = _probe_pjl(ip, timeout=min(timeout, 2.5))

    # صفحه‌ی وب دستگاه — دسترسی HTTP و HTTPS + دامپ محتوای مرتبط با تونر/کارتریج
    # (برای فیکس‌های مبتنی‌بر اسکریپ: HP با SuppliesStatus/DevMgmt، Brother با
    # صفحات EWS، Canon با Remote UI). خروجی «toner_contexts» قطعه‌های HTML اطراف
    # کلیدواژه‌هاست تا پترن regex دقیق برای هر مدل نوشته شود.
    from core.collectors.base import fetch_first_web_page

    web = {}
    desc_l = str(data.get("sysDescr") or "").lower()
    hint = next((b for tok, b in (
        ("brother", "brother"), ("canon", "canon"),
        ("hp ", "hp"), ("laserjet", "hp"), ("jetdirect", "hp"),
        ("toshiba", "toshiba"),
    ) if tok in desc_l), None)
    page_paths = {
        "hp": ["/hp/device/InternalPages/Index?id=SuppliesStatus",
               "/hp/info/suppliesStatus.html",
               "/DevMgmt/ConsumableConfigDyn.xml", "/"],
        "brother": ["/general/information.html", "/general/status.html", "/"],
        "canon": ["/", "/Status.html", "/status.html", "/supply.html"],
        None: ["/", "/status.html", "/supply.html"],
    }[hint]
    data["_web_brand_hint"] = hint

    kw_re = re.compile(r"toner|drum|cartridge|suppl|percent", re.IGNORECASE)
    pct_re = re.compile(r"\d{1,3}\s*%")

    for p in page_paths:
        body = None
        tried = []
        for scheme in ("http", "https"):
            url = f"{scheme}://{ip}{p}"
            used, body = fetch_first_web_page(ip, [url], timeout=min(timeout, 5.0))
            if body:
                break
            tried.append(type(None).__name__ if used is None else "error")
        if not body:
            web[f"http(s)://{ip}{p}"] = {"error": "unreachable"}
            continue
        entry = {
            "status": 200,
            "final_url": used,
            "chars": len(body),
            "snippet": " ".join(body.split())[:400],
        }
        # دامپ کامل صفحات کوچک (XMLها و صفحات Brother/HP کلاسیک) — برای نهایی‌کردن
        # دقیق پترن‌های اسکریپ، کانتکست‌های ناقص کافی نیستند.
        if len(body) <= 12000:
            entry["full"] = body
        contexts = []
        for m in kw_re.finditer(body):
            start = max(0, m.start() - 90)
            ctx = " ".join(body[start:m.end() + 110].split())
            if ctx not in contexts:
                contexts.append(ctx)
            if len(contexts) >= 25:
                break
        for m in pct_re.finditer(body):
            start = max(0, m.start() - 90)
            ctx = " ".join(body[start:m.end() + 60].split())
            if ctx not in contexts:
                contexts.append(ctx)
            if len(contexts) >= 35:
                break
        entry["toner_contexts"] = contexts
        web[used] = entry
    data["web"] = web
    return data


def _probe_pjl(ip: str, timeout: float = 2.5) -> str:
    """ارسال فرمان PJL به پورت 9100 و خواندن پاسخ (در صورت باز بودن)."""
    import socket
    try:
        with socket.create_connection((ip, 9100), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"\x1b%-12345X@PJL\r\n@PJL INFO STATUS\r\n\x1b%-12345X")
            chunks = []
            try:
                while sum(len(c) for c in chunks) < 2048:
                    data = s.recv(1024)
                    if not data:
                        break
                    chunks.append(data)
            except socket.timeout:
                pass
            raw = b"".join(chunks)
            return raw.decode("latin-1", errors="replace").strip() or "(پاسخ خالی)"
    except Exception as exc:
        return f"(در دسترس نیست: {exc.__class__.__name__})"


def render_md(d):
    L = []
    L.append(f"# گزارش تشخیص پرینتر {d['ip']}")
    L.append(f"- زمان: {d['time']}")
    for k in ("sysDescr", "sysObjectID", "sysUpTime", "model_prtMIB", "serial_prtMIB", "life_count"):
        L.append(f"- **{k}**: `{d.get(k)}`")
    L.append("")
    L.append("## جدول prtMarkerSuppliesTable (خام)")
    L.append("")
    L.append("| idx | description | class | type | unit | max | level |")
    L.append("|---|---|---|---|---|---|---|")
    for r in d["supplies"]:
        L.append(f"| {r['idx']} | {r['name']} | {r['class']} | {r['type']} | {r['unit']} | {r['max']} | {r['level']} |")
    L.append("")
    L.append("### عناصر کلیدی تفسیر")
    L.append("- unit: `19` یعنی درصد؛ `8` sheets ؛ `7` impressions")
    L.append("- level/max: `-1` بدون محدودیت، `-2` نامشخص، `-3` «مقداری هست»")
    L.append("- Canon i-SENSYS ممکن است کد گسسته `0=Empty / 5=Low / 7=OK` بدهد")
    L.append("")
    if d["colorants"]:
        L.append("## prtMarkerColorantValue")
        for idx, v in d["colorants"]:
            L.append(f"- idx {idx}: `{v}`")
        L.append("")
    for sect, title in (("hp_private", "OIDهای خصوصی HP"), ("canon_private", "OIDهای خصوصی Canon"),
                        ("brother_private", "OIDهای خصوصی Brother")):
        if not d.get(sect):
            continue
        L.append(f"## {title}")
        for o, v in d[sect].items():
            L.append(f"- `{o}` → `{v}`")
        L.append("")
    if d.get("pjl_status"):
        L.append("## پاسخ PJL (پورت 9100)")
        L.append("```")
        L.append(str(d["pjl_status"]))
        L.append("```")
        L.append("")
    if d.get("web"):
        L.append("## پنل وب دستگاه")
        if d.get("_web_brand_hint"):
            L.append(f"- حدس برند از sysDescr: `{d['_web_brand_hint']}`")
        for url, info in d["web"].items():
            if "error" in info:
                L.append(f"- {url} → `{info}`")
            else:
                L.append(f"- {url} → status={info['status']} chars={info['chars']} final=`{info['final_url']}`")
                L.append(f"  - snippet: `{info['snippet']}`")
                if info.get("toner_contexts"):
                    L.append("  - 🔍 تکه‌های مرتبط با تونر/کارتریج (برای پترن‌های اسکریپ):")
                    for c in info["toner_contexts"]:
                        L.append(f"    - `{c}`")
                if info.get("full") is not None:
                    L.append("  - 📄 دامپ کامل صفحه:")
                    L.append("")
                    L.append("```html")
                    L.append(info["full"])
                    L.append("```")
                    L.append("")
        L.append("")
    return "\n".join(L)


def _scan_toshiba_paper_map(ip, community, timeout, snmp_version):
    """اسکن شاخه‌های جدول شمارنده‌ی خصوصی Toshiba برای کشف نگاشت دقیق سایز.

    نگاشت شناخته‌شده (اثبات روی 2050C/3015AC):
      207=کل بزرگ، 208=کل کوچک، 209=پرینت(بزرگ)، 210=پرینت(کوچک)، 227=نامشخص
    اگر شاخه‌های دیگری (مثلاً A3 لحظه‌ای یا copy-per-size) روی دستگاه پاسخ بدهند،
    با اجرای این پروب بلافاصله پس از یک چاپ A3 مشخص می‌شوند.
    """
    base = "1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1"
    known = {2: "print_total", 3: "printer", 4: "copy", 7: "fax", 8: "scan_net",
             9: "scan", 11: "list", 207: "ALL large", 208: "ALL small",
             209: "PRINT large", 210: "PRINT small", 227: "unknown-227"}
    cols = {1: "fc", 2: "twin?", 3: "bw", 4: "total"}
    lines = ["", "## Toshiba paper counter map (--paper-map)", "",
             "| branch | fc(1) | twin?(2) | bw(3) | total(4) | نگاشت |",
             "|---|---|---|---|---|---|"]
    found = 0
    print(f"[..] scanning Toshiba counter branches 200-240 on {ip} ...")
    for branch in range(200, 241):
        vals = {}
        for c in cols:
            v = g(ip, community, f"{base}.{branch}.1.{c}", timeout, snmp_version)
            if v not in (None, "", "N/A"):
                vals[c] = v
        if not vals:
            continue
        found += 1
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            branch,
            vals.get(1, "—"), vals.get(2, "—"), vals.get(3, "—"), vals.get(4, "—"),
            known.get(branch, "❓ جدید — کاندید نگاشت سایز")))
    if not found:
        lines.append("| — | — | — | — | — | هیچ شاخه‌ای پاسخ نداد |")
    print(f"[OK] paper-map scan done: {found} active branch(es)")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Printer supply diagnostic dumper")
    ap.add_argument("ip")
    ap.add_argument("--community", default="public")
    ap.add_argument("--timeout", type=float, default=2.5)
    ap.add_argument("--snmp-version", type=int, default=None, choices=[1, 2])
    ap.add_argument(
        "--paper-map", action="store_true",
        help="Toshiba: اسکن شاخه‌های شمارنده ۲۰۰ تا ۲۴۰ (ستون‌های ۱..۴) برای کشف "
             "نگاشت دقیق سایز کاغذ (A3/B4/A5 لحظه‌ای). بهتر است بلافاصله پس از "
             "چاپ یک صفحه A3 اجرا شود تا شاخه‌ی فقط‌A3 مشخص شود.")
    args = ap.parse_args()

    print(f"[..] probing {args.ip} (community={args.community!r}) ...")
    data = probe(args.ip, args.community, args.timeout, args.snmp_version)
    md = render_md(data)
    if args.paper_map:
        md += _scan_toshiba_paper_map(args.ip, args.community, args.timeout, args.snmp_version)
    out = f"diagnose_{args.ip}.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"[OK] report written: {out}")
    print("     این فایل را ارسال کنید تا نگاشت دقیق مدل به پروژه اضافه شود.")


if __name__ == "__main__":
    main()
