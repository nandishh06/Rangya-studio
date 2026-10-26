@echo off
REM Rangya Studio - Windows launcher. First run sets everything up.
cd /d "%~dp0"
where ffmpeg >nul 2>nul || (
  echo ffmpeg is not installed. Install it with:  winget install Gyan.FFmpeg
  echo then close and reopen this window.
  pause & exit /b 1
)
if not exist .venv\Scripts\python.exe (
  echo Setting up ^(first run^)...
  py -3.12 -m venv .venv 2>nul || py -3.11 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
)
.venv\Scripts\python.exe -m pip install -q --upgrade pip
.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if "%PORT%"=="" set PORT=8000
start "" http://127.0.0.1:%PORT%
.venv\Scripts\python.exe -m studio.app
pause
