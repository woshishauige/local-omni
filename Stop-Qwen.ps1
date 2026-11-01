$ErrorActionPreference = 'Stop'
$qwenPython = $env:QWEN_PYTHON
if (-not $qwenPython) { $qwenPython = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../ComfyUI-BSAI_v010/python/python.exe')) }
$qwenService = Join-Path $PSScriptRoot 'qwen_service.py'
if (-not (Test-Path -LiteralPath $qwenPython -PathType Leaf)) { throw "Python not found: $qwenPython (set QWEN_PYTHON)" }
& $qwenPython -X utf8 $qwenService stop
exit $LASTEXITCODE
