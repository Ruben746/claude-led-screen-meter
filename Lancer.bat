@echo off
setlocal
cd /d "%~dp0"
title LED Meter

if exist ".venv\Scripts\python.exe" goto dependencies
echo Preparation du premier lancement...
py -3 --version >nul 2>&1
if errorlevel 1 goto use_python
py -3 -m venv .venv
if errorlevel 1 goto setup_error
goto dependencies

:use_python
python --version >nul 2>&1
if errorlevel 1 goto missing_python
python -m venv .venv
if errorlevel 1 goto setup_error

:dependencies
".venv\Scripts\python.exe" -c "import requests, PIL, aiohttp, pypixelcolor, dotenv" >nul 2>&1
if not errorlevel 1 goto launch
echo Installation des dependances...
".venv\Scripts\python.exe" -m pip --version >nul 2>&1
if not errorlevel 1 goto install
".venv\Scripts\python.exe" -m ensurepip --upgrade
if errorlevel 1 goto setup_error
:install
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto setup_error

:launch
if exist ".env" goto configured
copy /y ".env.example" ".env" >nul
if errorlevel 1 goto setup_error
:configured
set "LED_IMG_PATH=%~dp0preview.png"
if not defined LED_OPEN_BROWSER set "LED_OPEN_BROWSER=1"
echo Demarrage du compteur. Le panneau va s'ouvrir dans le navigateur.
echo Garde cette fenetre ouverte. Pour arreter : Ctrl+C ou ferme la fenetre.
".venv\Scripts\python.exe" -u windows_runner.py
if errorlevel 1 goto run_error
exit /b 0

:missing_python
echo Python 3 est introuvable. Installe Python depuis https://www.python.org/downloads/windows/
echo Active l'option Add Python to PATH, puis relance ce fichier.
goto failed
:setup_error
echo La preparation a echoue. Consulte le message ci-dessus et verifie ta connexion Internet.
goto failed
:run_error
echo Le compteur s'est arrete avec une erreur. Consulte le message ci-dessus.
:failed
pause
exit /b 1
