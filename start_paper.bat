@echo off
setlocal
cd /d "%~dp0"
title angel_auto - PAPER (nakli paisa)

echo.
echo ================================================================
echo   angel_auto  -  PAPER TRADING  (nakli paisa, testing)
echo ================================================================
echo.

".venv\Scripts\python.exe" scripts\check_config.py
if errorlevel 1 (
    echo.
    echo Config mein galti hai - upar dekhein. App start nahi hua.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -c "import sys; from angel_auto.settings import Mode, get_settings; sys.exit(0 if get_settings().app.mode == Mode.PAPER else 3)"
if errorlevel 1 (
    echo.
    echo config\config.yaml mein "mode: paper" nahi hai - paper start nahi kiya.
    echo Paper ke liye mode: paper karein, ya live ke liye start_live.bat chalayein.
    pause
    exit /b 1
)

start "" cmd /c "timeout /t 12 /nobreak >nul & start http://127.0.0.1:8000"
echo.
echo Dashboard shuru ho raha hai - browser apne aap khulega.
echo Band karne ke liye is window mein Ctrl+C dabayein.
echo.
".venv\Scripts\python.exe" scripts\run_dashboard.py
echo.
echo App band ho gaya.
pause
