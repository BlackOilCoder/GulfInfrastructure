@echo off
rem Refreshes the Bluewater ocean-currents forecast. Safe to run from Windows Task Scheduler.
cd /d "%~dp0.."
.venv-currents\Scripts\python tools\build_currents.py --days 3 %*
