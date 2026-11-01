<#
.SYNOPSIS
    共享入口：启动服务与公网隧道，然后打开集成的"共享面板"。

.DESCRIPTION
    桌面「Qwen 共享面板」指向本脚本。它做三件事，然后就不再打扰你：

      1. 放行防火墙（校园网被识别为 Public，不放行局域网连不上）——需要管理员权限
      2. 启动服务；共享还没配置过时先生成口令与配置
      3. 用本机管理员令牌打开控制台，右下角就是集成面板：谁在连接、当前口令、
         共享链接、踢人、换口令、一键断联，都在那一个面板里

    不想共享时请走老入口（桌面 Start-Qwen.bat），那个入口不会开隧道，
    也不会读取共享配置。
#>
param(
    [switch]$Share,
    [switch]$NoBrowser,
    [string]$PanelUrl,
    [switch]$Quiet,
    # 由桌面「Qwen 共享面板」入口使用：先把自己交给 WMI 重启一份脱离调用者进程树的副本，
    # 再原地等待网关就绪，并把结果写进 logs\panel-entry.json。见下方「脱离启动」一节。
    [switch]$Detached
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$shareScript = Join-Path $PSScriptRoot 'Start-Qwen-Share.ps1'
$shareConfig = Join-Path $PSScriptRoot 'share.config.json'
$adminLinkFile = Join-Path $PSScriptRoot 'logs\admin-link.txt'
$startScript = Join-Path $PSScriptRoot 'Start-Qwen.ps1'
$gatewayPort = 8081


function Write-Say([string]$text, [string]$color = 'Gray') {
    if (-not $Quiet) { Write-Host $text -ForegroundColor $color }
}
function Write-Step([string]$text) { Write-Say "==> $text" 'Cyan' }
function Write-Ok([string]$text) { Write-Say "OK  $text" 'Green' }
function Write-Warn([string]$text) { Write-Say "!!  $text" 'Yellow' }

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-GatewayListening {
    <#
        端口层面的「网关在不在」，不带任何业务语义：409 之类都算在监听。
        用原始 socket 而不是 Invoke-WebRequest，是因为这里要区分「没人监听」（网关没起来）
        和「有监听但拒绝请求」（网关起来了、只是不接受未登录访问），后者也是成功。
    #>
    $client = $null
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $connect = $client.ConnectAsync('127.0.0.1', $gatewayPort)
        return ($connect.Wait(800) -and $client.Connected)
    } catch {
        return $false
    } finally {
        if ($null -ne $client) { $client.Close() }
    }
}

# ---------------------------------------------------------------- 脱离启动
# 为什么必须绕这一圈：从 DeepSeek Harness（或它启动的任何终端）里直接拉起服务时，
# 服务整条进程链都挂在那棵进程树上。DSH 一退出，无论是它自己关、还是被 taskkill /T
# 连树带走，网关、管理器、llama-server 全部跟着死——这正是「DS 一断连，面板跟着断联」。
# Win32_Process.Create 让新进程的父进程变成 WmiPrvSE.exe（系统服务），既不在任何终端树
# 里，也不在任何控制台作业对象里；同时 supervisor 自己就是 DETACHED_PROCESS，于是整条
# 服务链与调用者彻底解耦。
if ($Detached) {
    $panelResultFile = Join-Path $PSScriptRoot 'logs\panel-entry.json'
    try {
        if (Test-GatewayListening) {
            # Already serving: leave it exactly as it is. Re-running the share script would
            # re-read the share configuration and, when the listener looks wrong, restart
            # the gateway -- interrupting the owner for no reason.
            Write-Say "网关已在监听 $gatewayPort，沿用现有实例" 'Green'
        } else {
            $child = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`"" +
                     " -Share -Quiet -NoBrowser -PanelUrl `"$PanelUrl`""
            $created = ([wmiclass]'Win32_Process').Create("powershell.exe $child")
            if ($created.ReturnValue -ne 0) {
                Write-Host "WMI 创建脱离进程失败，ReturnValue=$($created.ReturnValue)"
                exit 1
            }
            Write-Say "已创建脱离进程 pid=$($created.ProcessId)（父进程是 WmiPrvSE，不随本进程树退出）" 'DarkGray'

            # Wait here so that "the gateway came up" is an observable result: the panel
            # entry reads the file written below and decides whether to report a failure.
            $deadline = (Get-Date).AddSeconds(120)
            while ((Get-Date) -lt $deadline) {
                if (Test-GatewayListening) { break }
                Start-Sleep -Milliseconds 700
            }
            if (Test-GatewayListening) { Write-Say "网关已就绪：http://127.0.0.1:$gatewayPort/" 'Green' }
            else { Write-Say "等待网关超时（120 秒）" 'Yellow' }
        }
        $ready = Test-GatewayListening
        # Written atomically (temp + move) and never deleted beforehand: two double-clicks make two
        # of these run at once, and one deleting or half-writing the verdict while the other reads
        # it is what produced "run returned 1" and a stale/false "not ready" for the panel.
        $verdict = @{ ready = $ready; port = $gatewayPort; at = (Get-Date).ToString('s') } |
            ConvertTo-Json -Compress
        $stamp = Join-Path $PSScriptRoot ("logs\panel-entry.{0}.tmp" -f $PID)
        Set-Content -LiteralPath $stamp -Value $verdict -Encoding UTF8
        Move-Item -LiteralPath $stamp -Destination $panelResultFile -Force
        if ($ready) { exit 0 } else { exit 1 }
    } catch {
        Write-Host "脱离启动失败：$($_.Exception.Message)"
        exit 1
    }
}

# ---------------------------------------------------------------- 启动
# This entry point always means "shared": the local-only入口 is the original
# Start-Qwen.bat shortcut. The first click creates the share configuration, so there
# is no separate setup step to remember.
if ($Share -and (Test-Path -LiteralPath $shareScript)) {
    if (-not (Test-Admin)) {
        Write-Warn '当前不是管理员，防火墙放行会被跳过（局域网用户可能连不上）。'
        Write-Warn '如需局域网访问：右键桌面「Qwen 共享面板」→ 以管理员身份运行。'
    }
    Write-Step '启动服务并确保共享开启'
    if ($Detached) {
        # 脱离副本只负责把服务拉起来，不该再等隧道：隧道由网关自己在启动后接管
        # （模式服务会注册并守护 QwenCloudflaredTunnel），在这里再等一次只会让
        # 「双击后面板迟迟不开」多出十几秒到一分多钟的等待。
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $shareScript -NoTunnel -Quiet
    } else {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $shareScript
    }
} else {
    Write-Step '启动本机服务（不共享）'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startScript -NoBrowser
}

# ---------------------------------------------------------------- 等待网关
Write-Step '等待服务响应'
$port = 8081
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:$port/local/status" -UseBasicParsing -TimeoutSec 3 | Out-Null
        $ready = $true
        break
    } catch {
        # 401 is the expected answer while a passphrase is configured: it proves the
        # gateway is up and the gate is working, which is all this loop waits for.
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 401) {
            $ready = $true
            break
        }
    }
    Start-Sleep -Milliseconds 500
}
if ($ready) { Write-Ok '网关已响应' } else { Write-Warn '网关暂未响应，仍会尝试打开面板' }

# ---------------------------------------------------------------- 打开集成面板
$target = $PanelUrl
if (-not $target -and (Test-Path -LiteralPath $adminLinkFile)) {
    $link = (Get-Content -LiteralPath $adminLinkFile -Raw).Trim()
    if ($link -match '^https?://') { $target = $link }
}
if (-not $target) {
    $target = "http://127.0.0.1:$port/"
    Write-Warn "未找到管理员链接（$adminLinkFile），将以普通方式打开。"
    Write-Warn '如果看不到「共享管理器」，说明当前浏览器不是管理员身份。'
}

if (-not $NoBrowser) {
    Write-Step '打开共享面板'
    Start-Process $target
    Write-Say ''
    Write-Say '面板已打开：右下角「共享管理器」里有连接列表、当前口令、共享链接、' 'White'
    Write-Say '踢人、换口令和一键断联。这个窗口可以直接关掉，服务和隧道会继续运行。' 'White'
    Write-Say '要从面板里彻底结束本机服务，点面板里的「停止共享并退出」。' 'DarkGray'
} else {
    Write-Say "共享面板：$target" 'White'
}
