@echo off
chcp 65001 >nul
rem =====================================================================
rem  install-startup-task.bat — جلوگیری از خاموش‌ماندن مانیتورینگ
rem
rem  علت ساخت: خاموشی ۸۹ساعته‌ی سرور (پنجشنبه تا یکشنبه صبح) باعث شد چاپ‌ها
rem  دیر ثبت شوند. این اسکریپت:
rem   ۱) یک نگهبان (run-watchdog.bat) می‌سازد که اگر برنامه کرش کرد، بعد از
rem      ۱۰ ثانیه دوباره بالا می‌آورد
rem   ۲) یک Scheduled Task می‌سازد که هنگام بالا آمدن ویندوز (حتی بدون لاگین)
rem      نگهبان را اجرا کند
rem   ۳) Sleep خودکار ویندوز را خاموش می‌کند
rem
rem  اجرا: روی فایل راست‌کلیک → Run as administrator
rem  حذف تسک: schtasks /Delete /TN "PrintGuardMonitor" /F
rem =====================================================================
setlocal

set "APP_DIR=%~dp0"
set "APP_DIR=%APP_DIR:~0,-1%"
rem اگر اسکریپت داخل tools\ است، ریشه‌ی برنامه = والد آن است
if not exist "%APP_DIR%\run.py" if exist "%APP_DIR%\..\run.py" set "APP_DIR=%APP_DIR%\.."
if not exist "%APP_DIR%\run.py" (
  echo [FAIL] این فایل باید کنار run.py (ریشه‌ی برنامه) باشد: %APP_DIR%
  exit /b 1
)

set "PY_CMD=python"
if exist "%APP_DIR%\.venv\Scripts\python.exe" set "PY_CMD=%APP_DIR%\.venv\Scripts\python.exe"

echo.
echo === [۱/۳] ساخت نگهبان run-watchdog.bat
if not exist "%APP_DIR%\logs" mkdir "%APP_DIR%\logs"
> "%APP_DIR%\run-watchdog.bat" (
  echo @echo off
  echo title PrintGuard Monitor ^(watchdog^)
  echo pushd "%APP_DIR%"
  echo :loop
  echo echo [%%date%% %%time%%] starting run.py ^>^> logs\watchdog.log
  echo "%PY_CMD%" run.py ^>^> logs\watchdog.log 2^>^&1
  echo echo [%%date%% %%time%%] exited — restart in 10s ^>^> logs\watchdog.log
  echo timeout /t 10 /nobreak ^>nul
  echo goto loop
)
echo    OK

echo.
echo  === [۲/۳] ساخت Scheduled Task (ONLOGON — اجرای خودکار با کاربر فعلی)
rem مسیر پروژه روی share شبکه است؛ SYSTEM معمولاً به آن دسترسی ندارد.
rem ONLOGON با حساب فعلی اجازه دسترسی به مسیر UNC را حفظ می‌کند.
schtasks /Create /TN "PrintGuardMonitor" /SC ONLOGON /RU "%USERNAME%" /TR "cmd.exe /c call \"%APP_DIR%\\run-watchdog.bat\"" /F
if errorlevel 1 (
  echo [هشدار] ساخت تسک ONLOGON ناموفق بود.
  echo این فایل را با حساب کاربری فعلی اجرا کن یا Task را دستی بساز.
) else (
  echo    OK — تسک ساخته شد و با ورود کاربر اجرا می‌شود
)

echo.
echo === [۳/۳] خاموش‌کردن Sleep خودکار ویندوز (روی برق)
powercfg /change standby-timeout-ac 0 >nul 2>&1 && echo    OK || echo    [هشدار] نیازمند Run as administrator

echo.
echo =============================================================
echo  ✓ از این پس مانیتورینگ با هر بالا آمدن ویندوز خودش اجرا می‌شود
echo    و کرش هم بعد از ۱۰ ثانیه ری‌استارت می‌شود.
echo  ✓ برای شروع همین حالا: schtasks /Run /TN "PrintGuardMonitor"
echo  ✓ وضعیت: schtasks /Query /TN "PrintGuardMonitor" /V /FO LIST
echo =============================================================
exit /b 0
