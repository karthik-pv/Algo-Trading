@echo off
cd /d "C:\Users\phvra\Desktop\Algo-Trading - 1.8"
title AlgoOptionScalper Server

REM Console-mode launcher - this window IS the server (loguru logs
REM print here) and closing the app window stops the server with it.
REM Taskbar icon / start_app_silent.vbs run the hidden-splash flow
REM instead (scripts\splash.py spawns that server itself), so this
REM bat does NOT start the splash - launching both would race two
REM servers against each other.
REM We call the venv python DIRECTLY - do NOT use activate.bat, because the
REM project path contains spaces which breaks venv PATH activation.
echo Starting server in this console window - live logs print below.
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
