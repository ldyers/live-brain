param([string]$Action = "restart")
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$py  = 'C:\Python314\python.exe'
$app = 'C:\live-brain'

function Kill-Brain {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'live_brain\.py' } |
        ForEach-Object { Write-Host ("killed " + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}

function Start-Brain {
    # launch into INTERACTIVE session via scheduled task (winmm audio needs it);
    # IgnoreNew = no duplicate instances; ExecutionTimeLimit 0 = remove default 72h auto-kill;
    # trigger in the past = never auto-triggers, manual Start-ScheduledTask only
    $action  = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c cd /d C:\live-brain && C:\Python314\python.exe live_brain.py >> C:\live-brain\logs\stdout.log 2>&1'
    $trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddDays(-1))
    $principal = New-ScheduledTaskPrincipal -UserId 'Administrator' -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    Register-ScheduledTask -TaskName 'LiveBrain_run' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName 'LiveBrain_run'
    Write-Host "started via scheduled task"
}

function Wait-Health {
    for ($i = 0; $i -lt 25; $i++) {
        Start-Sleep -Seconds 1
        try {
            $r = Invoke-WebRequest -Uri 'http://127.0.0.1:23460/health' -UseBasicParsing -TimeoutSec 3
            if ($r.Content -eq 'ok') { Write-Host "healthy after $i s"; exit 0 }
        } catch {}
    }
    Write-Host "FAILED to become healthy"
    Get-Content 'C:\live-brain\logs\stdout.log' -Tail 15 -ErrorAction SilentlyContinue
    exit 1
}

switch ($Action) {
    "stop"   { Kill-Brain }
    "start"  { Start-Brain; Wait-Health }
    "status" {
        $p = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'live_brain\.py' }
        if ($p) { Write-Host ("running pid=" + $p.ProcessId) } else { Write-Host "not running" }
    }
    default  { Kill-Brain; Start-Brain; Wait-Health }
}
