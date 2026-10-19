@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe manage.py stop
if errorlevel 1 goto fail
pause
exit /b 0
:fail
echo Operation failed. Check the message above and docs/WINDOWS.md.
pause
exit /b 1
