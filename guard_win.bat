@echo off
rem live-brain guard: port 23460 down -> clean stale -> restart via brain_ctl.ps1 (interactive session)
setlocal
set APPDIR=C:\live-brain
set WARMUP=90
set LOG=%APPDIR%\logs\guard.log

:loop
netstat -ano | findstr :23460 | findstr LISTEN >nul
if not errorlevel 1 goto ok

echo %date% %time% port down - cleaning stale instances and restarting >> "%LOG%"
cd /d %APPDIR%
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object {$_.CommandLine -match 'live_brain\.py'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >> "%LOG%" 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -File %APPDIR%\brain_ctl.ps1 -Action start >> "%LOG%" 2>&1

set /a n=0
:waitloop
timeout /t 10 /nobreak >nul
netstat -ano | findstr :23460 | findstr LISTEN >nul
if not errorlevel 1 goto ok
set /a n+=10
if %n% lss %WARMUP% goto waitloop
echo %date% %time% warmup timeout after %WARMUP%s - will retry >> "%LOG%"
goto loop

:ok
timeout /t 60 /nobreak >nul
goto loop
