@echo off
setlocal
cd /d "%~dp0"
title angel_auto - LIVE (asli paisa)

echo.
echo ================================================================
echo   angel_auto  -  LIVE TRADING  (ASLI PAISA LAGEGA)
echo ================================================================
echo.

".venv\Scripts\python.exe" scripts\check_config.py
if errorlevel 1 (
    echo.
    echo Config mein galti hai - upar dekhein. App start nahi hua.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -c "import sys; from angel_auto.settings import Mode, get_settings; sys.exit(0 if get_settings().app.mode == Mode.LIVE else 3)"
if errorlevel 1 (
    echo.
    echo config\config.yaml mein "mode: live" nahi hai - live start nahi kiya.
    echo Paper chalana hai to start_paper.bat chalayein.
    pause
    exit /b 1
)

echo.
echo  ASLI orders aapke Angel One account mein jayenge.
echo   - Angel One app saath mein khuli rakhein aur har order milayein
echo   - Pehle din sirf 1 lot, poore market time screen ke saamne rahein
echo   - Kuch galat lage to dashboard par Kill switch dabayein
echo.
set "CONFIRM="
set /p "CONFIRM=Pakka live chalana hai? Haan ho to YES likhein (bade aksharon mein): "
if not "%CONFIRM%"=="YES" (
    echo.
    echo Live start cancel kar diya.
    pause
    exit /b 1
)

rem The live-trading lock is set only for this window's process - never saved anywhere.
set "ANGEL_LIVE_TRADING_CONFIRMED=YES_I_UNDERSTAND_THE_RISK"
start "" cmd /c "timeout /t 12 /nobreak >nul & start http://127.0.0.1:8000"
echo.
echo Dashboard shuru ho raha hai - browser apne aap khulega.
echo Band karne ke liye is window mein Ctrl+C dabayein.
echo.
".venv\Scripts\python.exe" scripts\run_dashboard.py
echo.
echo App band ho gaya.
pause
