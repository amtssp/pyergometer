@echo off
rem Starts the pyergometer workout page so it can be used from this PC and
rem from phones or tablets on the same network. Close this window to stop.
title Pyergometer
cd /d "%~dp0"
set PYTHONUNBUFFERED=1

where py >nul 2>nul
if %errorlevel%==0 (
    set PYTHON=py
) else (
    set PYTHON=python
)

echo Starter Pyergometer...
echo Luk dette vindue eller tryk Ctrl+C for at stoppe serveren.
echo.
%PYTHON% pyergometer.py --host 0.0.0.0 %*

echo.
echo Pyergometer er stoppet.
pause
