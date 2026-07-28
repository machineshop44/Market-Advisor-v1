@echo off
cd /d "%~dp0"
REM Console mode (debug). Prefer "Start Market Advisor.vbs" for tray / no CMD.
py -3.12 main.py
pause
