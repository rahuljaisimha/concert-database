# Registers a daily Windows Task Scheduler job that runs concertdb inside WSL.
# Run once in PowerShell (as your normal user):  .\deploy\windows-task.ps1
# Change $Distro if your WSL distribution isn't called "Ubuntu" (wsl -l -v).
$Distro = "Ubuntu"
$Action = New-ScheduledTaskAction -Execute "wsl.exe" `
    -Argument "-d $Distro -- bash -lc ~/concert-database/deploy/run.sh"
$Trigger = New-ScheduledTaskTrigger -Daily -At 4am
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)
# -StartWhenAvailable: if the PC was off or asleep at 4am, run as soon as it's back.
# -WakeToRun: wake the PC from sleep for the run (needs wake timers enabled in power options).
Register-ScheduledTask -TaskName "concert-database" -Action $Action -Trigger $Trigger `
    -Settings $Settings -Description "Daily concert-database crawl (WSL)" -Force
