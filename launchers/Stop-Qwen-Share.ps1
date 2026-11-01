<#
.SYNOPSIS
    停止共享，把服务恢复成只有本机能访问的状态。

.DESCRIPTION
    依次关闭公网隧道（注销计划任务）、删除共享配置 share.config.json、
    移除防火墙放行规则，然后重启服务。settings.json 和其他个人配置不受影响。
#>
param()

$ErrorActionPreference = 'Stop'

# This file lives in launchers/; the project root is one level up.
$root = Split-Path -Parent $PSScriptRoot

Set-Location -LiteralPath $root

$configPath = Join-Path $root 'share.config.json'
$python = $env:QWEN_PYTHON
if (-not $python) { $python = [IO.Path]::GetFullPath((Join-Path $root '..\ComfyUI-BSAI_v010\python\python.exe')) }
$port = 8081

function Write-Ok([string]$text) { Write-Host "OK  $text" -ForegroundColor Green }
function Write-Warn([string]$text) { Write-Host "!!  $text" -ForegroundColor Yellow }

function Invoke-QwenService([string]$Action) {
    # Progress goes to stderr, which Windows PowerShell would turn into a
    # terminating error under ErrorActionPreference=Stop. Exit code is the signal.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = & $python -X utf8 (Join-Path $root 'qwen_service.py') $Action 2>&1
        # Read the exit code before any formatting pipeline captures it.
        $code = $LASTEXITCODE
        $text = ($lines | ForEach-Object { "$_" }) -join "`n"
        return [pscustomobject]@{ ExitCode = $code; Output = $text }
    } finally {
        $ErrorActionPreference = $previous
    }
}

# 先记下可能用到的局域网地址，用于最后提示
$lanAddress = $null
foreach ($adapter in (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue)) {
    if ($adapter.IPAddress -like '127.*' -or $adapter.IPAddress -like '169.254.*') { continue }
    if ($adapter.InterfaceAlias -match 'vEthernet|Virtual|Loopback|Bluetooth|蓝牙') { continue }
    if ($adapter.InterfaceAlias -match 'WLAN|Wi-Fi|Ethernet|以太网|无线') { $lanAddress = $adapter.IPAddress; break }
}

# ---------------------------------------------------------------- 关闭隧道
$stopped = 0
$task = Get-ScheduledTask -TaskName 'QwenCloudflaredTunnel' -ErrorAction SilentlyContinue
if ($null -ne $task) {
    Stop-ScheduledTask -TaskName 'QwenCloudflaredTunnel' -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName 'QwenCloudflaredTunnel' -Confirm:$false -ErrorAction SilentlyContinue
    Write-Ok '已注销隧道计划任务'
}
$pidPath = Join-Path $root 'cloudflared.pid'
if (Test-Path -LiteralPath $pidPath) {
    # Older versions recorded the tunnel as a child process instead of a task.
    $tunnelId = (Get-Content -LiteralPath $pidPath -Raw).Trim()
    if ($tunnelId -match '^\d+$') {
        $process = Get-Process -Id ([int]$tunnelId) -ErrorAction SilentlyContinue
        if ($null -ne $process -and $process.ProcessName -eq 'cloudflared') {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            $stopped++
        }
    }
    Remove-Item -LiteralPath $pidPath -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        # A quick tunnel always carries the local URL flag, whatever port it used.
        $_.CommandLine -like "*127.0.0.1:$port*" -or
        ($_.CommandLine -like '*--url*' -and $_.CommandLine -like '*127.0.0.1*')
    } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $stopped++
    }
Write-Ok "已关闭隧道进程：$stopped 个"

# ---------------------------------------------------------------- 撤销配置
if (Test-Path -LiteralPath $configPath) {
    Remove-Item -LiteralPath $configPath -Force
    Write-Ok '已删除 share.config.json（服务将回到仅本机可访问）'
} else {
    Write-Ok '没有共享配置需要删除'
}

# ---------------------------------------------------------------- 防火墙
$ruleName = "Qwen-Local gateway $port (LAN share)"
$rules = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($null -ne $rules) {
    $rules | Remove-NetFirewallRule -ErrorAction SilentlyContinue
    Write-Ok "已移除防火墙规则：$ruleName"
}

# ---------------------------------------------------------------- 重启服务
Write-Host '==> 重启 Qwen 服务' -ForegroundColor Cyan
Remove-Item Env:\QWEN_SHARE_CONFIG -ErrorAction SilentlyContinue
Remove-Item Env:\QWEN_ACCESS_TOKEN -ErrorAction SilentlyContinue
Remove-Item Env:\QWEN_LISTEN_HOST -ErrorAction SilentlyContinue
try {
    $stopResult = Invoke-QwenService 'stop'
    if ($stopResult.ExitCode -ne 0) {
        Write-Warn "停止旧实例返回 $($stopResult.ExitCode)（将继续启动新实例）"
    }
} catch {
    Write-Warn "停止旧实例时出现问题（将继续启动）：$($_.Exception.Message)"
}
Start-Sleep -Seconds 2
# 等端口真正释放再启动，避免监督进程关停竞态把新实例的启动判成心跳丢失。
$portFree = $false
for ($i = 0; $i -lt 20; $i++) {
    if ($null -eq (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
        $portFree = $true
        break
    }
    Start-Sleep -Milliseconds 500
}
if (-not $portFree) { Write-Warn "端口 $port 仍被占用，继续尝试启动。" }

$startResult = Invoke-QwenService 'start'
$startOutput = $startResult.Output
try { $status = $startOutput | ConvertFrom-Json } catch { $status = $null }
if ($startResult.ExitCode -ne 0 -and $null -eq $status) {
    Write-Host $startOutput
    throw 'Qwen 启动失败，详见 Qwen-Local\logs\ 下的日志。'
}
if ($startResult.ExitCode -ne 0) {
    Write-Warn "启动器返回 $($startResult.ExitCode)，但已拿到状态，继续按实际状态验收。"
}
Write-Ok '服务已恢复，仅本机（127.0.0.1）可访问'
if ($null -ne $lanAddress) {
    Write-Host "提示：局域网地址 http://${lanAddress}:$port/ 现在应当无法访问。" -ForegroundColor White
}
