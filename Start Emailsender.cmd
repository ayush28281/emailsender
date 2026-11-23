@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto python
py -3 -m outreach --open
goto done
:python
python -m outreach --open
:done
if errorlevel 1 pause
