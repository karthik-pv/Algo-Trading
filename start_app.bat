@echo off
setlocal EnableDelayedExpansion
cd /d "C:\Users\phvra\Desktop\Algo-Trading - 1.8"

REM Start the server in its own window (close that window to stop the app).
REM We call the venv python DIRECTLY - do NOT use activate.bat, because the
REM project path contains spaces which breaks venv PATH activation.
start "Algo Trading Server" cmd /k "venv\Scripts\python.exe server.py"

REM The server takes 20-30+ seconds to initialize. Wait until port 5000 is
REM actually listening (up to ~90 seconds) before opening the browser.
echo Starting server... this usually takes under a minute.
set /a tries=0
:waitloop
ping -n 3 127.0.0.1 >nul
netstat -ano | findstr "LISTENING" | findstr ":5000" >nul
if not errorlevel 1 goto openbrowser
set /a tries+=1
echo Still waiting for server to come online... !tries!/45
if !tries! lss 45 goto waitloop

echo.
echo Server did not start in time. Check the "Algo Trading Server" window for errors.
pause
exit /b

:openbrowser
set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if exist "%CHROME%" (
    start "" "%CHROME%" --app=http://127.0.0.1:5000
) else if exist "%EDGE%" (
    start "" "%EDGE%" --app=http://127.0.0.1:5000
) else (
    start "" http://127.0.0.1:5000
)
echo Server is online - app window opened.
pause
exit /b
