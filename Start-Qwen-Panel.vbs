'  Qwen sharing panel entry: make sure the service is up, then open the panel as a
'  standalone window.
'
'  This is the file the desktop shortcut for the sharing panel points at. It does
'  three things:
'    1. makes sure the service is running. If nothing listens on 8081 yet, it starts
'       the service through "Start-Qwen-Local.ps1 -Detached", which hands the work to
'       WMI so the service is NOT a child of whatever launched this file. That is the
'       whole point: an earlier version started the service as a child of the caller,
'       so closing DeepSeek Harness (or any terminal that spawned it) took the Qwen
'       gateway, manager and llama-server down with it -- "the service dies whenever
'       DS disconnects".
'    2. waits for the gateway to answer, so the panel never opens onto a dead address
'       (that blank window is what a failed launch looked like).
'    3. opens the panel with Edge/Chrome --app mode: its own window, its own taskbar
'       button that can be closed on its own, no address bar, and a private browser
'       profile so it never becomes just another tab in the browser already running.
'
'  Everything the owner sees is: one panel window titled with the panel name.
'
'  NOTE: this file must stay ASCII-only. Windows Script Host reads .vbs files using
'  the ANSI code page, so non-ASCII text here fails to parse with an "invalid
'  character" error. Chinese notices are therefore built from ChrW codes (see Notice).
'
'  NOTE 2: a second double-click arrives while the first instance is still waiting for
'  the gateway. It must not die on the log file the first one holds open (that was
'  800A0046 "permission denied" in a Windows Script Host error box), and it must not
'  look like nothing happened -- measured 52 s from click to window on this machine.
Option Explicit

Dim shell, fso, root, script, url, linkFile, stream, command, trace, traceFile
Dim profile, arguments, app, ready, attempts, runResult, resultFile, text, payload
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
script = fso.BuildPath(root, "Start-Qwen-Local.ps1")
resultFile = fso.BuildPath(root, "logs\panel-entry.json")
url = ""
text = ""

' A silent launcher is impossible to diagnose when it fails, so every step is also
' appended to this file. It is small, overwritten on each run, and safe to delete.
' A second instance finds it locked (the first one is still waiting) and moves to a
' file of its own instead of failing: see the On Error block below.
traceFile = fso.BuildPath(root, "logs\panel-entry.log")
On Error Resume Next
Set trace = fso.OpenTextFile(traceFile, 2, True)
If Err.Number <> 0 Then
    Err.Clear
    traceFile = fso.BuildPath(root, "logs\panel-entry-" & Replace(CStr(Timer), ".", "-") & ".log")
    Set trace = fso.OpenTextFile(traceFile, 2, True)
    If Err.Number <> 0 Then
        Set trace = Nothing
        Err.Clear
    End If
End If
On Error GoTo 0
Note "root=" & root

If WScript.Arguments.Count > 0 Then
    url = WScript.Arguments(0)
End If

If Not fso.FileExists(script) Then
    Note "FATAL launcher not found: " & script
    CloseTrace
    MsgBox "Launcher not found: " & script, 16, "Qwen"
    WScript.Quit 1
End If

' The owner link carries the token that marks this profile as the owner's. It is
' written by the service on startup, so one earlier run is enough to have it here.
linkFile = fso.BuildPath(root, "logs\admin-link.txt")
If url = "" And fso.FileExists(linkFile) Then
    Set stream = fso.OpenTextFile(linkFile, 1)
    If Not stream.AtEndOfStream Then
        url = Trim(stream.ReadLine())
    End If
    stream.Close
End If
If Left(url, 4) <> "http" Then
    url = "http://127.0.0.1:8081/"
End If

' 0. Say something while the service is down. Measured on this machine: click to window
'    is about 52 s when the gateway has to be started (the stack closes itself when the
'    page closes), and a screen that changes nothing for a minute is what makes the
'    owner click again. One short HTTP probe decides whether to show it: any answer
'    (including the passphrase gate's 401) means the service is already there, and then
'    nothing is shown at all -- the ordinary one-click path stays instant.
If Not GatewayAnswers(url) Then
    Note "step0 gateway is not answering yet; showing the starting notice"
    shell.Popup Notice(), 5, "Qwen", 64
End If

' 1. Hand the whole "make the service available" decision to the launcher script.
'    It owns the port probe and the wait, because a raw TCP connect on this machine
'    takes ~2 s to come back refused, and a probe loop built out of that would keep
'    the panel waiting for no reason. The script also starts the service through WMI,
'    so the service is NOT a child of whatever launched this file -- closing DeepSeek
'    Harness no longer takes the Qwen gateway down with it.
'
'    bWaitOnReturn = True with a hard timeout: the launcher normally returns as soon
'    as the gateway answers (or after its own 120 s), and the timeout keeps this file
'    from ever hanging the double-click.
payload = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & script & _
          """ -Share -Quiet -NoBrowser -Detached -PanelUrl """ & url & """"
command = "powershell.exe " & payload
Note "step1 t=" & FormatNumber(Timer, 2) & " launching detached start"
runResult = shell.Run(command, 0, True)
Note "step1 t=" & FormatNumber(Timer, 2) & " run returned " & runResult

' The launcher publishes its verdict as a small JSON file; reading it is the only
' reliable way to tell "the gateway is up" from "the launcher gave up".
ready = False
If fso.FileExists(resultFile) Then
    Set stream = fso.OpenTextFile(resultFile, 1)
    If Not stream.AtEndOfStream Then
        text = stream.ReadAll()
    End If
    stream.Close
    If InStr(1, text, """ready"":true", 1) > 0 Or InStr(1, text, """ready"": true", 1) > 0 Then
        ready = True
    End If
End If
Note "step2 t=" & FormatNumber(Timer, 2) & " ready=" & ready & " result=" & text

' The verdict file is advisory: a second double-click makes two parents race over it (one can
' delete or fail to write while the other is reading), and a stale "ready" from an earlier run
' would send the window to a dead address. What actually matters for the window is whether the
' port answers, so ask it directly -- one probe, and the answer is never out of date.
If Not ready Then
    ready = GatewayAnswers(url)
    Note "step2b verdict overruled by the port probe: ready=" & ready
End If

' 3. Open the panel as its own window. The dedicated profile is what makes the
'    window separate instead of a tab in the browser that is already running.
'    No --start-minimized here: the owner double-clicked to SEE the panel, and a
'    minimized window reads as "it did not start".
profile = fso.BuildPath(root, "logs\panel-profile")
If Not fso.FolderExists(profile) Then
    fso.CreateFolder profile
End If

app = fso.BuildPath(Environment("ProgramFiles(x86)"), "Microsoft\Edge\Application\msedge.exe")
If Not fso.FileExists(app) Then
    app = fso.BuildPath(Environment("ProgramFiles"), "Google\Chrome\Application\chrome.exe")
End If

If fso.FileExists(app) Then
    arguments = " --user-data-dir=""" & profile & """ --no-first-run --no-default-browser-check" & _
                " --app=""" & url & """"
    Note "step3 app=" & app
    Note "step3 args=" & arguments
    shell.Run """" & app & """" & arguments, 1, False
Else
    ' No Chromium browser to host the panel: fall back to the default browser tab.
    Note "step3 no chromium found; opening the default browser"
    shell.Run """" & url & """", 1, False
End If

' 4. Say something when the service did not come up. A silent exit is what made the
'    failure impossible to place; the log line above plus this message is the whole
'    diagnosis the owner needs. The text is built from ChrW codes so this file stays
'    ASCII-only (see the note at the top).
If Not ready Then
    MsgBox Notice2(), 48, "Qwen panel"
End If

Note "done ready=" & ready
CloseTrace
WScript.Quit 0

Function Environment(name)
    Dim value
    value = shell.ExpandEnvironmentStrings("%" & name & "%")
    If value = "%" & name & "%" Then
        value = ""
    End If
    Environment = value
End Function

' Whether the gateway answers at all. Any HTTP status counts -- the passphrase gate
' answers 401 to a stranger and that still means "the service is up". Only a refused
' connection or a timeout means "not yet", which is the only thing the caller needs.
'
' WinHttp is the object that can express this: measured on this machine, a live 401
' comes back as "no error, status=401", while a dead port is "error -2147012867,
' status=-1". MSXML2 cannot: it reports the live 401 as error -2147024891 with
' status=0, i.e. exactly like a failure, which is how the notice once appeared on a
' perfectly healthy service.
Function GatewayAnswers(target)
    Dim probe, code
    GatewayAnswers = False
    On Error Resume Next
    Set probe = CreateObject("WinHttp.WinHttpRequest.5.1")
    If Err.Number = 0 Then
        probe.SetTimeouts 1500, 1500, 1500, 1500
        probe.Open "GET", target, False
        probe.Send
        code = probe.Status
        Err.Clear
        If code >= 100 Then
            GatewayAnswers = True
            On Error GoTo 0
            Exit Function
        End If
    End If
    Err.Clear
    On Error GoTo 0
    ' No WinHttp on this machine: fall back to "assume it is up" rather than blocking the
    ' launch with a notice the owner cannot act on.
    If probe Is Nothing Then GatewayAnswers = True
End Function

' "Qwen is starting the service, about a minute; please do not click again." built from
' character codes (see the ASCII-only note at the top of this file).
Function Notice()
    Notice = ChrW(&H0051) & ChrW(&H0077) & ChrW(&H0065) & ChrW(&H006E) & ChrW(&H0020) & _
             ChrW(&H6B63) & ChrW(&H5728) & ChrW(&H542F) & ChrW(&H52A8) & ChrW(&H670D) & _
             ChrW(&H52A1) & ChrW(&HFF0C) & ChrW(&H7EA6) & ChrW(&H9700) & ChrW(&H4E00) & _
             ChrW(&H5206) & ChrW(&H949F) & ChrW(&HFF1B) & ChrW(&H8BF7) & ChrW(&H4E0D) & _
             ChrW(&H8981) & ChrW(&H91CD) & ChrW(&H590D) & ChrW(&H70B9) & ChrW(&H51FB) & _
             ChrW(&H3002)
End Function

' "The service is not ready yet; the window opens first and the page retries by itself."
' plus the two log paths, again built from character codes.
Function Notice2()
    Notice2 = ChrW(&H670D) & ChrW(&H52A1) & ChrW(&H8FD8) & ChrW(&H6CA1) & ChrW(&H5C31) & _
              ChrW(&H7EEA) & ChrW(&HFF0C) & ChrW(&H7A97) & ChrW(&H53E3) & ChrW(&H5148) & _
              ChrW(&H5F00) & ChrW(&HFF1B) & ChrW(&H7B49) & ChrW(&H4E00) & ChrW(&H4E0B) & _
              ChrW(&H9875) & ChrW(&H9762) & ChrW(&H4F1A) & ChrW(&H81EA) & ChrW(&H5DF1) & _
              ChrW(&H91CD) & ChrW(&H8BD5) & ChrW(&H3002) & vbCrLf & vbCrLf & _
              "Entry log: logs\panel-entry.log" & vbCrLf & _
              "Service log: logs\supervisor.log"
End Function

' Every diagnostic line goes through here: a launcher that cannot write its log must
' still be able to start the service and open the panel.
Sub Note(value)
    On Error Resume Next
    If Not trace Is Nothing Then
        trace.WriteLine value
    End If
    On Error GoTo 0
End Sub

Sub CloseTrace()
    On Error Resume Next
    If Not trace Is Nothing Then
        trace.Close
        Set trace = Nothing
    End If
    On Error GoTo 0
End Sub
