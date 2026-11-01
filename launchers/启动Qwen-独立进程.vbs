' Double-click this file to start (or attach to) the local Qwen service.
' It hands the real work to "启动Qwen-独立进程.ps1" next to it, and that script creates the
' process through WMI, so the service never hangs off a terminal or off DeepSeek Harness:
' closing DS does not take Qwen down with it.
' This file is deliberately ASCII-only: Windows Script Host reads .vbs as ANSI unless it is
' UTF-16, and a UTF-8 file with non-ASCII text fails to compile ("unterminated string constant").
Option Explicit

Dim fso, shell, root, script, command
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)
script = root & "\" & ChrW(21551) & ChrW(21160) & "Qwen-" & ChrW(29420) & ChrW(31435) & ChrW(36827) & ChrW(31243) & ".ps1"

If Not fso.FileExists(script) Then
  MsgBox "Launcher script not found: " & script, 16, "Qwen launcher"
  WScript.Quit 1
End If

command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & script & """"
' 0 = hidden window, False = do not wait (the service keeps running on its own)
shell.Run command, 0, False
