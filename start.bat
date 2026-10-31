@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Голосовой ассистент
set PY=
where py >nul 2>nul && set PY=py
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
    echo Не найден Python 3.10 или новее.
    echo Установите его с https://www.python.org/downloads/ ^(отметьте "Add python.exe to PATH"^)
    echo и запустите этот файл снова.
    pause
    exit /b 1
)
%PY% app\run.py
if errorlevel 1 pause
