@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>&1
    if errorlevel 1 (
        python -m venv .venv
    ) else (
        py -3 -m venv .venv
    )
    if errorlevel 1 goto error
)
".venv\Scripts\python.exe" -c "from PyQt5 import QtCore, QtGui, QtWidgets; import requests, urllib3" >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto error
)
".venv\Scripts\python.exe" IAD.py
if errorlevel 1 goto error
exit /b 0

:error
echo.
echo No se pudo iniciar IAD. Verifica que Python 3 este instalado.
echo La instalacion inicial de dependencias requiere Internet.
pause
exit /b 1
