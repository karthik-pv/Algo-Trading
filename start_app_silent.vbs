' AlgoOptionScalper - silent launcher.
' scripts\splash.py paints instantly, spawns the hidden server itself,
' shows live startup progress, and closes when the server is online.
' Logs: data\logs\<MM_DD>\  Console alternative: start_app.bat
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\phvra\Desktop\Algo-Trading - 1.8"
sh.Run """" & sh.CurrentDirectory & "\venv\Scripts\pythonw.exe"" scripts\splash.py", 1, False
