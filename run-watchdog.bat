@echo off
setlocal
pushd "%~dp0"
:loop
"python" run.py >> logs\watchdog.log 2>&1
 timeout /t 10 /nobreak >nul
goto loop
