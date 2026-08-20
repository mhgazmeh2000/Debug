#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ممیزی سازگاری لاگ صفحات با شمارنده‌ی دستگاه (tools/audit_pages.py)

منطق:
  برای هر پرینتر، در یک بازه‌ی زمانی:
    Δcounter = جمع دلتاهای مثبت شمارنده‌ی عمر دستگاه (از snapshotهای معتبر DB)
    real     = جمع صفحات رویدادهای PRINT (غیرتخمینی) در همان بازه
    est      = جمع صفحات رویدادهای تخمینی (PRINT_GAP / estimated)
    diff     = Δcounter - real - est
  |diff| کوچک ⇒ دفترداری سازگار است؛ diff منفی بزرگ ⇒ لاگ بیش از واقعیت
  (مانند باگ قدیمی بازگشت-شمارنده پس از ریبوت)؛ diff مثبت بزرگ ⇒ چاپ بدون لاگ.

اجرا (از ریشه‌ی پروژه):
    python tools/audit_pages.py                     # همه دستگاه‌ها، کل بازه
    python tools/audit_pages.py --days 7            # بازه‌ی ۷ روز اخیر (نسبت به جدیدترین داده)
    python tools/audit_pages.py --ip 172.16.25.36 --md audit.md
    python tools/audit_pages.py --json audit.json

نکته: «شمارنده‌ی فعلی» = آخرین snapshot معتبر در DB (یعنی مقدار شمارنده‌ی
دستگاه در آخرین پایش موفق؛ حداکثر POLL_INTERVAL قدمت دارد). برای خواندن زنده‌ی
لحظه‌ای شمارنده از tools/diagnose_printer.py استفاده کنید.

مبنای دلتا (شکاف‌آگاه): آخرین snapshot معتبرِ *قبل* از شروع پنجره — چون رویدادهای
داخل پنجره ممکن است حاصل حرکت شمارنده در زمانِ قبل از پنجره باشند (خاموشی چندساعته‌ی
سرور → ثبت جبرانی در اولین پایش). شکاف‌های پایش >۱۵ دقیقه هم در گزارش دیده می‌شوند.
"""
import argparse
import datetime as _dt
import json
import os
import sqlite3
import sys

# امکان اجرا از هر پوشه‌ای
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# آستانه‌ی سازگاری: حداکثر این مقدار اختلاف نادیده گرفته می‌شود
TOL_ABS = 5           # صفحه (کف جبرانی برای نویز گردکردن لبه‌ی پنجره)
TOL_RATIO = 0.005     # ۰٫۵٪ دلتای شمارنده
EST_SHARE_WARN = 0.30 # هشدار اگر بیش از ۳۰٪ لاگ تخمینی باشد
GAP_MIN_SECONDS = 900 # فاصله‌ی بیش از ۱۵ دقیقه بین دو خوانش = «شکاف پایش» (خاموشی سرور/قطعی)

PAGE_TYPES = ("PRINT", "PRINT_GAP")


def _fmt(n):
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def _window_args(args, latest_ts):
    """محاسبه‌ی t0/t1 از روی آرگومان‌ها. اگر چیزی داده نشده باشد → کل بازه."""
    t1 = None
    if args.until:
        t1 = args.until if "T" in args.until else args.until + "T23:59:59"
    t0 = None
    if args.since:
        t0 = args.since if "T" in args.since else args.since + "T00:00:00"
    if args.days is not None and not t0:
        base = latest_ts or _dt.datetime.now().isoformat(timespec="seconds")
        try:
            end_dt = _dt.datetime.fromisoformat((t1 or base)[:19])
        except ValueError:
            end_dt = _dt.datetime.now()
        t0 = (end_dt - _dt.timedelta(days=args.days)).isoformat(timespec="seconds")
    return t0, t1


def audit_printer(conn, ip, t0=None, t1=None):
    """ممیزی یک پرینتر در بازه [t0, t1]. خروجی: dict نتیجه."""
    q_snap = (
        "SELECT timestamp, print_total, printer_model FROM toner_snapshots_v2 "
        "WHERE printer_ip=? AND color='black' AND valid=1 AND print_total IS NOT NULL"
    )
    params = [ip]
    if t0:
        q_snap += " AND timestamp>=?"; params.append(t0)
    if t1:
        q_snap += " AND timestamp<=?"; params.append(t1)
    q_snap += " ORDER BY timestamp"
    snaps = conn.execute(q_snap, params).fetchall()

    # ─── لنگر شکاف‌آگاه: مبنای دلتا = آخرین snapshot معتبرِ *قبل* از شروع
    # پنجره. دلیل: رویدادهای داخل پنجره ممکن است حاصل حرکت شمارنده در زمان‌های
    # *قبل* از پنجره باشند (مورد واقعی ۱۶ آگوست: سرور ۸۹ ساعت خاموش بود؛ در
    # اولین خوانش یکشنبه، دلتای انباشته به‌عنوان PRINTِ داخل پنجره ثبت شد در
    # حالی که مصرف شمارنده‌اش پیش از پنجره رخ داده بود — بدون این لنگر، ممیزی
    # کاذب «لاگ بیش از شمارنده» گزارش می‌کرد). ───
    anchor = None
    if t0:
        anchor = conn.execute(
            "SELECT timestamp, print_total FROM toner_snapshots_v2 "
            "WHERE printer_ip=? AND color='black' AND valid=1 AND print_total IS NOT NULL "
            "AND timestamp<? ORDER BY timestamp DESC LIMIT 1", (ip, t0)).fetchone()

    # ─── شکاف‌های پایش (گپ‌های >GAP_MIN_SECONDS بین خوانش‌های معتبر) ───
    gaps = []
    def _gap(a, b):
        try:
            dt = (_dt.datetime.fromisoformat(str(b)[:19]) - _dt.datetime.fromisoformat(str(a)[:19])).total_seconds()
        except ValueError:
            return 0
        return dt
    if anchor and snaps and _gap(anchor[0], snaps[0][0]) > GAP_MIN_SECONDS:
        gaps.append([str(anchor[0])[:19], str(snaps[0][0])[:19],
                     round(_gap(anchor[0], snaps[0][0]) / 3600, 1)])
    for i in range(1, len(snaps)):
        if _gap(snaps[i-1][0], snaps[i][0]) > GAP_MIN_SECONDS:
            gaps.append([str(snaps[i-1][0])[:19], str(snaps[i][0])[:19],
                         round(_gap(snaps[i-1][0], snaps[i][0]) / 3600, 1)])

    # ─── دلتای شمارنده به روش قطعه‌ای (مصون از ریست/بازگشت NVRAM) ───
    # بعد از افت شمارنده (ریبوت)، بازگشت تا «سطح قبل از افت» چاپ جدید حساب
    # نمی‌شود (همان الگوی reboot-restore که کامیت COUNTER_RESTORED در کالکتور
    # اصلاح کرد — اینجا همان معناشناسی در سمت ممیزی تکرار می‌شود).
    counter_delta = 0
    resets = 0
    reset_ref = None
    restore_tol = 0
    first_total = last_total = None
    first_ts = last_ts = None
    prev = anchor[1] if anchor else None
    anchor_ts = anchor[0] if anchor else None
    model = "—"
    for ts, total, mdl in snaps:
        if mdl:
            model = mdl
        if first_total is None:
            first_total, first_ts = total, ts
        last_total, last_ts = total, ts
        if prev is not None:
            if reset_ref is not None:
                # در ناحیه‌ی بازگشت: فقط وقتی از سطح قبل از افت (+تلورانس) عبور کرد،
                # مازاد آن چاپ واقعی جدید است.
                if total > reset_ref + restore_tol:
                    counter_delta += total - reset_ref
                    reset_ref = None
            elif total >= prev:
                counter_delta += total - prev
            else:
                resets += 1  # افت شمارنده (ریبوت واقعی/بازنویسی)
                reset_ref = prev
                restore_tol = max(50, int(prev * 0.005))
        prev = total

    # ─── جمع صفحات لاگ ───
    q_log = (
        "SELECT type, pages, timestamp, details FROM logs "
        "WHERE printer_ip=? AND type IN (?, ?) AND pages IS NOT NULL"
    )
    params = [ip, *PAGE_TYPES]
    if t0:
        q_log += " AND timestamp>=?"; params.append(t0)
    if t1:
        q_log += " AND timestamp<=?"; params.append(t1)
    q_log += " ORDER BY timestamp"
    rows = conn.execute(q_log, params).fetchall()

    real = est = 0
    gap_events = []
    for typ, pages, ts, details in rows:
        try:
            pages = int(pages)
        except (TypeError, ValueError):
            continue
        is_est = (typ == "PRINT_GAP")
        if not is_est and details:
            try:
                is_est = bool(json.loads(details).get("estimated"))
            except (ValueError, TypeError):
                pass
        if is_est:
            est += pages
            gap_events.append({"ts": ts, "pages": pages, "type": typ})
        else:
            real += pages

    diff = counter_delta - real - est
    tol = max(TOL_ABS, int(counter_delta * TOL_RATIO))
    logged = real + est
    est_share = (est / logged) if logged else 0.0

    if not snaps and not rows:
        verdict = "— بدون داده"
        ok = None
    elif len(snaps) == 0 or (len(snaps) < 2 and anchor is None):
        # ⛔ بدون مرجع: هیچ snapshot معتبری داخل پنجره نیست (حتی اگر لنگر
        # قبل از پنجره هست، بدون نقطه‌ی داخل پنجره دلتای مصرفی ساخته نمی‌شود)،
        # یا فقط یک نقطه و بدون لنگر → تطبیق ممکن نیست (جلوگیری از مثبت‌کاذب
        # «لاگ بیش از شمارنده» برای دستگاه‌هایی با snapshotهای ردشده‌ی مداوم).
        verdict = "⛔ بدون مرجع (snapshot معتبر کافی نیست)"  # نه داخل پنجره نه لنگر کافی
        if est:
            verdict += f" · ⚠️ {_fmt(est)} صفحه تخمینی تاریخی"
        ok = None
    elif diff < -tol:
        verdict = "❌ لاگ بیش از شمارنده (تخمین/لاگ مشکوک)"
        ok = False
    elif diff > tol:
        verdict = "⚠️ شمارنده بیش از لاگ (چاپ ثبت‌نشده)"
        ok = False
    else:
        verdict = "✅ سازگار" + (" (شامل تخمین)" if est else "")
        ok = True
    if ok and est_share > EST_SHARE_WARN:
        verdict += f" · ⚠️ سهم تخمین {est_share:.0%}"
        ok = None

    return {
        "ip": ip,
        "model": model,
        "window": [t0 or (first_ts or "—"), t1 or (last_ts or "—")],
        "snapshots": len(snaps),
        "first_total": first_total,
        "last_total": last_total,
        "counter_delta": counter_delta,
        "resets": resets,
        "real_pages": real,
        "est_pages": est,
        "logged_total": logged,
        "diff": diff,
        "tolerance": tol,
        "est_share": round(est_share, 3),
        "verdict": verdict,
        "ok": ok,
        "gap_events": gap_events,
        "anchor_ts": anchor_ts,
        "monitor_gaps": gaps,
    }


_SERVER_PROBE_MIN_HITS = 20  # حداقل اسنپِ دستگاه‌های «خارج از خوشه» تا بگوییم سرور روشن بوده


def _cluster_gaps(results, min_fleet=5, bucket_hours=2, server_probe=None):
    """شکاف‌هایی که لحظه‌ی پایانشان نزدیک هم است (±bucket_hours) و روی حداقل
    min_fleet دستگاه دیده شوند → «شکاف همگانی».
    بقیه → دستگاهی (احتمالاً دیر اضافه شدن دستگاه یا قطعی همان دستگاه).

    server_probe(start_iso, end_iso, member_ips) → True یعنی در آن بازه دستگاه‌های
    دیگر اسنپ داشته‌اند (پس سرور روشن بوده و خاموشی سمتِ دستگاه‌هاست)؛ False یعنی
    هیچ دستگاهی دیده نشده (خاموشی/قطعی خودِ سرور مانیتور). پنجره‌ی سنجش = از
    میانه‌ی شروع‌ها تا میانه‌ی پایان‌های اعضا (نه گستره‌ی کامل، چون اعضای
    زودقطع/دیروصل لبه‌ها را کش می‌دهند)."""
    all_gaps = [(g, r["ip"]) for r in results for g in r.get("monitor_gaps", [])]
    if not all_gaps:
        return [], []
    def _p(x):
        return _dt.datetime.fromisoformat(str(x)[:19])
    remaining = sorted(all_gaps, key=lambda x: x[0][1])
    used = [False] * len(remaining)
    clusters = []
    for i, (g, ip) in enumerate(remaining):
        if used[i]:
            continue
        grp = [(g, ip)]
        used[i] = True
        for j in range(i + 1, len(remaining)):
            if used[j]:
                continue
            g2, ip2 = remaining[j]
            if abs((_p(g2[1]) - _p(g[1])).total_seconds()) <= bucket_hours * 3600:
                grp.append((g2, ip2))
                used[j] = True
        clusters.append(grp)
    fleet = []
    for grp in clusters:
        if len(grp) < min_fleet:
            continue
        starts = sorted(g[0] for g, _ in grp)
        ends = sorted(g[1] for g, _ in grp)
        ms, me = starts[len(starts) // 2], ends[len(ends) // 2]
        server_on = None
        if server_probe is not None and ms < me:
            server_on = server_probe(ms, me, sorted({ip for _, ip in grp}))
        fleet.append({
            "gaps": grp, "a0": starts[0], "b1": ends[-1],
            "durs": sorted(float(g[2]) for g, _ in grp), "server_on": server_on,
        })
    rest = [x for c in clusters if len(c) < min_fleet for x in c]
    return fleet, rest


def render_console(results, t0, t1, server_probe=None):
    line = "═" * 118
    print(line)
    print("🧾 گزارش ممیزی سازگاری «لاگ صفحات» با «شمارنده‌ی دستگاه»")
    w0 = t0 or min((r["window"][0] for r in results if r["window"][0] != "—"), default="—")
    w1 = t1 or max((r["window"][1] for r in results if r["window"][1] != "—"), default="—")
    print(f"بازه: {str(w0)[:19]}  ←  {str(w1)[:19]}   |   آستانه‌ی سازگاری: ±{_fmt(TOL_ABS)} صفحه یا ±۰٫۵٪")
    print(line)
    head = f"{'IP':<15} {'Δشمارنده':>12} {'واقعی':>10} {'تخمینی':>10} {'اختلاف':>12} {'ریست':>4}  نتیجه"
    print(head)
    print("─" * 118)
    for r in results:
        print(
            f"{r['ip']:<15} {_fmt(r['counter_delta']):>12} {_fmt(r['real_pages']):>10} "
            f"{_fmt(r['est_pages']):>10} {_fmt(r['diff']):>12} {r['resets']:>4}  {r['verdict']}"
        )
    print("─" * 118)
    n_ok = sum(1 for r in results if r["ok"] is True)
    n_bad = sum(1 for r in results if r["ok"] is False)
    n_noref = sum(1 for r in results if r["ok"] is None)
    print(f"جمع: {len(results)} دستگاه | ✅ سازگار: {n_ok} | ⚠️/❌ ناسازگار: {n_bad} | ⛔ بدون مرجع: {n_noref}")
    for r in results:
        if r["ok"] is False or (r["ok"] is None and r["est_pages"] > 0):
            print(f"\n🔎 {r['ip']} ({r['model']}) — {r['verdict']}:")
            if r["snapshots"] < 2 and not r.get("anchor_ts"):
                print(f"   snapshot معتبر در بازه: {r['snapshots']} (تطبیق ممکن نیست؛ فقط حجم رویدادها گزارش می‌شود)")
            for g in sorted(r["gap_events"], key=lambda x: -x["pages"])[:5]:
                print(f"   [{str(g['ts'])[:19]}] {g['type']}: {_fmt(g['pages'])} صفحه")

    # ─── شکاف‌های پایش سرور (خاموشی/قطعی) — خوشه‌بندی‌شده ───
    fleet_gaps, dev_gaps = _cluster_gaps(results, server_probe=server_probe)
    if fleet_gaps:
        for fc in fleet_gaps:
            grp = fc["gaps"]; a0, b1 = fc["a0"], fc["b1"]
            hh = round((_dt.datetime.fromisoformat(b1) - _dt.datetime.fromisoformat(a0)).total_seconds() / 3600, 1)
            if fc["server_on"] is True:
                why = "سرور هم‌زمان دستگاه‌های دیگر را دیده → خاموشی/قطعی همگانی «دستگاه‌ها» (نه سرور)"
            elif fc["server_on"] is False:
                why = "هیچ دستگاهی در این بازه دیده نشده → سرور مانیتور خاموش/قطع بوده"
            else:
                why = "سرور مانیتور خاموش/قطع بوده"
            print(f"\n⏸ شکاف همگانی پایش: {a0} ← {b1}  (~{hh} ساعت) روی {len(grp)} دستگاه — {why}")
            d = fc["durs"]
            if len(d) >= 3:
                print(f"   مدت خاموشی اعضا: از {d[0]:.1f} تا {d[-1]:.1f} ساعت (میانه {d[len(d) // 2]:.1f})")
    if dev_gaps:
        dev_gaps.sort(key=lambda x: -x[0][2])
        print(f"\n⏸ شکاف‌های دستگاهی ({len(dev_gaps)}):")
        for (a, b, h), ip in dev_gaps[:5]:
            print(f"   {a} ← {b}  ({h} ساعت)  روی {ip}")
    if fleet_gaps or dev_gaps:
        print("   نکته: رویدادهای PRINT بلافاصله بعد از شکاف، «جبران انباشته» هستند —")
        print("   صفحات واقعاً چاپ شده‌اند و فقط با تأخیر ثبت شده‌اند (ممیزی با لنگر قبل از پنجره تطبیق داده شد).")


def render_markdown(results, t0, t1, server_probe=None):
    w0 = t0 or min((r["window"][0] for r in results if r["window"][0] != "—"), default="—")
    w1 = t1 or max((r["window"][1] for r in results if r["window"][1] != "—"), default="—")
    out = [
        "# 🧾 گزارش ممیزی سازگاری لاگ صفحات با شمارنده‌ی دستگاه",
        "",
        f"- بازه: `{str(w0)[:19]}` ← `{str(w1)[:19]}`",
        f"- آستانه‌ی سازگاری: ±{_fmt(TOL_ABS)} صفحه یا ±{TOL_RATIO:.1%} دلتای شمارنده",
        "- Δشمارنده از snapshotهای معتبر (قطعه‌ای؛ افت‌های شمارنده به‌عنوان ریست شمرده و در دلتا حساب نمی‌شوند)",
        "",
        "| IP | مدل | Δشمارنده | چاپ واقعی | تخمینی | جمع لاگ | اختلاف | ریست | نتیجه |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in results:
        out.append(
            f"| {r['ip']} | {r['model']} | {_fmt(r['counter_delta'])} | {_fmt(r['real_pages'])} "
            f"| {_fmt(r['est_pages'])} | {_fmt(r['logged_total'])} | {_fmt(r['diff'])} "
            f"| {r['resets']} | {r['verdict']} |"
        )
    bad = [r for r in results if r["ok"] is False or (r["ok"] is None and r["est_pages"] > 0)]
    if bad:
        out += ["", "## 🔎 رویدادهای تخمینی برجسته (ناسازگار یا بدون مرجع)", ""]
        for r in bad:
            out.append(f"### {r['ip']} — {r['model']} — {r['verdict']}")
            if r["snapshots"] < 2:
                out.append(f"- snapshot معتبر در بازه: **{r['snapshots']}** (تطبیق ممکن نیست)")
            for g in sorted(r["gap_events"], key=lambda x: -x["pages"])[:10]:
                out.append(f"- `[{str(g['ts'])[:19]}]` {g['type']}: **{_fmt(g['pages'])}** صفحه")
            out.append("")
    fleet_gaps, dev_gaps = _cluster_gaps(results, server_probe=server_probe)
    if fleet_gaps:
        out += ["", "## ⏸ شکاف‌های همگانی پایش", ""]
        for fc in fleet_gaps:
            grp = fc["gaps"]; a0, b1 = fc["a0"], fc["b1"]
            hh = round((_dt.datetime.fromisoformat(b1) - _dt.datetime.fromisoformat(a0)).total_seconds() / 3600, 1)
            if fc["server_on"] is True:
                why = "سرور روشن بوده ← خاموشی/قطعی همگانی **دستگاه‌ها**"
            elif fc["server_on"] is False:
                why = "هیچ دستگاهی دیده نشده ← **خاموشی/قطعی سرور مانیتور**"
            else:
                why = "احتمال خاموشی/قطعی سرور مانیتور"
            d = fc["durs"]
            extra = (f" — مدت اعضا: {d[0]:.1f} تا {d[-1]:.1f} ساعت "
                     f"(میانه {d[len(d) // 2]:.1f})") if len(d) >= 3 else ""
            out.append(f"- **{a0} ← {b1}** (~{hh} ساعت) روی **{len(grp)} دستگاه** — {why}{extra}")
    if dev_gaps:
        dev_gaps.sort(key=lambda x: -x[0][2])
        out += ["", "## ⏸ شکاف‌های دستگاهی", ""]
        out.append("| از | تا | ساعت | دستگاه |")
        out.append("|---|---|---:|---|")
        for (a, b, h), ip in dev_gaps[:15]:
            out.append(f"| {a} | {b} | {h} | {ip} |")
    if fleet_gaps or dev_gaps:
        out.append("")
        out.append("> رویدادهای PRINT بلافاصله بعد از شکاف «جبران انباشته» هستند: صفحات واقعاً چاپ شده‌اند")
        out.append("> و فقط با تأخیر ثبت شده‌اند. ممیزی با لنگر آخرین snapshot قبل از پنجره تطبیق داده شده است.")
    n_ok = sum(1 for r in results if r["ok"] is True)
    n_bad = sum(1 for r in results if r["ok"] is False)
    n_noref = sum(1 for r in results if r["ok"] is None)
    out += [
        "---",
        f"جمع: **{len(results)}** دستگاه | ✅ سازگار: **{n_ok}** | ⚠️/❌ ناسازگار: **{n_bad}** | ⛔ بدون مرجع: **{n_noref}**",
        "",
    ]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="ممیزی سازگاری لاگ صفحات با شمارنده‌ی دستگاه")
    ap.add_argument("--db", default="logs.db", help="مسیر فایل دیتابیس (پیش‌فرض: logs.db)")
    ap.add_argument("--ip", action="append", help="فقط این پرینتر (قابل تکرار)")
    ap.add_argument("--days", type=int, default=None, help="بازه‌ی N روز اخیر (نسبت به جدیدترین داده‌ی DB)")
    ap.add_argument("--since", help="از تاریخ (YYYY-MM-DD یا ISO)")
    ap.add_argument("--until", help="تا تاریخ (YYYY-MM-DD یا ISO)")
    ap.add_argument("--md", help="ذخیره‌ی گزارش Markdown در این فایل")
    ap.add_argument("--json", dest="json_out", help="ذخیره‌ی خروجی JSON در این فایل")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"❌ دیتابیس پیدا نشد: {args.db}", file=sys.stderr)
        sys.exit(2)
    conn = sqlite3.connect(args.db)
    try:
        latest = conn.execute("SELECT MAX(timestamp) FROM toner_snapshots_v2").fetchone()[0]
        t0, t1 = _window_args(args, latest)

        if args.ip:
            ips = args.ip
        else:
            rows = conn.execute(
                "SELECT DISTINCT printer_ip FROM toner_snapshots_v2 "
                "UNION SELECT DISTINCT printer_ip FROM logs WHERE printer_ip IS NOT NULL"
            ).fetchall()
            ips = sorted(r[0] for r in rows if r[0])

        results = [audit_printer(conn, ip, t0, t1) for ip in ips]

        def _server_probe(a, b, excl_ips):
            q = ("SELECT COUNT(*) FROM toner_snapshots_v2 WHERE timestamp>=? AND timestamp<=?"
                 + (" AND printer_ip NOT IN (%s)" % ",".join("?" * len(excl_ips)) if excl_ips else ""))
            return conn.execute(q, [str(a), str(b), *excl_ips]).fetchone()[0] >= _SERVER_PROBE_MIN_HITS

        render_console(results, t0, t1, server_probe=_server_probe)

        if args.md:
            with open(args.md, "w", encoding="utf-8") as f:
                f.write(render_markdown(results, t0, t1, server_probe=_server_probe))
            print(f"\n💾 گزارش Markdown: {args.md}")
    finally:
        conn.close()
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        print(f"💾 خروجی JSON: {args.json_out}")

    # کد خروج: ۰ اگر همه سازگار، ۱ اگر حداقل یک ناسازگاری جدی هست
    sys.exit(1 if any(r["ok"] is False for r in results) else 0)


if __name__ == "__main__":
    main()
