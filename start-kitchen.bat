@echo off
REM Family Kitchen: start the server and open the dashboard.
REM Double-click this file. Keep the window open while the system is in use.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo First run: setting up, this takes a minute...
    python -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt
)

REM Open the dashboard in the browser once the server has had time to start
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:5000/admin"

echo.
echo Family Kitchen is running. Close this window to stop it.
echo.
.venv\Scripts\python -m flask --app wsgi run --host 0.0.0.0
pause
