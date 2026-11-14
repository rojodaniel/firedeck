@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
  python app.py --download-adb %*
) else (
  py app.py --download-adb %*
)

if errorlevel 1 pause
endlocal
