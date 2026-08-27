@echo off
rem live-brain one-shot start via brain_ctl.ps1 (scheduled task, survives SSH)
powershell -NoProfile -ExecutionPolicy Bypass -File C:\live-brain\brain_ctl.ps1 -Action start
