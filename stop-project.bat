@echo off
setlocal
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"

echo.
echo  ============================================================
echo   PrintGuard - Stop Project
echo  ============================================================
echo.

:: --- Find port ---
set "PORT=5050"
if defined FLASK_PORT set "PORT=%FLASK_PORT%"

echo  [1/3] Killing processes on port %PORT%...
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    echo        Killing PID %%a
    taskkill /F /PID %%a >nul 2>&1
)

echo.
echo  [2/3] Killing Python processes for this project...

:: Kill python.exe running this project
for /f "tokens=2" %%i in ('tasklist /FI "IMAGENAME eq python.exe" /NH 2^>nul ^| findstr /R "^[0-9]"') do (
    wmic process where "ProcessId=%%i" get CommandLine /Value 2>nul | findstr /I "run.py" >nul 2>&1
    if not errorlevel 1 (
        echo        Killing python.exe PID %%i
        taskkill /F /PID %%i >nul 2>&1
    )
)

:: Kill python3.exe running this project
for /f "tokens=2" %%i in ('tasklist /FI "IMAGENAME eq python3.exe" /NH 2^>nul ^| findstr /R "^[0-9]"') do (
    wmic process where "ProcessId=%%i" get CommandLine /Value 2>nul | findstr /I "run.py" >nul 2>&1
    if not errorlevel 1 (
        echo        Killing python3.exe PID %%i
        taskkill /F /PID %%i >nul 2>&1
    )
)

echo.
echo  [3/3] Verifying...
timeout /t 1 /nobreak >nul 2>&1

netstat -aon 2>nul | findstr ":%PORT% " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo  [WARN] Port %PORT% still in use. Try closing manually.
) else (
    echo  [OK] Port %PORT% is free.
)

echo.
echo  Done!
echo.
pause
endlocal
