<#
.SYNOPSIS
    把 Qwen UI 共享给外网电脑：内网直连 + Cloudflare 隧道，并开启口令保护。

.DESCRIPTION
    共享配置写在 share.config.json 里，不改动 settings.json，所以随时可以用
    Stop-Qwen-Share.ps1 完全撤销。脚本会依次完成：
      1. 让服务监听局域网地址，并生成/复用一个访问口令
      2. 重启 Qwen 服务使配置生效
      3. 需要时下载 cloudflared 并建立公网隧道
      4. 打印给他人使用的网址和口令

    局域网访问需要在防火墙放行端口，这一步需要管理员权限；没有管理员权限时会
    跳过并给出提示，隧道部分仍然可用。

.PARAMETER Pin
    指定访问口令。不传则复用 share.config.json 里的旧口令，仍然没有就生成一个。

.PARAMETER NoTunnel
    只共享给同一局域网，不建立公网隧道。

.PARAMETER Regenerate
    忽略已有口令，重新生成一个（用于口令外泄后更换）。
#>
param(
    [string]$Pin,
    [switch]$NoTunnel,
    [switch]$Regenerate,
    [switch]$Quiet

)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$configPath = Join-Path $PSScriptRoot 'share.config.json'
$cloudflared = Join-Path $PSScriptRoot 'tools\cloudflared.exe'
$downloadUrl = 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe'
$python = $env:QWEN_PYTHON
if (-not $python) { $python = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\ComfyUI-BSAI_v010\python\python.exe')) }
$port = 8081


function Write-Say([string]$text, [string]$color = 'Gray') {
    if (-not $Quiet) { Write-Host $text -ForegroundColor $color }
}
function Write-Step([string]$text) { Write-Say "==> $text" 'Cyan' }
function Write-Warn([string]$text) { Write-Say "!!  $text" 'Yellow' }
function Write-Ok([string]$text) { Write-Say "OK  $text" 'Green' }

function Get-RandomPin {
    # 6 digits from the OS CSPRNG: easy to read out loud, and short-lived because
    # the tunnel address is disposable.
    Add-Type -TypeDefinition 'public static class QwenPin { public static int Next(int m){ byte[] b=new byte[4]; System.Security.Cryptography.RandomNumberGenerator.Create().GetBytes(b); return (int)(System.BitConverter.ToUInt32(b,0) % (uint)m); } }'
    $a = 100000 + [QwenPin]::Next(900000)
    $b = 100000 + [QwenPin]::Next(900000)
    $c = 100000 + [QwenPin]::Next(900000)
    return "$a-$b-$c"
}

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Write-ShareConfig([string]$Path, [string]$Token, [string[]]$Names, [int]$ListenPort) {
    # Built by hand instead of ConvertTo-Json: Windows PowerShell 5.1 escapes every
    # non-ASCII character and System.Text.Json is not loadable there, so the file
    # would become unreadable for whoever maintains it.
    $lines = @('{')
    $lines += '  "listen_host": "0.0.0.0",'
    $lines += "  `"listen_port`": $ListenPort,"
    $lines += "  `"access_token`": `"$Token`","
    $lines += '  "allowed_hosts": ['
    for ($i = 0; $i -lt $Names.Count; $i++) {
        $comma = if ($i -lt $Names.Count - 1) { ',' } else { '' }
        $lines += "    `"$($Names[$i])`"$comma"
    }
    $lines += '  ],'
    $lines += '  "note": "由 Start-Qwen-Share.ps1 生成；删除本文件并重启服务即可停止共享。"'
    $lines += '}'
    [IO.File]::WriteAllText($Path, ($lines -join "`r`n"), (New-Object Text.UTF8Encoding($false)))
}

function Invoke-QwenService([string]$Action) {
    # The service writes progress to stderr, which Windows PowerShell turns into a
    # terminating error under ErrorActionPreference=Stop and would abort this
    # script halfway through applying the share. The exit code is the real signal.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = & $python -X utf8 (Join-Path $PSScriptRoot 'qwen_service.py') $Action 2>&1
        # Read the exit code before any formatting pipeline, or it would already
        # belong to that pipeline instead of to python.
        $code = $LASTEXITCODE
        $text = ($lines | ForEach-Object { "$_" }) -join "`n"
        return [pscustomobject]@{ ExitCode = $code; Output = $text }
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Get-LanAddress {
    $candidates = @()
    foreach ($adapter in (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue)) {
        $ip = $adapter.IPAddress
        if ($adapter.InterfaceAlias -like '*Loopback*') { continue }
        if ($ip -like '127.*' -or $ip -like '169.254.*') { continue }
        if ($ip -like '172.1?.*' -or $ip -like '172.2?.*' -or $ip -like '172.3?.*') { continue }
        # Prefer a real Wi-Fi/Ethernet NIC over Hyper-V and VPN virtual switches.
        $rank = 2
        if ($adapter.InterfaceAlias -match 'WLAN|Wi-Fi|Ethernet|以太网|无线') { $rank = 0 }
        elseif ($adapter.InterfaceAlias -match 'vEthernet|Virtual|Loopback|Bluetooth|蓝牙') { $rank = 3 }
        $candidates += [pscustomobject]@{ IP = $ip; Rank = $rank }
    }
    $best = $candidates | Sort-Object Rank | Select-Object -First 1
    if ($null -eq $best) { return $null }
    return $best.IP
}

# ---------------------------------------------------------------- 访问口令
$previousPin = ''
$config = $null
if (Test-Path -LiteralPath $configPath) {
    try { $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $config = $null }
}
if ($null -ne $config -and $config.access_token) { $previousPin = [string]$config.access_token }
if ($Regenerate) { $Pin = '' }
if ([string]::IsNullOrWhiteSpace($Pin)) { $Pin = $previousPin }
$reused = -not [string]::IsNullOrWhiteSpace($Pin)
if (-not $reused) { $Pin = Get-RandomPin }

# ---------------------------------------------------------------- 监听地址
$lanAddress = Get-LanAddress
if ($null -eq $lanAddress) {
    Write-Warn '没有找到可用的局域网 IPv4 地址，只能通过隧道访问。'
} else {
    Write-Ok "局域网地址：$lanAddress"
}

# ---------------------------------------------------------------- 写入共享配置
$allowedNames = @('*.trycloudflare.com', '*.cfargotunnel.com')
if ($null -ne $lanAddress) { $allowedNames += $lanAddress }
Write-ShareConfig -Path $configPath -Token $Pin -Names $allowedNames -ListenPort $port
Write-Ok "共享配置已写入 share.config.json（settings.json 未被改动）"

# ---------------------------------------------------------------- 局域网放行
if (Test-Admin) {
    $ruleName = "Qwen-Local gateway $port (LAN share)"
    # Windows blocks inbound traffic on networks it classifies as Public, which is
    # how campus and guest Wi-Fi is classified. The rule therefore names the
    # current profiles explicitly instead of assuming a Private network; the
    # passphrase gate still stands in front of every request it lets through.
    $profiles = @(Get-NetConnectionProfile -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty NetworkCategory -Unique)
    if ($profiles.Count -eq 0) { $profiles = @('Private', 'Public') }
    Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $port -Profile $profiles -ErrorAction SilentlyContinue | Out-Null
    Write-Ok "已放行防火墙 TCP $port（配置：$($profiles -join ', ')）"
} else {
    Write-Warn "未以管理员身份运行，跳过防火墙放行。局域网用户如无法访问，请右键以管理员身份重跑本脚本。"
}

# ---------------------------------------------------------------- 重启服务
Write-Step '让 Qwen 服务应用共享配置'
$env:QWEN_SHARE_CONFIG = $configPath
$env:QWEN_LISTEN_HOST = '0.0.0.0'
# 口径必须一起导出：监督进程会把它写进 `supervisor.process.json`，之后 `qwen_service.py stop`
# 就是靠这个字段判断"这个监督进程是不是我这次要的口径"。少了这一行，从桌面「Qwen 共享面板」
# 起来的服务会被记成"未知"口径——共享其实是好的，但停止/重启会以"口径不同、无法结束"拒绝，
# 而且那个监督进程若是管理员身份起的，非管理员的 stop 根本碰不到它（实测两次都卡在这）。
$env:QWEN_SHARE_SCOPE = 'share'
# The passphrase IS exported, for one specific reason: the supervisor logs in with
# it so the gate answers its heartbeat instead of returning 401, which the
# supervisor would read as a dead gateway and restart endlessly. It cannot pin a
# rotation: the service only adopts this value into an empty store, so a passphrase
# changed in the manager panel stays in force across restarts.
$env:QWEN_ACCESS_TOKEN = $Pin

# The passphrase, host allowlist and origin rules are read per request, so a
# gateway that already listens on every interface needs no restart at all.
# Restarting only when the listener is wrong keeps a re-run of this script from
# interrupting whatever the owner is doing.
$listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
$alreadyShared = $false
if ($null -ne $listening -and $listening.LocalAddress -contains '0.0.0.0') {
    # Try the passphrase this script would install, then the one the service kept.
    # The service's own store wins after a rotation made in the manager panel, and
    # that is not a reason to restart a gateway that is already serving.
    foreach ($candidate in @($Pin, $previousPin) | Where-Object { $_ }) {
        try {
            $probe = (Invoke-WebRequest -Uri "http://127.0.0.1:$port/local/status?k=$candidate" `
                -UseBasicParsing -TimeoutSec 5).Content | ConvertFrom-Json
            if ($probe.service -eq 'qwen-local-modes') {
                $alreadyShared = $true
                break
            }
        } catch {
            continue
        }
    }
}
if ($alreadyShared) {
    Write-Ok '服务已在共享模式运行，无需重启'
    $state = 'unchanged'
} else {
    # The service is started through the shared launcher with -Share, which is what
    # seeds the passphrase and marks the scope. The tunnel needs no separate start
    # here: the manager owns the hidden task, the windowless launcher and the keeper,
    # and starting it from two places is what previously let the task reap it.
    Write-Step '启动服务（共享模式）'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
        (Join-Path $PSScriptRoot 'Start-Qwen.ps1') -Share -NoBrowser -Quiet
    if ($LASTEXITCODE -ne 0) {
        throw 'Qwen 启动失败，详见 Qwen-Local\logs\ 下的日志。'
    }
    $state = 'started'
    Write-Ok "服务状态：$state"
}

# 确认网关真的在局域网地址上应答，并把口令门当作验收条件
Start-Sleep -Seconds 2
$gateChecked = $false
if ($null -ne $lanAddress) {
    $probe = "http://${lanAddress}:$port/local/status"
    try {
        $response = Invoke-WebRequest -Uri $probe -UseBasicParsing -TimeoutSec 5
        Write-Warn "网关在 $probe 上应答但未要求口令，请检查 share.config.json。"
        $gateChecked = $true
    } catch {
        $code = $null
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        if ($code -eq 401) {
            Write-Ok "口令门生效（$probe 返回 401，未登录不可访问）"
            $gateChecked = $true
        } elseif ($null -ne $code) {
            Write-Warn "网关返回 HTTP $code，若不是 401 请查看 logs\ 下的日志。"
            $gateChecked = $true
        }
    }
    if (-not $gateChecked) {
        Write-Warn "无法访问 $probe —— 多为防火墙未放行，请以管理员身份重跑本脚本。"
    }
}

# ---------------------------------------------------------------- 公网隧道
if ($NoTunnel) {
    Write-Say ''
    Write-Say '仅局域网共享已开启（未建立公网隧道）。' 'White'
    if ($null -ne $lanAddress) {
        Write-Say "同一 WiFi 下的电脑打开： http://${lanAddress}:$port/" 'White'
    }
    Write-Say "访问口令： $Pin" 'White'
    return
}

if (-not (Test-Path -LiteralPath $cloudflared)) {
    Write-Step '下载 cloudflared（约 50 MB，仅首次；实测慢速线路需要十几分钟）'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $cloudflared) | Out-Null
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $downloadUrl -OutFile $cloudflared -UseBasicParsing
    Write-Ok "已下载：$cloudflared"
}

# 上一次的隧道如果还活着，就**沿用**它。
#
# quick tunnel 的名字是每次新建时随机给的：杀掉正在跑的 cloudflared 等于把已经发出去的链接
# 作废（所有者反馈："共享网站 … 打不开了"——实测那个域名在 DNS 上已经不存在，而同一时刻新隧道
# 的地址是好的）。所以这里只在"确实没有可用的隧道"时才清理遗留进程。
$tunnelAlive = $false
try {
    $shareStatus = Invoke-WebRequest -Uri "http://127.0.0.1:$port/local/share/status" `
        -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
    $tunnelAlive = ($shareStatus.Content | ConvertFrom-Json).online -eq $true
} catch {
    $tunnelAlive = $false
}
$staleTunnels = @(Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -like "*127.0.0.1:$port*" -or
        ($_.CommandLine -like '*--url*' -and $_.CommandLine -like '*127.0.0.1*')
    })
if ($tunnelAlive -and $staleTunnels.Count -gt 0) {
    Write-Ok "沿用正在运行的公网隧道（地址不变：$((Invoke-WebRequest -Uri "http://127.0.0.1:$port/local/share/status" -UseBasicParsing -TimeoutSec 5).Content | ConvertFrom-Json).url)"
} else {
    # 结束上一次遗留的隧道，避免同名隧道重复堆积
    $staleTunnels | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

$logDir = Join-Path $PSScriptRoot 'logs'
if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
$tunnelLog = Join-Path $logDir 'cloudflared.log'

function Wait-Tunnel {
    # The tunnel is started and kept alive by the service manager itself, which owns
    # the hidden scheduled task, the windowless VBS launcher and the keeper process.
    # Doing it here too would mean two places racing to start the same tunnel, and an
    # earlier version of that race is what let the task reap the tunnel on start.
    for ($i = 0; $i -lt 90; $i++) {
        $online = $false
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/local/share/status" `
                -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
            $online = ($response.Content | ConvertFrom-Json).online -eq $true
        } catch {
            # 403/401 before the owner cookie exists; fall back to the process table.
            $running = @(Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -like "*127.0.0.1:$port*" })
            $online = $running.Count -gt 0
        }
        if ($online) {
            Write-Ok '公网隧道已就绪'
            return $true
        }
        Start-Sleep -Seconds 1
    }
    Write-Warn '隧道暂未就绪；面板里的"恢复连接"可以随时重试。'
    return $false
}

Wait-Tunnel

# The tunnel prints its random public hostname into its own log, so that log is the
# only place the address can be read from. The panel reads it too, so this is only
# for the summary printed below.
$publicUrl = $null
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 1000
    if (-not (Test-Path -LiteralPath $tunnelLog)) { continue }
    $text = Get-Content -LiteralPath $tunnelLog -Raw -ErrorAction SilentlyContinue
    if ($text -match 'https://[a-z0-9-]+\.trycloudflare\.com') {
        $publicUrl = $Matches[0]
        break
    }
}

Write-Say ''
if ($null -eq $publicUrl) {
    Write-Warn "未能取得隧道网址，详见 $tunnelLog"
    Write-Say '局域网共享已生效，可直接使用上面的局域网地址。'
    return
}

# 隧道就绪后，从公网侧确认口令门真的挡住了未登录访问
Start-Sleep -Seconds 3
try {
    Invoke-WebRequest -Uri "$publicUrl/local/status" -UseBasicParsing -TimeoutSec 20 | Out-Null
    Write-Warn '公网地址未要求口令，请立即检查 share.config.json 后重试。'
} catch {
    $code = $null
    if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
    if ($code -eq 401) { Write-Ok '公网侧口令门已验证（未登录返回 401）' }
    else { Write-Warn "公网侧返回 HTTP $code，请自行打开网址确认。" }
}

Write-Say ''
Write-Say '================= 共享已开启 =================' 'Green'
Write-Say "公网地址： $publicUrl" 'White'
Write-Say "访问口令： $Pin" 'White'
Write-Say "局域网地址： http://${lanAddress}:$port/" 'White'
Write-Say ''
Write-Say '把网址和口令发给对方，第一次打开会要求输入口令，之后该设备自动记住。'
Write-Say '注意：这是任何人拿到网址都需要口令的临时地址，请勿把网址连同口令公开发布。'
Write-Say '关闭共享：运行 .\Stop-Qwen-Share.ps1'
Write-Say '==============================================' 'Green'
