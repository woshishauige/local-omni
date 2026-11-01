<#
.SYNOPSIS
    创建两个桌面启动入口：一个共享（带集成面板），一个不共享（老入口）。

.DESCRIPTION
    两个方式互相独立：

      · Qwen 共享面板   —— 启动服务 + 公网隧道，然后打开带管理面板的控制台。
                            需要管理员权限（只为放行防火墙），标记了 UAC。
      · Start-Qwen.bat  —— 老入口，只启动本机服务，不开隧道、不共享。

    用 -Remove 删除本脚本创建的快捷方式。不需要管理员权限即可运行本脚本本身；
    UAC 只在双击"Qwen 共享面板"时弹出。
#>
param(
    [switch]$Remove,
    [string]$PanelUrl
)

$ErrorActionPreference = 'Stop'

# This file lives in launchers/; the project root is one level up.
$root = Split-Path -Parent $PSScriptRoot

Set-Location -LiteralPath $root

$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
$panelLauncher = Join-Path $PSScriptRoot 'Start-Qwen-Panel.vbs'
$localEntry = Join-Path $root 'Start-Qwen.bat'

$shortcuts = @(
    @{
        # wscript.exe, not powershell.exe: this entry has to start with no console
        # window at all, and only the VBS launcher's window style 0 does that. The
        # only thing the owner sees is the browser panel.
        Path = Join-Path $desktop 'Qwen 共享面板.lnk'
        Target = $wscript
        Arguments = "`"$panelLauncher`""
        Admin = $true
        Description = 'Qwen 共享面板（启动服务与隧道并打开管理面板）'
    },
    @{
        Path = Join-Path $desktop 'Start-Qwen.bat - 快捷方式.lnk'
        Target = $localEntry
        Arguments = ''
        Admin = $false
        Description = 'Qwen 本机启动（不共享）'
    }
)

# Names this script owned in earlier revisions; they are replaced by the pair above
# so the desktop does not end up with two ways to do the same thing.
$retired = @('Qwen 一键启动.lnk', 'Qwen 停止共享.lnk')

foreach ($name in $retired) {
    $path = Join-Path $desktop $name
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
        Write-Host "已移除旧快捷方式：$name" -ForegroundColor DarkGray
    }
}

foreach ($entry in $shortcuts) {
    if ($Remove) {
        if (Test-Path -LiteralPath $entry.Path) {
            Remove-Item -LiteralPath $entry.Path -Force
            Write-Host "已删除：$($entry.Path)" -ForegroundColor Green
        }
        continue
    }
    if (-not (Test-Path -LiteralPath $entry.Target)) {
        Write-Host "跳过 $($entry.Path)：找不到 $($entry.Target)" -ForegroundColor Yellow
        continue
    }
    $shortcut = $shell.CreateShortcut($entry.Path)
    $shortcut.TargetPath = $entry.Target
    $shortcut.Arguments = $entry.Arguments
    $shortcut.WorkingDirectory = $root
    $shortcut.Description = $entry.Description
    # powershell.exe carries a usable icon, so nothing has to be shipped alongside.
    $shortcut.IconLocation = "$powershell,0"
    $shortcut.Save()
    if ($entry.Admin) {
        # Mark the shortcut itself as "run as administrator": double-clicking then
        # raises one UAC prompt, which is what lets the firewall rule be installed
        # and the hidden tunnel task be registered.
        $bytes = [IO.File]::ReadAllBytes($entry.Path)
        $bytes[0x15] = $bytes[0x15] -bor 0x20
        [IO.File]::WriteAllBytes($entry.Path, $bytes)
    }
    Write-Host "已创建：$($entry.Path)" -ForegroundColor Green
}

if ($Remove) { return }

Write-Host ''
Write-Host '桌面启动入口：' -ForegroundColor White
Write-Host '  · Qwen 共享面板   —— 启动服务 + 隧道，并打开带监控/口令/断联的管理面板（会弹一次 UAC）'
Write-Host '  · Start-Qwen.bat  —— 老入口：只启动本机服务，不共享'
Write-Host ''
Write-Host '删除这两个快捷方式：powershell -ExecutionPolicy Bypass -File .\New-Qwen-Shortcuts.ps1 -Remove'
