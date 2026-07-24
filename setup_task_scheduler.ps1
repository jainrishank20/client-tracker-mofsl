# Run once to register the daily pipeline in Windows Task Scheduler.
# After this, pipeline runs automatically Mon-Sat at 7:30 PM — no Oracle VM needed.
# To remove: Unregister-ScheduledTask -TaskName "MO Daily Pipeline" -Confirm:$false

$REPO_DIR = "C:\Users\jainr\Desktop\client-tracker"
$SCRIPT   = "$REPO_DIR\run_pipeline.ps1"
$TASK_NAME = "MO Daily Pipeline"

$action  = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$SCRIPT`"" `
    -WorkingDirectory $REPO_DIR

# Mon=2, Tue=3, Wed=4, Thu=5, Fri=6, Sat=7  (Sun=1 skipped)
$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday,Saturday `
    -At "19:30"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable    # runs at next opportunity if PC was off at 7:30 PM

Register-ScheduledTask `
    -TaskName  $TASK_NAME `
    -Action    $action `
    -Trigger   $trigger `
    -Settings  $settings `
    -RunLevel  Highest `
    -Force

Write-Host "Task '$TASK_NAME' registered. Runs Mon-Sat at 7:30 PM IST." -ForegroundColor Green
Write-Host "To run manually right now: .\run_pipeline.ps1"
Write-Host "To remove: Unregister-ScheduledTask -TaskName '$TASK_NAME' -Confirm:`$false"
