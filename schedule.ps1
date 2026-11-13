# Run .\run.ps1 automatically in the background (Windows Task Scheduler, current user).
#
#   .\schedule.ps1 install [minutes]   # default: every 60 minutes, and at logon
#   .\schedule.ps1 status
#   .\schedule.ps1 uninstall
#
# The task runs while you are logged on (a locked screen is fine). It never opens the
# browser: if the Plaud login expires, library\auto.log says to run .\run.ps1 once.
param([string]$Action = "", [int]$Minutes = 60)
Set-Location -LiteralPath $PSScriptRoot
$TaskName = "Plaud transcripts sync"
$Library = if ($env:PLAUD_LIBRARY) { $env:PLAUD_LIBRARY } else { Join-Path $PSScriptRoot "library" }
$Log = Join-Path $Library "auto.log"

switch ($Action) {
    "install" {
        if ($Minutes -lt 5) { Write-Host "ERROR: use 5 minutes or more."; exit 1 }
        $script = Join-Path $PSScriptRoot "run.ps1"
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $PSScriptRoot `
            -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -Unattended"
        $repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
        $logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 3)
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $repeat, $logon `
            -Settings $settings -Description "Fetch new Plaud recordings and transcribe them." -Force | Out-Null
        Write-Host "Installed: runs every $Minutes minutes while you are logged on."
        Write-Host "Log: $Log"
    }
    "uninstall" {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Removed the scheduled task."
    }
    "status" {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $task) { Write-Host "Not installed. Run: .\schedule.ps1 install"; exit 0 }
        $info = $task | Get-ScheduledTaskInfo
        Write-Host "State: $($task.State) | last run: $($info.LastRunTime) | last result: $($info.LastTaskResult) | next run: $($info.NextRunTime)"
        Write-Host "Log: $Log"
        if (Test-Path -LiteralPath $Log) { Get-Content -LiteralPath $Log -Tail 8 -Encoding UTF8 }
    }
    default {
        Write-Host "Usage: .\schedule.ps1 install [minutes] | status | uninstall"
        exit 1
    }
}
