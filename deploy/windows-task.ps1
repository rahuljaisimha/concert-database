# Registers the daily Windows Task Scheduler jobs that run concertdb inside WSL:
#   concert-database           deploy/run.sh: pages, setlist.fm, backup (about an hour)
#   concert-database-pollstar  deploy/run-pollstar.sh: Pollstar, ~1 request/s for hours
#   concert-database-gemini    deploy/run-gemini.sh: Gemini, ~1,400 grounded requests (1 am,
#                              soon after the free quota resets at midnight Pacific)
# They run side by side.
# Run once in PowerShell (as your normal user):  .\deploy\windows-task.ps1 -Distro Ubuntu-24.04
# -Distro is the WSL distribution with the repo in it (wsl -l -v).
param([string]$Distro = "Ubuntu")

function Register-ConcertTask($Name, $Script, $Hours, $Description, $At = "4am") {
    $Action = New-ScheduledTaskAction -Execute "wsl.exe" `
        -Argument "-d $Distro -- bash -lc ~/concert-database/deploy/$Script"
    $Trigger = New-ScheduledTaskTrigger -Daily -At $At
    $Settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -WakeToRun `
        -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours $Hours)
    # -StartWhenAvailable: if the PC was off or asleep at the start time, run as soon as it's back.
    # -WakeToRun: wake the PC from sleep for the run (needs wake timers enabled in power options).
    Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger `
        -Settings $Settings -Description $Description -Force
}

Register-ConcertTask "concert-database" "run.sh" 6 "Daily concert-database crawl (WSL)"
# Stops before the next day's 4 am start; the process also ends by itself when
# its daily budget or the due work runs out.
Register-ConcertTask "concert-database-pollstar" "run-pollstar.sh" 23 "Daily Pollstar capture for concert-database (WSL)"
Register-ConcertTask "concert-database-gemini" "run-gemini.sh" 12 "Daily concert-database Gemini opener search (WSL)" "1am"
