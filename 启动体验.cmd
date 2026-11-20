@echo off
cd /d "%~dp0"

if not exist "tracelab\__main__.py" (
  echo [ERROR] tracelab\ directory not found.
  echo Please extract the whole submission zip first,
  echo then run this launcher from the extracted folder.
  echo Current directory: %CD%
  echo.
  pause
  exit /b 2
)

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] python not found in PATH. Please install Python 3.11+ first.
  pause
  exit /b 2
)

echo [demo] Running sample evaluation and opening the demo page ...
python -m tracelab demo
if errorlevel 1 goto fail
echo.
echo Demo page opened in your browser. Full reports are under out\demo\
pause
exit /b 0

:fail
echo Failed - see the error message above.
pause
exit /b 1
