"""
نقشه OIDهای اختصاصی Toshiba e-STUDIO
"""

OID_BASE = "1.3.6.1.4.1.1129.2.3.50"

OIDS = {
    "model":               f"{OID_BASE}.1.2.3.1.3.1.1",
    "serial":              f"{OID_BASE}.1.2.4.1.8.1.1",
    "firmware":            f"{OID_BASE}.1.2.3.1.4.1.1",
    "uptime":              f"{OID_BASE}.1.2.3.1.11.1.1",
    "print_total":         f"{OID_BASE}.1.3.21.6.1.2.1.4",
    "print_fc":            f"{OID_BASE}.1.3.21.6.1.2.1.1",
    "print_bw":            f"{OID_BASE}.1.3.21.6.1.2.1.3",
    "print_printer_fc":    f"{OID_BASE}.1.3.21.6.1.3.1.1",
    "print_printer_bw":    f"{OID_BASE}.1.3.21.6.1.3.1.3",
    "print_copy_fc":       f"{OID_BASE}.1.3.21.6.1.4.1.1",
    "print_copy_bw":       f"{OID_BASE}.1.3.21.6.1.4.1.3",
    "print_twin":          f"{OID_BASE}.1.3.21.6.1.2.1.2",
    "print_fax":           f"{OID_BASE}.1.3.21.6.1.7.1.4",
    "print_list":          f"{OID_BASE}.1.3.21.6.1.11.1.4",
    # ─── شمارنده‌های سایز کاغذ Toshiba ───────────────────────────────────
    # هشدار: این OIDها به نام‌های کاغذ نیستند! معنی واقعی:
    # branch 207 (a3_*): کل چاپ روی کاغذ بزرگ (ALL large paper: A3+B4+...)
    # branch 208 (a4_*): کل چاپ روی کاغذ کوچک (ALL small paper: A4+A4R+A5+...)
    # branch 209 (a4r_*): زیر-شمارنده Printer job روی کاغذ بزرگ
    # branch 210 (a5_*): زیر-شمارنده Printer job روی کاغذ کوچک
    # branch 227 (b4_*): زیر-شمارنده ناشناخته (در total_raw محاسبه نشود)
    # رابطه: a4_total + a3_total == print_total (تأیید شده)
    "a4_total":            f"{OID_BASE}.1.3.21.6.1.208.1.4",  # ALL small paper
    "a4_fc":               f"{OID_BASE}.1.3.21.6.1.208.1.1",
    "a4_bw":               f"{OID_BASE}.1.3.21.6.1.208.1.3",
    "a3_total":            f"{OID_BASE}.1.3.21.6.1.207.1.4",  # ALL large paper
    "a3_fc":               f"{OID_BASE}.1.3.21.6.1.207.1.1",
    "a3_bw":               f"{OID_BASE}.1.3.21.6.1.207.1.3",
    "a4r_total":           f"{OID_BASE}.1.3.21.6.1.209.1.4",  # Printer(large)
    "a4r_fc":              f"{OID_BASE}.1.3.21.6.1.209.1.1",
    "a4r_bw":              f"{OID_BASE}.1.3.21.6.1.209.1.3",
    "a5_total":            f"{OID_BASE}.1.3.21.6.1.210.1.4",  # Printer(small)
    "a5_fc":               f"{OID_BASE}.1.3.21.6.1.210.1.1",
    "a5_bw":               f"{OID_BASE}.1.3.21.6.1.210.1.3",
    "b4_total":            f"{OID_BASE}.1.3.21.6.1.227.1.4",  # Unknown sub-counter
    "b4_fc":               f"{OID_BASE}.1.3.21.6.1.227.1.1",
    "b4_bw":               f"{OID_BASE}.1.3.21.6.1.227.1.3",
    "scan_fc":             f"{OID_BASE}.1.3.21.6.1.9.1.1",
    "scan_bw":             f"{OID_BASE}.1.3.21.6.1.9.1.3",
    "scan_net_fc":         f"{OID_BASE}.1.3.21.6.1.8.1.1",
    "scan_net_bw":         f"{OID_BASE}.1.3.21.6.1.8.1.3",
    "tray1_level":         f"{OID_BASE}.1.3.5.1.1.5.1.1",
    "tray2_level":         f"{OID_BASE}.1.3.5.1.1.5.1.2",
    "tray3_level":         f"{OID_BASE}.1.3.5.1.1.5.1.3",
    "tray6_level":         f"{OID_BASE}.1.3.5.1.1.5.1.6",
    "tray1_size":          f"{OID_BASE}.1.3.5.1.1.4.1.1",
    "tray2_size":          f"{OID_BASE}.1.3.5.1.1.4.1.2",
    "tray3_size":          f"{OID_BASE}.1.3.5.1.1.4.1.3",
    "tray6_size":          f"{OID_BASE}.1.3.5.1.1.4.1.6",
    "tray1_status":        f"{OID_BASE}.1.3.5.1.1.3.1.1",
    "tray2_status":        f"{OID_BASE}.1.3.5.1.1.3.1.2",
    "tray3_status":        f"{OID_BASE}.1.3.5.1.1.3.1.3",
    "tray6_status":        f"{OID_BASE}.1.3.5.1.1.3.1.6",
    "toner_cyan_usage":    f"{OID_BASE}.1.3.21.8.1.2.1.1",
    "toner_magenta_usage": f"{OID_BASE}.1.3.21.8.1.2.1.2",
    "toner_yellow_usage":  f"{OID_BASE}.1.3.21.8.1.2.1.3",
    "toner_black_usage":   f"{OID_BASE}.1.3.21.8.1.2.1.4",
    "toner_magenta_status": f"{OID_BASE}.1.3.21.8.1.5.1.1",
    "toner_cyan_status":    f"{OID_BASE}.1.3.21.8.1.5.1.2",
    "toner_yellow_status":  f"{OID_BASE}.1.3.21.8.1.5.1.3",
    "toner_black_status":   f"{OID_BASE}.1.3.21.8.1.5.1.4",
    "alert1_msg":          f"{OID_BASE}.1.3.15.2.1.8.1.1",
    "alert2_msg":          f"{OID_BASE}.1.3.15.2.1.8.1.2",
    "alert3_msg":          f"{OID_BASE}.1.3.15.2.1.8.1.3",
    "alert1_code":         f"{OID_BASE}.1.3.15.2.1.7.1.1",
    "alert2_code":         f"{OID_BASE}.1.3.15.2.1.7.1.2",
    "alert3_code":         f"{OID_BASE}.1.3.15.2.1.7.1.3",
}

PAPER_SIZE_MAP = {
    3:"A4", 4:"B5", 7:"A5", 8:"A4R", 14:"A5R",
    37:"A3", 38:"B4", 10:"A3R", 15:"B4R",
    1:"Letter", 2:"Legal",
}

TONER_STATUS = {3:"ok", 4:"ok", 5:"low", 6:"low", 7:"critical", 8:"empty", 9:"critical"}
TONER_LEVEL  = {3:85,   4:70,   5:30,   6:20,   7:10,          8:0,      9:5}