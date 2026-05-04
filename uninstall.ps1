$ErrorActionPreference = "Continue"
$here = $PSScriptRoot
$folderName = Split-Path $here -Leaf
$taskName = "ClaudeRelay-$folderName"

Write-Host "Uninstalling $taskName" -ForegroundColor Yellow
try {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
    Write-Host "Removed Task Scheduler task: $taskName" -ForegroundColor Green
} catch {
    Write-Host "No scheduled task named $taskName" -ForegroundColor Gray
}
Write-Host ""
Write-Host "To fully clean up, delete the folder:"
Write-Host "  Remove-Item -Recurse -Force '$here'"
