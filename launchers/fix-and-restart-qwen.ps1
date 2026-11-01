# Repair + restart for the local Qwen app, in one elevated run.
#
# Why this exists: swapping the 27B model also swapped its llama-server binary (stock -> the
# PrismML fork). When the manager was stopped, the old backend survived as an orphan holding
# port 8082, and the manager's own orphan cleanup refuses to kill it -- that cleanup only
# matches a backend whose executable AND model equal the currently selected profile's, so a
# previous model is deliberately left alone instead of being mistaken for a foreign process.
# An occupied 8082 makes the next backend fail to bind, so the leftover has to go by hand.
#
# Run through fix-and-restart-qwen.bat (it checks for elevation first). ASCII only on purpose:
# Windows PowerShell 5.1 reads a .ps1 without a BOM as ANSI, so non-ASCII text here would show
# up as mojibake in the console.
param([switch]$DryRun)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Stop-One([int]$ProcessId, [string]$Why) {
    if (-not $ProcessId) { return }
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $process) { Write-Host ("  pid {0} is already gone ({1})" -f $ProcessId, $Why); return }
    Write-Host ("  stopping pid {0} = {1} ({2})" -f $ProcessId, $process.ProcessName, $Why)
    try {
        Stop-Process -Id $ProcessId -Force -ErrorAction Stop
        Write-Host "    ok"
    } catch {
        Write-Host ("    FAILED: {0}" -f $_.Exception.Message) -ForegroundColor Red
    }
}

Write-Host "=== 1. free port 8082 (leftover backend) ==="
foreach ($holder in @(Get-NetTCPConnection -State Listen -LocalPort 8082 -ErrorAction SilentlyContinue).OwningProcess) {
    Stop-One ([int]$holder) 'listening on 8082'
}

Write-Host "=== 2. stop the recorded supervisor / manager / backend ==="
foreach ($name in 'supervisor.process.json', 'manager.process.json', 'supervisor-manager.process.json', 'backend.process.json') {
    $record_path = Join-Path $root $name
    if (-not (Test-Path $record_path)) { continue }
    try { $record = Get-Content $record_path -Raw | ConvertFrom-Json } catch { continue }
    foreach ($key in 'pid', 'manager_pid') {
        if ($record.PSObject.Properties.Name -contains $key) { Stop-One ([int]$record.$key) $name }
    }
}

Start-Sleep -Seconds 3

Write-Host "=== 3. port status ==="
$busy = $false
foreach ($port in 8081, 8082) {
    $owner = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue).OwningProcess
    if ($owner) {
        Write-Host ("  port {0} STILL HELD by pid {1}" -f $port, ($owner -join ',')) -ForegroundColor Red
        $busy = $true
    } else {
        Write-Host ("  port {0} free" -f $port) -ForegroundColor Green
    }
}

if ($busy) {
    Write-Host "Something still holds a Qwen port, so the app was NOT started." -ForegroundColor Red
    Write-Host "Close that process (Task Manager shows the pid above) and run this again." -ForegroundColor Red
    exit 1
}

if ($DryRun) {
    Write-Host "=== dry run: ports are clear, but the app was not started ===" -ForegroundColor Yellow
    exit 0
}

Write-Host "=== 4. starting Qwen ==="
& (Join-Path $root 'Start-Qwen.bat')
Write-Host "=== done: the chat page opens by itself, first load of the 27B takes 20-40 s ==="
exit 0
