import os
import tempfile
import unittest

from core.yield_engine import (
    DEFAULT_YIELD_PER_PAGE,
    get_yield_status,
    process_cartridge_snapshot,
    register_manual_refill,
)


class YieldEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        self.old_catalog_env = os.environ.get("CARTRIDGE_YIELD_CATALOG")
        os.chdir(self.tmp.name)
        self.catalog_path = os.path.join(self.tmp.name, "catalog.json")
        with open(self.catalog_path, "w", encoding="utf-8") as f:
            f.write('{"entries":[{"id":"test-catalog-toner","color":"black","yield_per_page":4100,"match":{"color":"black","cartridge_contains":["catalog toner x"]}}]}')
        os.environ["CARTRIDGE_YIELD_CATALOG"] = self.catalog_path

    def tearDown(self):
        if self.old_catalog_env is None:
            os.environ.pop("CARTRIDGE_YIELD_CATALOG", None)
        else:
            os.environ["CARTRIDGE_YIELD_CATALOG"] = self.old_catalog_env
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def test_low_usage_anchor_accumulates_until_toner_drops(self):
        # Anchor at 80%, then many low-page polls with no toner change.
        # ✅ کامیت ۱۷: قانون کوانتایزیشن (≥150 صفحه به‌ازای هر ۱٪ افت) — پس
        # سناریو با مقیاس واقعی (≥200 صفحه/پالس) اجرا می‌شود؛ ورودی‌های کوچک‌تر
        # عمداً رد می‌شوند تا سمپل‌های هذیانی (yield=320/394 در Debug واقعی)
        # ساخته نشوند.
        process_cartridge_snapshot(
            ip="10.0.0.1", color="black", printer_model="HP X", cartridge_name="CF283A",
            level=80, counters={"total": 1000}, device_type="mono", timestamp="2026-01-01T00:00:00",
        )
        for total in (1200, 1400, 1600, 1800):
            meta = process_cartridge_snapshot(
                ip="10.0.0.1", color="black", printer_model="HP X", cartridge_name="CF283A",
                level=80, counters={"total": total}, device_type="mono", timestamp="2026-01-02T00:00:00",
            )
            self.assertEqual(meta["yield_per_page"], DEFAULT_YIELD_PER_PAGE)

        # When level finally drops, pages are calculated from the original anchor, not last poll.
        meta = process_cartridge_snapshot(
            ip="10.0.0.1", color="black", printer_model="HP X", cartridge_name="CF283A",
            level=79, counters={"total": 1950}, device_type="mono", timestamp="2026-01-03T00:00:00",
        )
        # 950 صفحه از anchor با ۱٪ افت → 95000 (در بازه‌ی مجاز 300..100000)
        self.assertEqual(meta["yield_per_page"], 95000)
        self.assertEqual(meta["yield_source"], "auto_learn")

    def test_high_confidence_profile_is_shared_by_model_and_cartridge_name(self):
        ip1 = "10.0.0.10"
        model = "Canon MF Test"
        cartridge = "Cartridge 137"
        # Existing second printer with the same model/cartridge starts as default.
        process_cartridge_snapshot(
            ip="10.0.0.11", color="black", printer_model=model, cartridge_name=cartridge,
            level=90, counters={"total": 500}, device_type="mono", timestamp="2026-01-01T00:00:00",
        )

        total = 1000
        level = 90
        process_cartridge_snapshot(
            ip=ip1, color="black", printer_model=model, cartridge_name=cartridge,
            level=level, counters={"total": total}, device_type="mono", timestamp="2026-01-01T00:00:00",
        )
        for i in range(4):
            total += 400   # ✅ کامیت ۱۷: هر سمپل باید ≥150 صفحه/۱٪ داشته باشد
            level -= 1
            meta = process_cartridge_snapshot(
                ip=ip1, color="black", printer_model=model, cartridge_name=cartridge,
                level=level, counters={"total": total}, device_type="mono", timestamp=f"2026-01-0{i+2}T00:00:00",
            )

        self.assertEqual(meta["confidence"], "high")
        self.assertEqual(meta["yield_per_page"], 40000)

        rows = get_yield_status()
        second = [r for r in rows if r["printer_ip"] == "10.0.0.11" and r["color"] == "black"][0]
        self.assertEqual(second["yield_per_page"], 40000)
        self.assertEqual(second["yield_source"], "shared_profile")
        self.assertEqual(second["confidence"], "high")

    def test_manual_refill_resets_anchor_without_losing_existing_profile(self):
        meta = process_cartridge_snapshot(
            ip="10.0.0.20", color="black", printer_model="Brother Test", cartridge_name="TN Test",
            level=60, counters={"total": 2000}, device_type="mono", timestamp="2026-01-01T00:00:00",
        )
        self.assertEqual(meta["anchor_level"], 60)

        meta = register_manual_refill(
            ip="10.0.0.20", color="black", printer_model="Brother Test", cartridge_name="TN Test",
            new_level=100, counters={"total": 2100}, device_type="mono", timestamp="2026-01-02T00:00:00",
        )
        self.assertEqual(meta["anchor_level"], 100)
        self.assertEqual(meta["anchor_counter"], 2100)
        rows = get_yield_status(ip="10.0.0.20")
        self.assertEqual(rows[0]["last_refill_at"], "2026-01-02T00:00:00")

    def test_catalog_capacity_replaces_default_when_no_better_source_exists(self):
        meta = process_cartridge_snapshot(
            ip="10.0.0.24", color="black", printer_model="Any Model", cartridge_name="Catalog Toner X",
            level=80, counters={"total": 1000}, device_type="mono", timestamp="2026-01-01T00:00:00",
        )
        self.assertEqual(meta["yield_per_page"], 4100)
        self.assertEqual(meta["yield_source"], "catalog")
        self.assertEqual(meta["confidence"], "medium")

    def test_device_reported_capacity_replaces_default_until_auto_learn(self):
        # ✅ کامیت ۱۷: با device_capacity بزرگ (کوپی‌ها) سمپل‌های ≥150ص/۱٪ قابل
        # یادگیری‌اند؛ یک سمپل با اعتماد کم baseline را override نمی‌کند ولی با
        # سمپل دوم (confidence متوسط) auto_learn جایگزین می‌شود.
        process_cartridge_snapshot(
            ip="10.0.0.25", color="black", printer_model="HP Capacity", cartridge_name="Device Toner",
            level=75, counters={"total": 1000}, device_type="mono", device_capacity_pages=32000,
            timestamp="2026-01-01T00:00:00",
        )
        # یک sample با اعتماد کم نباید baseline بهتر مثل device_capacity را override کند.
        meta = process_cartridge_snapshot(
            ip="10.0.0.25", color="black", printer_model="HP Capacity", cartridge_name="Device Toner",
            level=74, counters={"total": 1300}, device_type="mono", device_capacity_pages=32000,
            timestamp="2026-01-02T00:00:00",
        )
        self.assertEqual(meta["yield_per_page"], 32000)
        self.assertEqual(meta["yield_source"], "device_capacity")

        # با sample کافی و confidence متوسط، auto_learn می‌تواند جایگزین شود.
        meta = process_cartridge_snapshot(
            ip="10.0.0.25", color="black", printer_model="HP Capacity", cartridge_name="Device Toner",
            level=73, counters={"total": 1600}, device_type="mono", device_capacity_pages=32000,
            timestamp="2026-01-03T00:00:00",
        )
        self.assertEqual(meta["yield_source"], "auto_learn")
        self.assertEqual(meta["confidence"], "medium")
        self.assertEqual(meta["yield_per_page"], 30000)

    def test_device_capacity_protected_from_weak_samples(self):
        # ✅ جدید (کامیت ۱۷): کارتریج کوچک (device_capacity=3200) ذاتاً هیچ‌وقت
        # سمپل ≥150ص/۱٪ نمی‌دهد؛ همه به‌عنوان «نویز کوانتایزیشن ۱٪» رد می‌شوند
        # و ظرفیت دستگاه سم‌پاشی نمی‌شود. این همان باگ yield=1901 مسموم روی
        # e-STUDIO306 بود (لاگ واقعی Debug).
        process_cartridge_snapshot(
            ip="10.0.0.26", color="black", printer_model="HP Small", cartridge_name="Small Toner",
            level=75, counters={"total": 1000}, device_type="mono", device_capacity_pages=3200,
            timestamp="2026-01-01T00:00:00",
        )
        for i, total in enumerate((1050, 1100, 1150)):
            meta = process_cartridge_snapshot(
                ip="10.0.0.26", color="black", printer_model="HP Small", cartridge_name="Small Toner",
                level=74 - i, counters={"total": total}, device_type="mono",
                device_capacity_pages=3200, timestamp=f"2026-01-0{i+2}T00:00:00",
            )
        self.assertEqual(meta["yield_per_page"], 3200)
        self.assertEqual(meta["yield_source"], "device_capacity")

    def test_counter_decrease_resets_anchor_and_does_not_learn_bad_sample(self):
        process_cartridge_snapshot(
            ip="10.0.0.30", color="black", printer_model="HP Reset", cartridge_name="Toner",
            level=70, counters={"total": 5000}, device_type="mono", timestamp="2026-01-01T00:00:00",
        )
        meta = process_cartridge_snapshot(
            ip="10.0.0.30", color="black", printer_model="HP Reset", cartridge_name="Toner",
            level=69, counters={"total": 100}, device_type="mono", timestamp="2026-01-02T00:00:00",
        )
        self.assertEqual(meta["yield_per_page"], DEFAULT_YIELD_PER_PAGE)
        self.assertEqual(meta["anchor_counter"], 100)
        self.assertEqual(meta["anchor_level"], 69)

    def test_zero_plateau_can_reach_high_after_next_refill(self):
        # شروع چرخه با reset/شارژ 100٪
        register_manual_refill(
            ip="10.0.0.35", color="black", printer_model="Zero Test", cartridge_name="ZT",
            new_level=100, counters={"total": 1000}, device_type="mono", timestamp="2026-01-01T00:00:00",
        )
        meta = process_cartridge_snapshot(
            ip="10.0.0.35", color="black", printer_model="Zero Test", cartridge_name="ZT",
            level=0, counters={"total": 5200}, device_type="mono", timestamp="2026-01-10T00:00:00",
        )
        # تونر به صفر می‌رسد اما چاپ ادامه دارد؛ سیستم باید pages_after_zero را track کند.
        self.assertEqual(meta["cycle_status"], "zero_plateau")
        meta = process_cartridge_snapshot(
            ip="10.0.0.35", color="black", printer_model="Zero Test", cartridge_name="ZT",
            level=0, counters={"total": 6200}, device_type="mono", timestamp="2026-01-15T00:00:00",
        )
        self.assertEqual(meta["pages_after_zero"], 1000)
        # شارژ بعدی چرخه قبلی را می‌بندد و yield واقعی را high می‌کند.
        meta = register_manual_refill(
            ip="10.0.0.35", color="black", printer_model="Zero Test", cartridge_name="ZT",
            new_level=100, counters={"total": 6800}, device_type="mono", timestamp="2026-01-20T00:00:00",
        )
        self.assertEqual(meta["yield_source"], "cycle_learn")
        self.assertEqual(meta["confidence"], "high")
        self.assertEqual(meta["yield_per_page"], 5800)

    def test_cmy_with_full_color_counter_is_conservative_before_high_confidence(self):
        # ✅ کامیت ۱۷: مقیاس سمپل ها بالا رفت (≥150ص/۱٪) و engine قبل از high
        # محافظه‌کارانه رفتار می‌کند (conf پایین‌تر برای کانال‌های رنگی).
        process_cartridge_snapshot(
            ip="10.0.0.40", color="cyan", printer_model="Color Test", cartridge_name="C Toner",
            level=80, counters={"total": 9000, "full_color": 1000}, device_type="color", timestamp="2026-01-01T00:00:00",
        )
        total = 1000
        level = 80
        for i in range(4):
            total += 400
            level -= 1
            meta = process_cartridge_snapshot(
                ip="10.0.0.40", color="cyan", printer_model="Color Test", cartridge_name="C Toner",
                level=level, counters={"total": 9000 + total, "full_color": total}, device_type="color", timestamp=f"2026-01-0{i+2}T00:00:00",
            )
        self.assertEqual(meta["yield_per_page"], 40000)
        self.assertEqual(meta["confidence"], "medium")

    def test_toner_blip_does_not_move_anchor(self):
        # ✅ جدید (کامیت ۱۶/۱۷، از لاگ واقعی e-STUDIO306): افتِ لحظه‌ای ۱۰۰→۸٪ با
        # ۳ صفحه چاپ «نویز خوانش سطح» است؛ نه anchor جابه‌جا می‌شود نه سمپل ساخته
        # می‌شود. discriminating value: اگر blip رد شود → سمپل بعدی از anchor
        # قدیمی محاسبه می‌شود (30300)، اگر رد نشود → 30000.
        process_cartridge_snapshot(
            ip="10.0.0.60", color="black", printer_model="Toshiba Blip", cartridge_name="T-Blip",
            level=100, counters={"total": 1000}, device_type="mono", timestamp="2026-01-01T00:00:00",
        )
        meta = process_cartridge_snapshot(
            ip="10.0.0.60", color="black", printer_model="Toshiba Blip", cartridge_name="T-Blip",
            level=8, counters={"total": 1003}, device_type="mono", timestamp="2026-01-02T00:00:00",
        )
        self.assertEqual(meta["anchor_level"], 100)
        self.assertEqual(meta["anchor_counter"], 1000)

        process_cartridge_snapshot(
            ip="10.0.0.60", color="black", printer_model="Toshiba Blip", cartridge_name="T-Blip",
            level=100, counters={"total": 1003}, device_type="mono", timestamp="2026-01-03T00:00:00",
        )
        meta = process_cartridge_snapshot(
            ip="10.0.0.60", color="black", printer_model="Toshiba Blip", cartridge_name="T-Blip",
            level=99, counters={"total": 1303}, device_type="mono", timestamp="2026-01-04T00:00:00",
        )
        self.assertEqual(meta["yield_per_page"], 30300)
        self.assertEqual(meta["yield_source"], "auto_learn")


if __name__ == "__main__":
    unittest.main()
