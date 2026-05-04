$ErrorActionPreference = "Stop"
$here = $PSScriptRoot

Write-Host ""
Write-Host "relay installer" -ForegroundColor Cyan
Write-Host "================" -ForegroundColor Cyan
Write-Host ""

# 1. Venv
if (-not (Test-Path "$here\.venv")) {
    Write-Host "Creating Python venv..." -ForegroundColor Yellow
    py -3.12 -m venv "$here\.venv"
} else {
    Write-Host "Venv already exists" -ForegroundColor Gray
}

# 2. Dependencies
Write-Host "Installing dependencies..." -ForegroundColor Yellow
& "$here\.venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
& "$here\.venv\Scripts\pip.exe" install -r "$here\requirements.txt"

# 3. Config
if (-not (Test-Path "$here\config.json")) {
    Copy-Item "$here\config.json.example" "$here\config.json"
    Write-Host "Created config.json from example" -ForegroundColor Yellow
    Write-Host "  NOTE: EDIT $here\config.json to fill in Discord token + channel_id" -ForegroundColor Magenta
}

# 4. State folder
if (-not (Test-Path "$here\state")) {
    New-Item -ItemType Directory -Path "$here\state" | Out-Null
    New-Item -ItemType Directory -Path "$here\state\logs" | Out-Null
}

# 5. Task Scheduler registration
$folderName = Split-Path $here -Leaf
$taskName = "ClaudeRelay-$folderName"
$pythonw = "$here\.venv\Scripts\pythonw.exe"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m runner" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Days 365) `
    -Hidden `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host ""
Write-Host "Task Scheduler task registered: $taskName" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Edit config.json -- set plugins.discord.token and channel_id"
Write-Host "  2. Edit CLAUDE.md if you want to customise the persona"
Write-Host "  3. Start now:   Start-ScheduledTask -TaskName '$taskName'"
Write-Host "     Or log out/in to trigger on next login."
Write-Host ""
Write-Host "  Logs:     $here\state\logs"
Write-Host "  Stop:     Stop-ScheduledTask -TaskName '$taskName'"
Write-Host '  Uninstall: .\uninstall.ps1'
Write-Host ""
