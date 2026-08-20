@echo off
chcp 65001 >nul
rem =====================================================================
rem  install-weekly-audit.bat — ممیزی هفتگی خودکار سازگاری لاگ/شمارنده
rem
rem  یک Scheduled Task می‌سازد که هر شنبه ساعت ۸ صبح ممیزی ۷روز اخیر را
rem  اجرا کند و خروجی را در logs\audit-history.log الحاق نماید.
rem
rem  اجرا: روی فایل راست‌کلیک → Run as administrator
rem  اجرای دستی ممیزی: schtasks /Run /TN "PrintGuardWeeklyAudit"
rem  حذف تسک:        schtasks /Delete /TN "PrintGuardWeeklyAudit" /F
rem =====================================================================
setlocal

set "APP_DIR=%~dp0"
set "APP_DIR=%APP_DIR:~0,-1%"
rem اگر اسکریپت داخل tools\ است، ریشه‌ی برنامه = والد آن است
if not exist "%APP_DIR%\run.py" if exist "%APP_DIR%\..\run.py" set "APP_DIR=%APP_DIR%\.."
if not exist "%APP_DIR%\tools\audit_pages.py" (
  echo [FAIL] این فایل باید کنار tools\audit_pages.py ^(ریشه‌ی برنامه^) باشد
  exit /b 1
)
set "PY_CMD=python"
if exist "%APP_DIR%\.venv\Scripts\python.exe" set "PY_CMD=%APP_DIR%\.venv\Scripts\python.exe"
if not exist "%APP_DIR%\logs" mkdir "%APP_DIR%\logs"

echo === [۱/۲] ساخت اجراکننده‌ی run-weekly-audit.bat
> "%APP_DIR%\run-weekly-audit.bat" (
  echo @echo off
  echo title PrintGuard Weekly Audit
  echo cd /d "%APP_DIR%"
  echo echo [%%date%% %%time%%] weekly audit start ^>^> logs\audit-history.log
  echo "%PY_CMD%" tools\audit_pages.py --days 7 --md audit-weekly.md ^>^> logs\audit-history.log 2^>^&1
  echo echo [%%date%% %%time%%] done ^>^> logs\audit-history.log
)
echo    OK

echo === [۲/۲] ساخت Scheduled Task هفتگی
schtasks /Create /TN "PrintGuardWeeklyAudit" /SC WEEKLY /D SAT /ST 08:00 /RU SYSTEM /RL HIGHEST /F /TR "'%APP_DIR%\run-weekly-audit.bat'"
if errorlevel 1 (echo [FAIL] ساخت تسک ناموفق ^(Run as administrator؟^) & exit /b 1)

echo.
echo ✓ تسک هفتگی ساخته شد: هر شنبه ۰۸:۰۰ ممیزی ۷روز اخیر
echo ✓ گزارش: audit-weekly.md  ^|  تاریخچه‌ی اجراها: logs\audit-history.log
echo ✓ اجرای همین حالا: schtasks /Run /TN "PrintGuardWeeklyAudit"
exit /b 0
