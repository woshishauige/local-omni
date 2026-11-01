param(
    # Default: local only. Sharing is an explicit opt-in, so the original
    # Start-Qwen.bat entry point keeps starting a loopback-only server.
    [switch]$Share,
    [switch]$NoBrowser,
    [switch]$Quiet

)
$ErrorActionPreference = 'Stop'
# The interpreter is resolved the same way everywhere: QWEN_PYTHON wins, then the
# sibling ComfyUI install this project was developed against. setup.ps1 prints the
# value it found so a wrong environment fails before the model starts loading.
$qwenPython = $env:QWEN_PYTHON
if (-not $qwenPython) { $qwenPython = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../ComfyUI-BSAI_v010/python/python.exe')) }
$qwenService = Join-Path $PSScriptRoot 'qwen_service.py'
$shareConfig = Join-Path $PSScriptRoot 'share.config.json'
$qwenBrowserUrl = 'http://127.0.0.1:8081/?qwen_ui=zh-CN-v5'


function Write-Say([string]$text, [string]$color = 'Gray') {
    if (-not $Quiet) { Write-Host $text -ForegroundColor $color }
}
function Invoke-QwenService([string]$Action) {
    # The service reports progress on stderr, which Windows PowerShell turns into a
    # terminating error under ErrorActionPreference=Stop. This launcher has to keep
    # working in that case, so the exit code is read explicitly instead.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = & $qwenPython -X utf8 $qwenService $Action 2>&1
        $code = $LASTEXITCODE
        $text = ($lines | ForEach-Object { "$_" }) -join "`n"
        return [pscustomobject]@{ ExitCode = $code; Output = $text }
    } finally {
        $ErrorActionPreference = $previous
    }
}

try {
    if (-not (Test-Path -LiteralPath $qwenPython -PathType Leaf)) {
        throw "Qwen Python runtime was not found: $qwenPython"
    }
    if (-not (Test-Path -LiteralPath $qwenService -PathType Leaf)) {
        throw "Qwen service script was not found: $qwenService"
    }

    # Sharing is switched on by Start-Qwen-Share.ps1 (or -Share). Its overlay is
    # reloaded here so starting the service keeps the shared address, the passphrase
    # and the tunnel name allowlist in force instead of silently dropping back to
    # loopback-only. Without -Share the scope is marked local, so a machine that has
    # a share.config.json on disk still starts as a local-only server.
    $sharing = $Share -and (Test-Path -LiteralPath $shareConfig -PathType Leaf)
    if ($sharing) {
        $env:QWEN_SHARE_CONFIG = $shareConfig
        $env:QWEN_SHARE_SCOPE = 'share'
        Write-Say "Sharing configuration found; starting with $shareConfig"
    } else {
        Remove-Item Env:\QWEN_SHARE_CONFIG -ErrorAction SilentlyContinue
        Remove-Item Env:\QWEN_ACCESS_TOKEN -ErrorAction SilentlyContinue
        Remove-Item Env:\QWEN_LISTEN_HOST -ErrorAction SilentlyContinue
        # A marker, not a blank variable: "this start is local only" has to survive
        # the service seeding itself from whatever configuration exists on disk.
        $env:QWEN_SHARE_SCOPE = 'local'
        if ($Share) {
            Write-Say 'Sharing was requested but share.config.json is missing; starting local only.'
        }
    }

    $result = Invoke-QwenService 'start'
    if ($result.ExitCode -ne 0) {
        Write-Say $result.Output
        throw 'Qwen startup failed. See the error above and Qwen-Local/logs.'
    }
    # The status is a single JSON line, but the service also writes progress to stderr and this
    # launcher merges both streams (`2>&1` above). Parsing the merged text therefore failed the
    # moment the service logged anything - which is exactly how a start that succeeded came to be
    # reported as "Qwen did not reach a usable startup state", with the service up and listening a
    # few seconds later. Take the last line that is actually JSON.
    $status = $null
    foreach ($line in ($result.Output -split "`n")) {
        $trimmed = $line.Trim()
        if (-not $trimmed.StartsWith('{')) { continue }
        try {
            $status = $trimmed | ConvertFrom-Json
            break
        } catch {
            $status = $null
        }
    }
    if ($null -eq $status -or $status.state -notin @('ready', 'starting', 'switching', 'waiting')) {
        Write-Say $result.Output
        throw 'Qwen did not reach a usable startup state. See Qwen-Local/logs/supervisor.log.'
    }
    Write-Say "Qwen website is available ($($status.state)): $qwenBrowserUrl"
    if ($sharing) {
        Write-Say 'Sharing is active: the LAN address and the current tunnel URL also serve this page.'
    }
    if ($status.state -ne 'ready') {
        Write-Say 'The model is loading or waiting for ComfyUI. This page will reconnect automatically.'
    }
    if (-not $NoBrowser) { Start-Process $qwenBrowserUrl }
    exit 0
} catch {
    Write-Error $_
    exit 1
}
