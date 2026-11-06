@echo off
rem Dubbelklik om The Format House - Leads te starten (Windows).
cd /d "%~dp0"
echo.
echo   The Format House - Leads wordt gestart...
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
if exist .venv\Scripts\python.exe goto run
echo   Eerste keer: alles installeren (1 a 2 minuten)...
%PY% -m venv .venv || goto nopy
.venv\Scripts\python -m pip install -q -e . || goto fail
:run
if not exist .env copy .env.example .env >nul
.venv\Scripts\python -m formathouse.webapp
goto end
:nopy
echo   Python 3.10 of nieuwer is nodig. Installeer het via python.org (vink "Add Python to PATH" aan).
start https://www.python.org/downloads/windows/
pause
goto end
:fail
echo   Installatie mislukt. Stuur een screenshot van dit venster.
pause
:end
