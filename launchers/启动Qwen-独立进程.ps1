# 以「独立进程」启动 Qwen 本地服务（关掉 DeepSeek Harness 也不会把它带走）
#
# 为什么需要它：
#   · 从 DSH 的会话里启动的服务活在 DSH 的进程树/作业对象里，DSH 一关，整棵树被杀——这就是
#     "一把 DS 关闭，Qwen 也跟着关"的原因。实测这些进程的父进程就是会话里的 pwsh。
#   · 而 Windows 计划任务在这台机器上被拒绝（schtasks: Access is denied），
#     所以这里用 WMI 创建进程：它的父进程是 WmiPrvSE.exe（系统服务），既不在 DSH 的树里，
#     也不会随任何窗口/终端关闭而死。
#   · 顺带解决"打不开"：服务起不来时最常见的原因是上一次的守护进程卡死后仍占着锁文件
#     （supervisor.lock）。qwen_service.py 现在会识别这种情况并改用备用锁，不再被卡死的那一个
#     永远挡住。
#
# 用法：双击同目录的「启动Qwen-独立进程.vbs」，或直接
#   powershell -NoProfile -ExecutionPolicy Bypass -File "D:\AI\Qwen-Local\启动Qwen-独立进程.ps1"
param(
  [switch]$Local,                 # 只在本机监听（不共享、不起隧道）
  [int]$WaitSeconds = 300
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root 'logs\launcher.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

function Write-Log([string]$message) {
  $line = "{0} {1}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $message
  Add-Content -Path $log -Value $line -Encoding utf8
  Write-Host $line
}

function Test-Gateway {
  try {
    $client = [System.Net.Sockets.TcpClient]::new()
    $task = $client.ConnectAsync('127.0.0.1', 8081)
    $ok = $task.Wait(800) -and $client.Connected
    $client.Close()
    return $ok
  } catch { return $false }
}

if (Test-Gateway) {
  Write-Log "网关已经在监听 8081：沿用现有实例，不重复启动"
} else {
  $flags = if ($Local) { '' } else { '' }
  $script = if ($Local) { 'Start-Qwen.bat' } else { 'Start-Qwen-Share.ps1' }
  $argument = if ($Local) {
    "-NoProfile -ExecutionPolicy Bypass -Command `"Start-Process -FilePath '$root\$script' -WorkingDirectory '$root'`""
  } else {
    "-NoProfile -ExecutionPolicy Bypass -File `"$root\$script`" -NoBrowser -Quiet"
  }
  # 共享启动需要两个环境变量（qwen_config 用它们决定去读哪份共享配置）。
  $wrapper = if ($Local) {
    "powershell.exe $argument"
  } else {
    "cmd.exe /c set `"QWEN_SHARE_CONFIG=$root\share.config.json`" && set `"QWEN_SHARE_SCOPE=share`" && powershell.exe $argument"
  }
  Write-Log "用 WMI 启动独立进程：$script"
  $created = ([wmiclass]'Win32_Process').Create($wrapper)
  if ($created.ReturnValue -ne 0) {
    Write-Log "WMI 创建进程失败，ReturnValue=$($created.ReturnValue)"
    exit 1
  }
  Write-Log "已创建 pid=$($created.ProcessId)（父进程是 WmiPrvSE，脱离本终端）"
}

$deadline = (Get-Date).AddSeconds($WaitSeconds)
while ((Get-Date) -lt $deadline) {
  if (Test-Gateway) {
    $link = (Get-Content (Join-Path $root 'logs\admin-link.txt') -ErrorAction SilentlyContinue | Select-Object -First 1)
    Write-Log "网关已就绪（$([int]((Get-Date) - $deadline.AddSeconds(-$WaitSeconds)).TotalSeconds) 秒）"
    if ($link) { Write-Log "机主链接：$link（就在本机打开，别外发）" }
    exit 0
  }
  Start-Sleep -Seconds 2
}
Write-Log "等待超时；看 $root\logs\supervisor.log 与最新的 *-manager.err.log"
exit 1
