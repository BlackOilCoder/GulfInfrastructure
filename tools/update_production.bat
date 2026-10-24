@echo off
rem Refreshes Bluewater production data (BSEE OGOR-A wells + Production by Platform). Safe for Task Scheduler.
cd /d "%~dp0.."
python tools\build_production.py %*
