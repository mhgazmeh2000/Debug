# 🖨️ Multi‑Brand Printer Monitor

> 🇬🇧 **English version:** [README.en.md](README.en.md)
> 🗺️ **نمودار تعاملی معماری و جریان داده:** [docs/architecture.html](docs/architecture.html) (با خروجی XML برای draw.io)

سامانه‌ی پایش و مانیتورینگ پرینترهای **Toshiba، HP، Canon، Brother** و سنسورهای **ECS100G** با استفاده از **SNMP (v1/v2c) + HTTP**.

این پروژه برای نمایش وضعیت آنلاین/آفلاین، شمارنده‌های چاپ، تونر، سینی‌ها، رویدادها، گزارش‌گیری، گروه‌بندی پرینترها و مدیریت دسترسی کاربران طراحی شده است.

---

## 📑 فهرست مطالب

1. [قابلیت‌ها](#-قابلیت‌ها)
2. [معماری کلی](#-معماری-کلی-پروژه)
3. [جریان داده (چطور اطلاعات دریافت می‌شود)](#-جریان-داده)
4. [ساختار پروژه و توضیح فایل‌ها](#-ساختار-پروژه)
5. [پیش‌نیازها و نصب](#-پینیازها)
6. [تنظیمات محیطی](#-تنظیمات-مهم-محیطی)
7. [APIها](#-apiهای-مهم)
8. [تونر و Yield Engine](#-نکات-مربوط-به-تونر-و-مواد-مصرفی)
9. [صحت لاگ‌ها و شمارنده‌ها](#-صحت-لاگها-و-شمارندهها)
10. [ابزارها](#-ابزارها)
11. [امنیت](#-امنیت)

---

## ✨ قابلیت‌ها

### 🖨️ پشتیبانی از چند برند
- **Toshiba e‑STUDIO** (شامل شمارنده‌های vendor-specific، A3/A4، سینی‌ها، تونر از SNMP یا TopAccess)
- **HP LaserJet / FutureSmart / JetDirect** (شامل M527 و E52645 با درصدهای NPCL)
- **Canon i‑SENSYS / MF / LBP** (شامل LBP233dw با کدهای وضعیت گسسته تونر 0/5/7)
- **Brother MFC / NC series**
- **ECS100G** برای دما/رطوبت

### 📊 مانیتورینگ دستگاه
- تشخیص **آنلاین / آفلاین** با مکانیزم **حداقل ۲ چرخه شکست متوالی** تا قطعی لحظه‌ای شبکه باعث آفلاین کاذب نشود
- نمایش **مدل، سریال، firmware، uptime**
- شمارنده‌های چاپ: کل / رنگی / سیاه‌وسفید / کپی / پرینت / فکس / اسکن
- **تفکیک اندازه کاغذ (A3/A4)** با چرخه‌ی تطبیق Snapshotها (راستی‌آزمایی‌شده با شبیه‌سازی)
- نمایش **سینی‌های کاغذ** و سطح آن‌ها
- نمایش **تونر / کارتریج / درام** در قالب کارت‌های یکپارچه
- نمایش **هشدارهای فعال**

### 🎨 تونر و مواد مصرفی
- خواندن سطح تونر از **SNMP** و در برخی برندها **HTTP scraping** (Toshiba TopAccess / Canon Remote UI)
- پشتیبانی از واحدهای RFC 3805: درصد (`unit=19`)، کدهای گسسته Canon (`0/5/7`)، sentinel های `-2/-3`
- پشتیبانی از **manual toner reset** و محاسبه **pages since last reset**
- **Yield Engine** جدید (`core/yield_engine.py`) با:
  - یادگیری خودکار `yield_per_page`
  - روش **anchor-based learning** برای پرینترهای کم‌مصرف
  - سطح اعتماد `low` / `medium` / `high`
  - اشتراک yield بین کارتریج‌های هم‌مدل/هم‌نام
  - تاریخچه snapshotها و نمونه‌های یادگیری
- **کاتالوگ محلی کارتریج** (`cartridge_yield_catalog.json` — بیش از ۳۰ مدل) به‌جای fallback خام ۲۰۰۰ صفحه
- اولویت منابع ظرفیت: `auto_learn` → `shared_profile` → `device_capacity` → `catalog` → `default`
- نمایش **Dot Count / Mega Dots** در مدل‌هایی که واقعاً داده ارائه می‌کنند

### 📜 رویدادها و لاگ‌ها
- ثبت خودکار رویدادهای `PRINT` / `STATUS` / `ALERT` / `REFILL` / `SERVICE`
- `SENSOR_CHANGE` برای تغییر معنی‌دار سنسورها (دما ≥ ۱°C، رطوبت ≥ ۵٪)
- ثبت `paper_size` (`Large (A3/B4)` / `Small (A4/A5)` / `Mixed`) و `paper_split` در جزئیات لاگ
- ثبت `poll_timestamp` در رویدادهای `PRINT`
- **محافظت در برابر لاگ‌های غلط:** بی‌توجهی به اولین poll، تشخیص `COUNTER_RESET`، `COUNTER_DROP`، `PRINT_OVERFLOW` و `COUNTER_ANOMALY` (جزئیات در بخش [صحت لاگ‌ها](#-صحت-لاگها-و-شمارندهها))

### 👥 گروه‌بندی پرینترها
- ساخت/تغییرنام/حذف **گروه‌های سفارشی** با آیکون و رنگ (`groups.json`)
- گروه حتی بدون پرینتر هم باقی می‌ماند؛ `id` پایدار جدا از نام نمایشی
- گروه‌های پیش‌فرض بر اساس **subnet دفاتر**

### 📈 گزارش و خروجی
- نمودار مصرف روزانه (Chart.js)
- خروجی **Excel** (شیت‌های Printer Status / Job Log / Reset History)
- خروجی **CSV** و **JSON**
- **Import دیتابیس** با تحلیل، انتخاب بخش‌ها، فیلتر IP/تاریخ و backup خودکار

### 👤 کاربران و امنیت
- احراز هویت با **username/password** (bcrypt) و **Google OAuth**
- نقش‌ها: `admin` / `manager` / `viewer`
- محدودسازی بر اساس **دفاتر مجاز** و **ماژول‌های مجاز**
- **CSRF protection**، **rate limiting**، reCAPTCHA، **security audit log**
- صفحه مدیریت کاربران و داشبورد امنیت

### 🧩 رابط کاربری
- تم **دارک / لایت**، چیدمان responsive، راست‌به‌چپ فارسی
- Drag & Drop برای مرتب‌سازی کارت‌ها (Sortable.js)
- Tooltip ،Toast notification و آکاردئون برای شمارنده‌های تکمیلی

---

## 🧱 معماری کلی پروژه

| لایه | تکنولوژی / ماژول |
|---|---|
| رابط کاربری | HTML/CSS/JS خالص + Chart.js + Sortable.js (`web/templates`, `web/static`) |
| وب‌سرور و API | Flask + Blueprintها (`web/`)، پورت پیش‌فرض **۵۰۵۳** |
| منطق هسته | Poller + Collectorها + Yield Engine (`core/`) |
| پروتکل دستگاه | SNMP v1/v2c پیاده‌سازی داخلی روی **UDP/161** (`core/snmp/`) + HTTP scraping |
| ذخیره‌سازی | SQLite (`logs.db`) + فایل‌های JSON (`printers.json`, `groups.json`, `oid_profiles.json`) |

🗺️ برای دیدن **فلوچارت کامل و تعاملی** ارتباط فایل‌ها و جریان داده (با خروجی XML برای draw.io)، فایل **[docs/architecture.html](docs/architecture.html)** را در مرورگر باز کنید.

---

## 🔄 جریان داده

### چرخه‌ی Polling (هر ۶۰ ثانیه — `POLL_INTERVAL`)

```text
run.py ──▶ polling_loop (thread)
   │
   ├─▶ core/poller.py : poll_all()
   │      │  لیست پرینترها از core/store.py (printers.json)
   │      ▼
   │   collect(ip) ──▶ مسیر اصلی: core/enhanced_collector.py
   │      │                (Generic MIB-2 + Host Resources + Printer MIB + Vendor OIDs)
   │      │            اگر ناقص/ناموفق ─▶ fallback به collector برند:
   │      │                toshiba.py / hp.py / canon.py / brother.py
   │      │                (+ در صورت نیاز HTTP scraping تونر)
   │      ▼
   │   core/snmp/protocol.py ──▶ SNMP GET روی UDP/161
   │      │                    (v2c اول، fallback به v1؛ cache نسخه هر IP)
   │      ▼
   │   core/collectors/base.py ──▶ پردازش:
   │      │   • _counters_event: تشخیص چاپ واقعی vs ریست/ناهنجاری
   │      │   • attribute_paper_size: تفکیک A3/A4 با چرخه تطبیق
   │      │   • محاسبات تونر + Yield Engine (yield_engine.py + کاتالوگ)
   │      ▼
   ├─▶ core/store.py  ──▷ حافظه زنده (printer_data) برای APIها
   └─▶ core/database.py ──▷ add_event(...) به logs.db
            │
            ▼
   مرورگر کاربر ──▶ dashboard.js هر چند ثانیه GET /api/printers
            └──── Web routes از store/database می‌خوانند و JSON برمی‌گردانند
```

### Threadهای پس‌زمینه (`run.py`)
| Thread | کار |
|---|---|
| `poll-init` / `poll-loop` | اولین poll فوری + حلقه‌ی polling هر ۶۰ ثانیه |
| `weekly-scan` | اسکن هفتگی OIDها و به‌روزرسانی `oid_profiles.json` |
| `cleanup-loop` | حذف لاگ‌های قدیمی `PRINT` (prune) |
| Flask (thread اصلی) | سرو کردن وب روی `0.0.0.0:5050` |

### مسیرهای جریان داده‌ی دیگر
- **سنسور ECS100G** → `core/collectors/sensor.py` → جدول `sensor_readings` + رویداد `SENSOR_CHANGE`
- **اسکن OID** → `core/oid/scanner.py` → `oid_profiles.json` + `device_classifier.py`
- **اکشن‌های کاربر** (افزودن پرینتر، toner reset، گروه‌ها، مدیریت کاربران) → routeهای `web/routes/` → `store`/`database`
- **گزارش‌گیری** → `web/routes/export_bp.py` → Excel/CSV از `logs.db`

---

## 📁 ساختار پروژه

```text
Multi-Printer/
├── run.py                      # نقطه ورود: Flask + threadهای polling/scan/cleanup
├── models.py                   # مدل User (Flask-Login) + bcrypt
├── utils.py                    # ایمیل، توکن reset، reCAPTCHA
├── create_admin.py             # ساخت/ارتقای ادمین از CLI
├── requirements.txt            # وابستگی‌ها
├── printers.example.json       # نمونه فایل پرینترها
├── cartridge_yield_catalog.json# کاتالوگ ظرفیت کارتریج‌ها (+ مقادیر local/iran)
├── start.bat / stop-project.bat# اجرا/توقف در ویندوز
├── config/
│   ├── __init__.py
│   └── settings.py             # پورت، مسیر فایل‌ها، subnet دفاتر، SECRET_KEY، آستانه‌ها
├── core/
│   ├── database.py             # لایه SQLite: لاگ‌ها، کاربران، کانترها، toner/reset history
│   ├── poller.py               # حلقه polling، انتخاب collector، مدیریت offline/online
│   ├── store.py                # حافظه زنده (printer_data/PRINTERS) + PrevStore
│   ├── enhanced_collector.py   # مسیر اصلی جمع‌آوری (همه برندها): شمارنده/تونر/سینی
│   ├── yield_engine.py         # یادگیری yield + کاتالوگ کارتریج + گزارش وضعیت
│   ├── groups.py               # گروه‌های سفارشی پرینتر (groups.json)
│   ├── device_classifier.py    # طبقه‌بندی دستگاه از روی OIDهای اسکن‌شده
│   ├── security_audit.py       # ثبت/بازیابی رویدادهای امنیتی
│   ├── collectors/
│   │   ├── base.py             # توابع مشترک: رویداد چاپ، تونر، اختصاص A3/A4، اعتبارسنجی
│   │   ├── base_enhanced.py    # bridge به enhanced_collector
│   │   ├── toshiba.py          # Toshiba: OIDهای اختصاصی، A3/A4، twin، scrape تونر، سینی
│   │   ├── hp.py               # HP: شمارنده/تونر + fallback
│   │   ├── canon.py            # Canon: MF/LBP + scrape Remote UI برای تونر
│   │   ├── brother.py          # Brother: تونر/درام + fallback
│   │   └── sensor.py           # سنسور ECS100G (دما/رطوبت)
│   ├── snmp/
│   │   ├── protocol.py         # SNMP v1/v2c low-level روی UDP/161 + fallback نسخه + cache
│   │   └── oid_map.py          # نگاشت OIDها (خصوصاً Toshiba) و mapهای کاغذ/تونر
│   └── oid/
│       ├── scanner.py          # اسکن OID هر IP و ساخت profile (startup + هفتگی)
│       ├── catalog.py          # کاتالوگ OIDهای شناخته‌شده
│       └── validator.py        # اعتبارسنجی مقادیر OID + لاگ خطاها
├── web/
│   ├── __init__.py             # create_app: ثبت blueprintها، auth، security، CORS
│   ├── auth.py                 # login/register/OAuth/reset + دکوریتورهای نقش و دسترسی
│   ├── security.py             # CSRF، rate limiting، security headers
│   ├── routes/
│   │   ├── dashboard.py        # صفحه اصلی
│   │   ├── printers.py         # CRUD پرینترها + toner reset + debug
│   │   ├── logs.py             # API لاگ‌ها و رویداد دستی
│   │   ├── export_bp.py        # خروجی Excel/CSV
│   │   ├── discover.py         # کشف دستگاه‌ها در شبکه
│   │   ├── scan.py             # API اسکن/اعتبارسنجی OID
│   │   ├── stats.py            # آمار روزانه و داده نمودار
│   │   ├── system.py           # وضعیت سیستم + poll دستی
│   │   ├── security.py         # داشبورد امنیت + API رویدادها
│   │   ├── users.py            # مدیریت کاربران و دسترسی‌ها
│   │   ├── groups.py           # API گروه‌های سفارشی
│   │   ├── import_db.py        # تحلیل و import دیتابیس با backup
│   │   ├── validation.py       # سازگاری کانترها و profileها
│   │   └── yield_status.py     # وضعیت و گزارش Yield Engine
│   ├── templates/              # base/dashboard/login/register/users/security/...
│   └── static/
│       ├── css/style.css
│       └── js/                 # dashboard.js (منطق اصلی UI)، legacy-mode.js،
│                               # Sortable.min.js، chart.umd.min.js
├── tools/
│   └── diagnose_printer.py     # دامپ خام SNMP/Web یک پرینتر برای عیب‌یابی مدل جدید
└── docs/
    └── architecture.html       # فلوچارت تعاملی معماری + جریان داده + خروجی XML
```

### فایل‌های runtime (در git نیستند)
`logs.db` ،`printers.json` ،`groups.json` ،`oid_profiles.json` ،`oid_validation_errors.txt` ،`toner_report.txt` ،`yield_status_report.txt`

---

## 🔧 پیش‌نیازها

- Python **3.8+** (پیشنهاد: **3.10+**)
- دسترسی شبکه به پرینترها و باز بودن **161/UDP** (SNMP v1 یا v2c)
- برای Excel: `openpyxl` (در requirements هست)

## 🚀 نصب و اجرا

```bash
git clone https://github.com/mhgazmeh2000/multi-printer.git
cd multi-printer
python -m venv .venv
# Linux/macOS:  source .venv/bin/activate
# Windows:      .venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

پیش‌فرض: `http://localhost:5050`
در ویندوز می‌توانید از `start.bat` و `stop-project.bat` هم استفاده کنید.

اگر هیچ کاربری وجود نداشته باشد، **اولین ثبت‌نام** خودکار `admin` و `verified` می‌شود؛ یا با `python create_admin.py` ادمین بسازید.

---

## ⚙️ تنظیمات مهم محیطی

### امنیت و اجرا
```bash
export ENVIRONMENT=development
export SECRET_KEY=change-me
export FLASK_PORT=5050
```

### پرینترهای پیش‌فرض (اختیاری)
```bash
export DEFAULT_PRINTERS_JSON='[
  {"ip":"192.168.1.10","name":"Printer #1","community":"public"}
]'
```

### subnet دفاتر
```bash
export OFFICE_SUBNET_IMAMAT=172.16.25
export OFFICE_SUBNET_SOROUSH=172.16.24
export OFFICE_SUBNET_FALESTIN=172.16.0
export OFFICE_SUBNET_ELAHIYE=172.16.32
```

### سایر
```bash
export ASSET_VERSION=20260609-2          # cache busting برای assetها
export CORS_ALLOWED_ORIGINS="https://app.example.com"
# ایمیل (برای reset password) و OAuth و reCAPTCHA — در config/settings.py
```

---

## 🔌 APIهای مهم

### وضعیت سیستم
- `GET /api/status`
- `POST /api/poll/now`

### پرینترها
- `GET /api/printers` · `GET /api/printer/<ip>`
- `POST /api/printers/add` · `POST /api/printers/bulk-add` · `POST /api/printers/remove`
- `POST /api/printer/<ip>/update` (ویرایش نام/گروه/…)
- `POST /api/printer/<ip>/toner_reset`
- `POST /api/printers/discover` · `POST /api/discover/auto-add`

### لاگ‌ها
- `GET /api/logs/all` · `GET /api/printer/<ip>/log`
- `POST /api/logs/clear` · `POST /api/events/manual`

### گروه‌ها
- `GET/POST /api/groups`
- `POST /api/groups/<gid>/rename` · `DELETE /api/groups/<gid>`

### Import دیتابیس
- `POST /api/import/analyze` · `POST /api/import/confirm`

علاوه بر لاگ‌ها، `printer_counters`، `toner_history`، `sensor_readings` و جدول‌های Yield Engine هم قابل import هستند؛ با انتخاب بخش‌ها، فیلتر IP/تاریخ، مدیریت تکراری‌ها و backup خودکار.

### خروجی‌ها
- `GET /api/export/excel`
- `GET /api/export/logs?format=csv` / `?format=excel`

### اسکن و اعتبارسنجی
- `POST /api/scan/oids` · `GET /api/scan/oids/<ip>` · `POST /api/scan/all` · `GET /api/scan/profiles`
- `GET /api/validate/counters` · `GET /api/validate/oids/<ip>`

### Yield Engine
- `GET /api/yield/status`
- `POST /api/yield/report`

### آمار
- `GET /api/stats/daily` · `GET /api/stats/sensor/daily`

### کاربران و امنیت
- `GET /users` · `GET/POST /api/users`
- `POST /api/users/<id>/role` · `.../verify` · `.../access` · `DELETE /api/users/<id>`
- `GET /security` · `GET /api/security/events` · `GET /api/security/stats`

---

## 📊 نکات مربوط به تونر و مواد مصرفی

### کاتالوگ ظرفیت کارتریج
`cartridge_yield_catalog.json` وقتی هنوز `auto_learn` یا `device_capacity` وجود ندارد، به‌جای fallback خام `2000` مقدار واقع‌بینانه‌تری می‌دهد.

اولویت: **auto_learn → shared_profile → device_capacity → catalog → default**

```bash
export CARTRIDGE_YIELD_CATALOG=/path/to/cartridge_yield_catalog.json
# حالت ظرفیت (پیش‌فرض: local = مناسب بازار ایران/شارژی+طرح)
set CARTRIDGE_YIELD_MODE=local        # Windows
export CARTRIDGE_YIELD_MODE=local     # Linux/macOS  (oem | compatible | refill | local)
export CARTRIDGE_YIELD_FACTOR=0.90    # ضریب اختیاری محافظه‌کارانه
```

هر entry می‌تواند چهار مقدار `yield_per_page_oem/compatible/refill/local` داشته باشد.

### نکات برندها
- **Canon i‑SENSYS (مثل LBP233dw):** به‌جای درصد، کدهای گسسته `0=Empty / 5=Low / 7=OK` می‌دهد؛ سیستم آن‌ها را به‌صورت درصد «نمایشی» (۰/۱۵/۷۰) و با منبع `canon_status_code` نشان می‌دهد تا یادگیری yield خراب نشود. در صورت لزوم Remote UI هم scrape می‌شود.
- **HP FutureSmart (مثل M527 و E52645):** `MaxCapacity` منفی (‑3/‑2) است و `Remaining` خودِ **درصد** است (`unit=19`)؛ سیستم این حالت را تشخیص می‌دهد. مدل‌های کارتریج 87A/87X/87Y و 89A/89X/89Y در کاتالوگ هستند.
- **Brother:** بعضی مدل‌ها فقط «وجود تونر» را گزارش می‌کنند؛ در این حالت UI مقدار `—` نشان می‌دهد و مقدار ساختگی تولید نمی‌شود.
- **RFC 3805 sentinel `-3`** = «دستگاه مقدار دقیق نمی‌داند ولی مخزن هست» → وضعیت `unknown` نه `not_supported`.
- **Toshiba:** علاوه بر SNMP، در صورت نیاز TopAccess scrape می‌شود؛ `pages_since_last_reset` پشتیبانی می‌شود.

---

## 🧮 صحت لاگ‌ها و شمارنده‌ها

برای جلوگیری از لاگ‌های غلط/بادکرده، منطق `_counters_event` این قواعد را دارد:

| وضعیت | رفتار سیستم |
|---|---|
| اولین poll هر دستگاه | Snapshot پایه ثبت می‌شود؛ **لاگ PRINT ساخته نمی‌شود** |
| ریست شمارنده (مقادیر صفر/کاهش شدید + uptime=0) | رویداد `COUNTER_RESET`؛ بدون ثبت چاپ ساختگی |
| افت خفیف/بازنشانی نرم | `COUNTER_DROP` و بازتنظیم مبنا |
| جهش غیرواقعی (بیش از سقف فیزیکی چاپ در بازه poll) | `PRINT_OVERFLOW` رد می‌شود |
| افت خفیف بعد از reboot (صفحاتی که خود دستگاه هم فراموش کرده) | `COUNTER_ANOMALY` با جزئیات |
| چاپ عادی | رویداد `PRINT` با split دقیق A3/A4 و `poll_timestamp` |

تفکیک کاغذ با اتحاد چرخه‌ی Snapshotها انجام می‌شود و اگر شمارنده‌ی A3/A4 عقب‌مانده باشد، پرچم `a3_lagged/a4_lagged` ثبت می‌شود تا لاگ صادقانه بماند.

---

## 🛠️ ابزارها

### ابزار عیب‌یابی پرینتر
```bash
python tools/diagnose_printer.py <IP>
```
دامپ خام SNMP (سیستم، شمارنده‌ها، supplies) + صفحات وب دستگاه را در `diagnose_<ip>.md` ذخیره می‌کند تا پشتیبانی از مدل‌های جدید دقیق اضافه شود.

### مسیرهای debug
- `GET /api/debug/printer/<ip>`
- `GET /api/debug/brother-toner/<ip>`
- `GET /api/debug/toshiba-snmp/<ip>`

---

## 🛡️ امنیت

- احراز هویت **bcrypt** + Flask-Login؛ نقش‌های `admin/manager/viewer`
- **CSRF** روی همه‌ی درخواست‌های state-changing (در frontend با `apiFetch`)
- **Rate limiting**، security headers، CORS محدودشونده از ENV
- **Security audit log** رویدادهای ورود/خروج/ناموفق/مشکوک در `/security`
- `SECRET_KEY` فقط از ENV؛ در production بدون مقدار امن اجرا نمی‌شود (`config/settings.py`)
- فایل‌های runtime حساس (دیتابیس، لیست پرینترها) در `.gitignore` هستند
- نسخه‌ی فعلی مخزن پس از بازبینی امنیتی **پاک‌سازی شده** (حذف فایل‌های خارجی/مشکوک و داده‌های حساس از تاریخچه)

> ⚠️ قبل از استقرار: `SECRET_KEY` قوی تنظیم کنید، رمز ادمین پیش‌فرض را عوض کنید و در صورت عمومی بودن سرویس، HTTPS بگذارید.

---

## 📈 خروجی Excel

شیت‌ها: `Printer Status` ،`Job Log` ،`Reset History`

شیت **Reset History** شامل: زمان reset، IP/نام دستگاه، رنگ کارتریج، درصد تنظیم‌شده، total pages لحظه‌ی reset، صفحات بعد از reset و `yield_per_page`.

---

## ⚠️ نکات اجرایی

- assetها با `ASSET_VERSION` versioned می‌شوند (cache busting)
- در توسعه، endpointهای `/api/debug/*` فقط برای دیباگ داخلی‌اند
- فایل‌های runtime را commit نکنید (در `.gitignore` هستند)

---

## 📄 مجوز / License

در صورت نیاز، این بخش را با مجوز مدنظر پروژه تکمیل کنید.
