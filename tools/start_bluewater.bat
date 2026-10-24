@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv-bathymetry\Scripts\python.exe" (
  echo Run tools\install_bathymetry.bat once to install the local bathymetry service.
  exit /b 1
)
.venv-bathymetry\Scripts\python.exe -u tools\serve.py %*
exit /b %errorlevel%
