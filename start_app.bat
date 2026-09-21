@echo off
cd /d "C:\Users\phvra\Desktop\Algo-Trading - 1.8"
title Algo Trading Server

REM Single-window launcher:
REM   - this window IS the server (loguru logs print here)
REM   - the server opens the app window (browser) itself once it is online
REM   - closing the app window stops the server, and this window closes with it
REM We call the venv python DIRECTLY - do NOT use activate.bat, because the
REM project path contains spaces which breaks venv PATH activation.
echo Starting server... the app window opens automatically when it is online.
echo Closing the app window stops the server and closes this window.
venv\Scripts\python.exe server.py

REM Clean stop (app window closed): exit quietly, closing this window.
REM Crash: stay open so the error is readable; details also in data\logs.
if errorlevel 1 (
    echo.
    echo Server exited with an error. Details in data\logs.
    pause
)
exit /b
