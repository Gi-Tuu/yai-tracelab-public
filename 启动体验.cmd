@echo off
cd /d "%~dp0"
echo [demo] Running sample evaluation and opening the demo page ...
python -m tracelab demo
if errorlevel 1 goto fail
echo.
echo Demo page opened in your browser. Full reports are under out\demo\
pause
exit /b 0
:fail
echo Failed - see the red text above.
pause
exit /b 1
