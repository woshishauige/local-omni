$ErrorActionPreference = 'Stop'
$queue = Invoke-RestMethod 'http://127.0.0.1:8188/queue' -TimeoutSec 5
if ($queue.queue_running.Count -gt 0 -or $queue.queue_pending.Count -gt 0) {
    throw 'ComfyUI still has jobs; restart cancelled.'
}
$listener = Get-NetTCPConnection -LocalPort 8188 -State Listen
$serverProcessId = $listener.OwningProcess | Select-Object -Unique
$serverProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$serverProcessId"
if ($serverProcess.ExecutablePath -ne 'D:\AI\ComfyUI-BSAI_v010\python\python.exe' -or
    $serverProcess.CommandLine -notmatch 'ComfyUI-H3-Multishot') {
    throw 'Port 8188 does not belong to the deployed H3 server.'
}
Stop-Process -Id $serverProcessId -Force
Wait-Process -Id $serverProcessId -ErrorAction SilentlyContinue
Copy-Item -LiteralPath 'D:\AI\deployment\logs\comfy-startup.log' -Destination 'D:\AI\deployment\logs\comfy-before-restart.log'
Copy-Item -LiteralPath 'D:\AI\deployment\logs\comfy-startup.err.log' -Destination 'D:\AI\deployment\logs\comfy-before-restart.err.log'
$launcher = Start-Process -FilePath 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', 'D:\AI\ComfyUI-BSAI_v010\Start-H3-12GB.ps1', '-NoBrowser'
) -WorkingDirectory 'D:\AI\ComfyUI-BSAI_v010' -WindowStyle Hidden -RedirectStandardOutput 'D:\AI\deployment\logs\comfy-startup.log' -RedirectStandardError 'D:\AI\deployment\logs\comfy-startup.err.log' -PassThru
Write-Output "H3 launcher PID: $($launcher.Id)"
