# launchers

根目录只留最常用的两个入口：`Start-Qwen.bat` / `Stop-Qwen.bat`（以及它们调用的 `.ps1`）。
其余启动器都在这里，双击前先看这一行说明：

| 文件 | 干什么 | 什么时候用 |
| --- | --- | --- |
| `Start-Qwen-Shared.ps1` → 实际叫 `Start-Qwen-Share.ps1` | 打开「共享给他人」：生成访问口令 + 起 Cloudflare 隧道，把站点临时分享出去 | 想让别人在浏览器里用你这台机器 |
| `Stop-Qwen-Share.ps1` | 关掉共享：注销隧道、删除 `share.config.json`、重启为只监听本机 | 分享结束 |
| `Start-Qwen-Local.ps1` | 图形化启动面板：起服务、打开本地页面、显示状态 | 不想开命令行 |
| `Start-Qwen-Panel.vbs` | 双击入口，静默调用 `Start-Qwen-Local.ps1` | 同上（给桌面快捷方式用） |
| `New-Qwen-Shortcuts.ps1` | 在桌面/开始菜单创建启动快捷方式 | 第一次安装 |
| `启动Qwen-独立进程.ps1` / `.vbs` | 用 WMI 起一个和终端脱钩的独立进程 | 想让服务不跟着终端退出 |
| `fix-and-restart-qwen.ps1` / `.bat` | 修「8082 被旧后端占着」：按需管理员权限清理残留进程再重启 | 换了模型后起不来 |

**路径约定**：这些脚本都在 `launchers/` 里，项目根是上一级；脚本内部统一用
`$root = Split-Path -Parent $PSScriptRoot` 定位 `qwen_service.py`、`share.config.json`、`tools\cloudflared.exe`。
同目录互相调用的（面板 ↔ 本地启动、独立进程 `.vbs` ↔ `.ps1`、修复 `.bat` ↔ `.ps1`）保持原样。

改完记得验证：`powershell -NoProfile -Command "[void][System.Management.Automation.Language.Parser]::ParseFile('路径',[ref]$null,[ref]$e)"`，
或者直接看根目录 `Start-Qwen.bat` 是否仍然能起来。
