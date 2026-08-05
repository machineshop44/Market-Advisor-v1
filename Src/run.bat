@echo off
cd /d "%~dp0"
REM Console mode (debug) — Task Manager shows python.exe.
REM Preferred: project "Start Market Advisor.lnk" / Build-MarketAdvisor-Launcher.ps1 → MarketAdvisor.exe
py -3.12 main.py
pause
