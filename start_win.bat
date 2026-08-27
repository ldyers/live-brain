@echo off
rem live-brain one-shot start via schtasks (survives SSH session end)
schtasks /Delete /TN LiveBrain_run /F >nul 2>&1
schtasks /Create /TN LiveBrain_run /TR "cmd /c cd /d C:\live-brain && C:\Python314\python.exe live_brain.py >> C:\live-brain\logs\stdout.log 2>&1" /SC ONCE /ST 00:00 /RL HIGHEST /F >nul
schtasks /Run /TN LiveBrain_run >nul
echo started
exit /b 0
