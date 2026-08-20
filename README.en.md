# 🖨️ Multi‑Brand Printer Monitor

> 🇮🇷 **نسخه فارسی:** [README.md](README.md)
> 🗺️ **Interactive architecture & data-flow diagram:** [docs/architecture.html](docs/architecture.html) (exports to draw.io XML)

A monitoring system for **Toshiba, HP, Canon, Brother** printers and **ECS100G** environment sensors using **SNMP (v1/v2c) + HTTP**.

Built to track online/offline status, print counters, toner levels, paper trays, events/logs, reporting, printer grouping, and multi-user access control.

---

## 📑 Table of Contents

1. [Features](#-features)
2. [Architecture](#-architecture-overview)
3. [Data Flow (how data is collected)](#-data-flow)
4. [Project Structure & File Guide](#-project-structure)
5. [Prerequisites & Installation](#-prerequisites)
6. [Environment Configuration](#-environment-configuration)
7. [API Reference](#-api-reference)
8. [Toner & Yield Engine](#-toner--consumables)
9. [Log & Counter Integrity](#-log--counter-integrity)
10. [Tools](#-tools)
11. [Security](#-security)

---

## ✨ Features

### 🖨️ Multi-brand support
- **Toshiba e‑STUDIO** (vendor-specific counters, A3/A4, trays, toner via SNMP or TopAccess scraping)
- **HP LaserJet / FutureSmart / JetDirect** (incl. M527 & E52645 with NPCL percent units)
- **Canon i‑SENSYS / MF / LBP** (incl. LBP233dw with discrete toner status codes 0/5/7)
- **Brother MFC / NC series**
- **ECS100G** temperature/humidity sensors

### 📊 Device monitoring
- **Online/offline** detection with a **minimum of 2 consecutive failed cycles**, so a transient network hiccup never causes a false offline
- Model, serial, firmware, uptime
- Counters: total / color / mono / copy / print / fax / scan
- **Paper-size split (A3/A4)** using a snapshot-matching cycle
- Paper tray levels, toner/cartridge/drum cards, active alerts

### 🎨 Toner & consumables
- Toner level via **SNMP** and, on some brands, **HTTP scraping** (Toshiba TopAccess / Canon Remote UI)
- RFC 3805 supply-unit handling: percent (`unit=19`), Canon discrete codes (`0/5/7`), `-2/-3` sentinels
- **Manual toner reset** with *pages since last reset*
- **Yield Engine** (`core/yield_engine.py`):
  - Automatic `yield_per_page` learning
  - **Anchor-based learning** for low-volume printers
  - Confidence levels `low` / `medium` / `high`
  - Yield sharing across same-model/same-name cartridges
  - Snapshot & learning-sample history
- **Local cartridge catalog** (`cartridge_yield_catalog.json` — 30+ models) instead of a naive 2000-page fallback
- Capacity priority: `auto_learn` → `shared_profile` → `device_capacity` → `catalog` → `default`
- Dot Count / Mega Dots where the model genuinely provides them

### 📜 Events & logs
- Automatic `PRINT` / `STATUS` / `ALERT` / `REFILL` / `SERVICE` events
- `SENSOR_CHANGE` for meaningful sensor changes (temp ≥ 1°C, humidity ≥ 5%)
- `paper_size` (`Large (A3/B4)` / `Small (A4/A5)` / `Mixed`) and `paper_split` stored in log details
- `poll_timestamp` on `PRINT` events
- **Protection against wrong logs:** first-poll snapshot ignored, `COUNTER_RESET`, `COUNTER_DROP`, `PRINT_OVERFLOW` and `COUNTER_ANOMALY` detection — see [Log Integrity](#-log--counter-integrity)

### 👥 Printer groups
- Create/rename/delete **custom groups** with icon & color (`groups.json`)
- A group persists even with zero printers; stable `id` separate from display name
- Built-in office groups based on **subnet**

### 📈 Reporting & export
- Daily-usage charts (Chart.js)
- **Excel** export (Printer Status / Job Log / Reset History sheets)
- **CSV** and **JSON** export
- **Database import** with analysis, section selection, IP/date filters and automatic backup

### 👤 Users & security
- **Username/password** auth (bcrypt) and **Google OAuth**
- Roles: `admin` / `manager` / `viewer`
- Per-user **allowed offices** and **allowed modules**
- **CSRF protection**, **rate limiting**, reCAPTCHA, **security audit log**
- User-management page and security dashboard

### 🧩 UI
- **Dark/light** theme, responsive layout, Persian RTL
- Drag & drop card ordering (Sortable.js)
- Tooltips, toasts, and an accordion for secondary counters

---

## 🧱 Architecture Overview

| Layer | Technology / module |
|---|---|
| Frontend | Vanilla HTML/CSS/JS + Chart.js + Sortable.js (`web/templates`, `web/static`) |
| Web & API | Flask + Blueprints (`web/`), default port **5050** |
| Core logic | Poller + collectors + Yield Engine (`core/`) |
| Device protocol | In-house SNMP v1/v2c over **UDP/161** (`core/snmp/`) + HTTP scraping |
| Storage | SQLite (`logs.db`) + JSON files (`printers.json`, `groups.json`, `oid_profiles.json`) |

🗺️ For the **complete interactive flowchart** of file relationships and data flow (with draw.io XML export), open **[docs/architecture.html](docs/architecture.html)** in a browser.

---

## 🔄 Data Flow

### Polling cycle (every 60 s — `POLL_INTERVAL`)

```text
run.py ──▶ polling_loop (thread)
   │
   ├─▶ core/poller.py : poll_all()
   │      │  printer list from core/store.py (printers.json)
   │      ▼
   │   collect(ip) ──▶ primary path: core/enhanced_collector.py
   │      │                (Generic MIB-2 + Host Resources + Printer MIB + vendor OIDs)
   │      │            if incomplete/failed ─▶ brand collector fallback:
   │      │                toshiba.py / hp.py / canon.py / brother.py
   │      │                (+ HTTP toner scraping when needed)
   │      ▼
   │   core/snmp/protocol.py ──▶ SNMP GET over UDP/161
   │      │                    (v2c first, fallback to v1; per-IP version cache)
   │      ▼
   │   core/collectors/base.py ──▶ processing:
   │      │   • _counters_event: real printing vs reset/anomaly detection
   │      │   • attribute_paper_size: A3/A4 split via matching cycle
   │      │   • toner math + Yield Engine (yield_engine.py + catalog)
   │      ▼
   ├─▶ core/store.py  ──▷ live memory (printer_data) for the APIs
   └─▶ core/database.py ──▷ add_event(...) into logs.db
            │
            ▼
   User's browser ──▶ dashboard.js polls GET /api/printers every few seconds
            └──── web routes read store/database and return JSON
```

### Background threads (`run.py`)
| Thread | Purpose |
|---|---|
| `poll-init` / `poll-loop` | immediate first poll + the 60-second polling loop |
| `weekly-scan` | weekly OID scan, refreshes `oid_profiles.json` |
| `cleanup-loop` | prunes old `PRINT` logs |
| Flask (main thread) | serves the web app on `0.0.0.0:5050` |

### Other data paths
- **ECS100G sensors** → `core/collectors/sensor.py` → `sensor_readings` table + `SENSOR_CHANGE` events
- **OID scan** → `core/oid/scanner.py` → `oid_profiles.json` + `device_classifier.py`
- **User actions** (add printer, toner reset, groups, user management) → `web/routes/` → `store`/`database`
- **Reporting** → `web/routes/export_bp.py` → Excel/CSV from `logs.db`

---

## 📁 Project Structure

```text
Multi-Printer/
├── run.py                      # entry point: Flask + polling/scan/cleanup threads
├── models.py                   # User model (Flask-Login) + bcrypt
├── utils.py                    # email, reset tokens, reCAPTCHA
├── create_admin.py             # create/promote an admin from the CLI
├── requirements.txt            # dependencies
├── printers.example.json       # sample printers file
├── cartridge_yield_catalog.json# cartridge capacity catalog (+ local/Iran values)
├── start.bat / stop-project.bat# Windows start/stop helpers
├── config/
│   ├── __init__.py
│   └── settings.py             # ports, file paths, office subnets, SECRET_KEY, thresholds
├── core/
│   ├── database.py             # SQLite layer: logs, users, counters, toner/reset history
│   ├── poller.py               # polling loop, collector selection, online/offline handling
│   ├── store.py                # live memory (printer_data/PRINTERS) + PrevStore
│   ├── enhanced_collector.py   # primary collection path (all brands): counters/toner/trays
│   ├── yield_engine.py         # yield learning + cartridge catalog + status report
│   ├── groups.py               # custom printer groups (groups.json)
│   ├── device_classifier.py    # device classification from scanned OIDs
│   ├── security_audit.py       # security event log
│   ├── collectors/
│   │   ├── base.py             # shared helpers: print events, toner, A3/A4 attribution
│   │   ├── base_enhanced.py    # bridge to enhanced_collector
│   │   ├── toshiba.py          # Toshiba: vendor OIDs, A3/A4, twin, toner scrape, trays
│   │   ├── hp.py               # HP: counters/toner + fallback
│   │   ├── canon.py            # Canon: MF/LBP + Remote UI toner scrape
│   │   ├── brother.py          # Brother: toner/drum + fallback
│   │   └── sensor.py           # ECS100G sensor (temperature/humidity)
│   ├── snmp/
│   │   ├── protocol.py         # low-level SNMP v1/v2c over UDP/161 + version fallback + cache
│   │   └── oid_map.py          # OID maps (mainly Toshiba) + paper/toner mappings
│   └── oid/
│       ├── scanner.py          # per-IP OID scan & profile building (startup + weekly)
│       ├── catalog.py          # catalog of known OIDs
│       └── validator.py        # OID value validation + error log
├── web/
│   ├── __init__.py             # create_app: blueprints, auth, security, CORS
│   ├── auth.py                 # login/register/OAuth/reset + role & access decorators
│   ├── security.py             # CSRF, rate limiting, security headers
│   ├── routes/
│   │   ├── dashboard.py        # main page
│   │   ├── printers.py         # printer CRUD + toner reset + debug
│   │   ├── logs.py             # log APIs + manual events
│   │   ├── export_bp.py        # Excel/CSV export
│   │   ├── discover.py         # network device discovery
│   │   ├── scan.py             # OID scan/validation APIs
│   │   ├── stats.py            # daily stats & chart data
│   │   ├── system.py           # system status + manual poll
│   │   ├── security.py         # security dashboard + event APIs
│   │   ├── users.py            # user & access management
│   │   ├── groups.py           # custom groups API
│   │   ├── import_db.py        # database analyze/import with backup
│   │   ├── validation.py       # counter & profile consistency checks
│   │   └── yield_status.py     # Yield Engine status & report
│   ├── templates/              # base/dashboard/login/register/users/security/...
│   └── static/
│       ├── css/style.css
│       └── js/                 # dashboard.js (main UI logic), legacy-mode.js,
│                               # Sortable.min.js, chart.umd.min.js
├── tools/
│   └── diagnose_printer.py     # raw SNMP/Web dump of one printer for new-model debugging
└── docs/
    └── architecture.html       # interactive architecture + data-flow diagram + XML export
```

### Runtime files (not tracked in git)
`logs.db`, `printers.json`, `groups.json`, `oid_profiles.json`, `oid_validation_errors.txt`, `toner_report.txt`, `yield_status_report.txt`

---

## 🔧 Prerequisites

- Python **3.8+** (recommended **3.10+**)
- Network access to the printers with **161/UDP** open (SNMP v1 or v2c)
- `openpyxl` for Excel export (included in requirements)

## 🚀 Installation & Run

```bash
git clone https://github.com/mhgazmeh2000/multi-printer.git
cd multi-printer
python -m venv .venv
# Linux/macOS:  source .venv/bin/activate
# Windows:      .venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

Default: `http://localhost:5050`
On Windows you can also use `start.bat` and `stop-project.bat`.

If no user exists, the **first registered account** automatically becomes a verified `admin`; or run `python create_admin.py`.

---

## ⚙️ Environment Configuration

### Security & runtime
```bash
export ENVIRONMENT=development
export SECRET_KEY=change-me
export FLASK_PORT=5050
```

### Default printers (optional)
```bash
export DEFAULT_PRINTERS_JSON='[
  {"ip":"192.168.1.10","name":"Printer #1","community":"public"}
]'
```

### Office subnets
```bash
export OFFICE_SUBNET_IMAMAT=172.16.25
export OFFICE_SUBNET_SOROUSH=172.16.24
export OFFICE_SUBNET_FALESTIN=172.16.0
export OFFICE_SUBNET_ELAHIYE=172.16.32
```

### Miscellaneous
```bash
export ASSET_VERSION=20260609-2          # asset cache busting
export CORS_ALLOWED_ORIGINS="https://app.example.com"
# Mail (password reset), Google OAuth and reCAPTCHA keys — see config/settings.py
```

---

## 🔌 API Reference

### System
- `GET /api/status`
- `POST /api/poll/now`

### Printers
- `GET /api/printers` · `GET /api/printer/<ip>`
- `POST /api/printers/add` · `POST /api/printers/bulk-add` · `POST /api/printers/remove`
- `POST /api/printer/<ip>/update` (rename / group / …)
- `POST /api/printer/<ip>/toner_reset`
- `POST /api/printers/discover` · `POST /api/discover/auto-add`

### Logs
- `GET /api/logs/all` · `GET /api/printer/<ip>/log`
- `POST /api/logs/clear` · `POST /api/events/manual`

### Groups
- `GET/POST /api/groups`
- `POST /api/groups/<gid>/rename` · `DELETE /api/groups/<gid>`

### Database import
- `POST /api/import/analyze` · `POST /api/import/confirm`

Besides logs, `printer_counters`, `toner_history`, `sensor_readings` and Yield Engine tables can be imported — with section selection, IP/date filters, duplicate handling and automatic backup.

### Export
- `GET /api/export/excel`
- `GET /api/export/logs?format=csv` / `?format=excel`

### Scan & validation
- `POST /api/scan/oids` · `GET /api/scan/oids/<ip>` · `POST /api/scan/all` · `GET /api/scan/profiles`
- `GET /api/validate/counters` · `GET /api/validate/oids/<ip>`

### Yield Engine
- `GET /api/yield/status`
- `POST /api/yield/report`

### Stats
- `GET /api/stats/daily` · `GET /api/stats/sensor/daily`

### Users & security
- `GET /users` · `GET/POST /api/users`
- `POST /api/users/<id>/role` · `.../verify` · `.../access` · `DELETE /api/users/<id>`
- `GET /security` · `GET /api/security/events` · `GET /api/security/stats`

---

## 📊 Toner & Consumables

### Cartridge yield catalog
`cartridge_yield_catalog.json` provides realistic nominal capacities when neither `auto_learn` nor `device_capacity` exists yet — far better than a blind `2000` fallback.

Priority: **auto_learn → shared_profile → device_capacity → catalog → default**

```bash
export CARTRIDGE_YIELD_CATALOG=/path/to/cartridge_yield_catalog.json
# capacity mode (default: local — tuned for the Iranian refilled/compatible market)
set CARTRIDGE_YIELD_MODE=local        # Windows
export CARTRIDGE_YIELD_MODE=local     # Linux/macOS  (oem | compatible | refill | local)
export CARTRIDGE_YIELD_FACTOR=0.90    # optional extra-conservative factor
```

Each entry may carry all four values: `yield_per_page_oem/compatible/refill/local`.

### Brand notes
- **Canon i‑SENSYS (e.g. LBP233dw):** reports discrete codes `0=Empty / 5=Low / 7=OK` instead of a percentage. The system maps them to display-only percentages (0/15/70) with source `canon_status_code`, so yield learning stays unpolluted. Remote UI scraping is used as a fallback.
- **HP FutureSmart (e.g. M527 & E52645):** `MaxCapacity` is negative (‑3/‑2) and `Remaining` IS the percent (`unit=19`) — the system detects this. Cartridges 87A/87X/87Y and 89A/89X/89Y are in the catalog.
- **Brother:** some models only report toner *presence*; the UI then shows `—` and fabricates nothing.
- **RFC 3805 sentinel `-3`** = "supply present, exact level unknown" → status `unknown`, not `not_supported`.
- **Toshiba:** falls back to TopAccess scraping when needed; `pages_since_last_reset` is supported.

---

## 🧮 Log & Counter Integrity

To prevent wrong/inflated logs, `_counters_event` enforces these rules:

| Situation | System behavior |
|---|---|
| First poll of a device | Baseline snapshot stored; **no PRINT event created** |
| Counter reset (zeros/sharp drop + uptime=0) | `COUNTER_RESET` event; no fabricated printing |
| Slight drop / soft reset | `COUNTER_DROP`, baseline re-anchored |
| Unrealistic jump (beyond the physical max for the poll window) | rejected as `PRINT_OVERFLOW` |
| Slight drop after reboot (pages the device itself forgot) | `COUNTER_ANOMALY` with details |
| Normal printing | `PRINT` event with exact A3/A4 split and `poll_timestamp` |

Paper-size attribution runs on a snapshot-matching cycle; if an A3/A4 sub-counter lags, the `a3_lagged/a4_lagged` flags are recorded so logs stay honest.

---

## 🛠️ Tools

### Printer diagnostic tool
```bash
python tools/diagnose_printer.py <IP>
```
Dumps raw SNMP (system, counters, supplies) and the device's web pages into `diagnose_<ip>.md`, making it easy to add precise support for new models.

### Debug endpoints
- `GET /api/debug/printer/<ip>`
- `GET /api/debug/brother-toner/<ip>`
- `GET /api/debug/toshiba-snmp/<ip>`

---

## 🛡️ Security

- **bcrypt** auth + Flask-Login; roles `admin/manager/viewer`
- **CSRF** on every state-changing request (frontend uses `apiFetch`)
- **Rate limiting**, security headers, ENV-restricted CORS
- **Security audit log** (logins/failures/suspicious activity) at `/security`
- `SECRET_KEY` only from ENV; production refuses the insecure default (`config/settings.py`)
- Sensitive runtime files (database, printer list) are git-ignored
- The current repository has been **sanitized after a security review** (foreign/suspicious files and sensitive data removed from history)

> ⚠️ Before deploying: set a strong `SECRET_KEY`, change the default admin password, and put the service behind HTTPS if it is publicly reachable.

---

## 📈 Excel Export

Sheets: `Printer Status`, `Job Log`, `Reset History`

The **Reset History** sheet includes: reset time, device IP/name, cartridge color, reset percentage, total pages at reset, pages since reset, and `yield_per_page`.

---

## ⚠️ Operational Notes

- Assets are versioned with `ASSET_VERSION` (cache busting)
- `/api/debug/*` endpoints are for internal development only
- Never commit runtime files (already in `.gitignore`)

---

## 📄 License

Add your chosen license here.
