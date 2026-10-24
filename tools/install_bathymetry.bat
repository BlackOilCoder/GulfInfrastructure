@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv-bathymetry\Scripts\python.exe" (
  python -m venv .venv-bathymetry
  if errorlevel 1 exit /b 1
)
.venv-bathymetry\Scripts\python.exe -m pip install -r tools\requirements-bathymetry.txt
if errorlevel 1 exit /b 1
.venv-bathymetry\Scripts\python.exe -u tools\build_bathymetry.py %*
exit /b %errorlevel%
