@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
if not exist ".env" copy ".env.example" ".env" >nul
echo Fill TELEGRAM_BOT_TOKEN and YANDEX_MUSIC_TOKEN in .env, then run run.bat.
pause
exit /b 0
:failed
echo Setup failed. Python 3.10 or newer and internet access are required.
pause
exit /b 1
