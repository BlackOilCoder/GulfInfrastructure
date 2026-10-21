@echo off
rem Refreshes the Bluewater ocean-currents forecast. Safe to run from Windows Task Scheduler.
cd /d "%~dp0.."
python tools\build_currents.py --depths 50 100 200 %*
