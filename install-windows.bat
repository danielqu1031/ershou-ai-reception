@echo off
cd /d "%~dp0"
py -3.12 -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
if not exist config.local.json copy config.example.json config.local.json >nul
echo Installed. Edit config.local.json, install Ollama and download the model.
echo See docs/WINDOWS.md for next steps.
pause
exit /b 0
:fail
echo Installation failed. Install Python 3.12 with the Python Launcher first.
pause
exit /b 1
