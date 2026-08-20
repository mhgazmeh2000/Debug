@echo off
setlocal
set "SCRIPT_DIR=%~dp0"

title PrintGuard - Starting...

echo.
echo  ============================================================
echo   PrintGuard - Multi Printer Monitoring
echo  ============================================================
echo.

:: --- Navigate to script directory ---
:: pushd maps UNC to a drive letter (e.g. V:) which avoids the
:: parentheses problem in UNC paths like "workspace-... (1)"
pushd "%SCRIPT_DIR%" >nul 2>&1
if errorlevel 1 goto folder_error

:: IMPORTANT: After pushd, %CD% is the mapped drive path (clean, no UNC).
:: Always use %CD% (not %SCRIPT_DIR%) to avoid UNC + parentheses issues.

:: --- Step 1: Check Python ---
echo  [1/5] Checking Python...
set "PY_CMD="

:: Priority 1: venv
if exist "%CD%\.venv\Scripts\python.exe" set "PY_CMD=%CD%\.venv\Scripts\python.exe"
if defined PY_CMD goto python_found

:: Priority 2: py launcher
where py >nul 2>&1
if errorlevel 1 goto skip_py
set "PY_CMD=py"
:skip_py
if defined PY_CMD goto python_found

:: Priority 3: python in PATH
where python >nul 2>&1
if errorlevel 1 goto skip_python
set "PY_CMD=python"
:skip_python
if defined PY_CMD goto python_found

:: Priority 4: common install locations
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if defined PY_CMD goto python_found
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY_CMD goto python_found
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if defined PY_CMD goto python_found
if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if defined PY_CMD goto python_found
if exist "%USERPROFILE%\AppData\Local\Programs\Python\Python313\python.exe" set "PY_CMD=%USERPROFILE%\AppData\Local\Programs\Python\Python313\python.exe"
if defined PY_CMD goto python_found

goto python_error

:python_found
"%PY_CMD%" --version >nul 2>&1
if errorlevel 1 goto python_error
for /f "tokens=2" %%v in ('"%PY_CMD%" --version 2^>^&1') do echo  [OK] Python %%v

:: --- Step 2: Clean cache ---
echo  [2/5] Cleaning cache...
for /d /r "%CD%" %%d in (__pycache__) do rd /s /q "%%d" 2>nul
del /s /q "%CD%\*.pyc" 2>nul
echo  [OK] Cache cleared

:: --- Step 3: Dependencies ---
echo  [3/5] Checking dependencies...
"%PY_CMD%" -m pip install -r "%CD%\requirements.txt" -q --disable-pip-version-check 2>nul
echo  [OK] Dependencies ready

:: --- Step 4: Port ---
echo  [4/5] Port configuration...
set "USER_PORT=5050"
set /p USER_PORT="  Port [5050]: "
if "%USER_PORT%"=="" set USER_PORT=5050
set FLASK_PORT=%USER_PORT%
echo  [OK] Port: %USER_PORT%

:: --- Step 5: Kill old processes ---
echo  [5/5] Cleaning old processes on port %USER_PORT%...
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":%USER_PORT% " ^| findstr "LISTENING"') do (
    echo        Killing PID %%a
    taskkill /F /PID %%a >nul 2>&1
)
echo  [OK] Port %USER_PORT% ready

:: --- Launch ---
echo.
echo  ============================================================
echo   Launching on http://localhost:%USER_PORT%/
echo   Press Ctrl+C to stop
echo  ============================================================
echo.

title PrintGuard [Port %USER_PORT%]

start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 3; Start-Process 'http://localhost:%USER_PORT%/'"

"%PY_CMD%" run.py

echo.
echo  PrintGuard stopped.
popd
pause
exit /b 0

:folder_error
echo [ERROR] Cannot access project folder.
pause
exit /b 1

:python_error
echo  [ERROR] Python not found. Install Python 3.10+
echo  Download: https://www.python.org/downloads/
echo  Make sure "Add Python to PATH" is checked during install.
pause
exit /b 1
