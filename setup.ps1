# First-run environment check for QwenWeb.
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
# It only checks and reports; adding -Install also pip-installs requirements.txt.
param(
    [switch]$Install,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$problems = New-Object System.Collections.Generic.List[string]
$notes = New-Object System.Collections.Generic.List[string]

function Say([string]$text, [string]$color = 'Gray') {
    if (-not $Quiet) { Write-Host $text -ForegroundColor $color }
}
function Problem([string]$text) { $problems.Add($text); Say "!!  $text" 'Yellow' }
function Note([string]$text) { $notes.Add($text); Say "    $text" 'Gray' }

# --- interpreter -------------------------------------------------------------
$python = $env:QWEN_PYTHON
if (-not $python) { $python = [IO.Path]::GetFullPath((Join-Path $root '../ComfyUI-BSAI_v010/python/python.exe')) }
Say "==> interpreter: $python"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Problem "Python not found. Install Python 3.12 and set `$env:QWEN_PYTHON, or place ComfyUI at ..\ComfyUI-BSAI_v010\."
} else {
    $version = & $python -c "import sys;print('%d.%d' % sys.version_info[:2])"
    Say "    python $version"
    if ($version -ne '3.12') { Problem "Python 3.12 expected, found $version (3.12 is what this was validated on)." }

    if ($Install) {
        Say '==> installing requirements.txt'
        & $python -m pip install -r (Join-Path $root 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { Problem 'pip install failed; see the output above.' }
    } else {
        & $python -c "import fastapi, uvicorn, httpx, psutil, websockets, av, cv2" 2>$null
        if ($LASTEXITCODE -ne 0) { Problem 'Runtime dependencies missing: run .\setup.ps1 -Install' }
    }
}

# --- directories the code hard-codes ----------------------------------------
foreach ($dir in @('models', 'logs', 'runtime', 'runtime-stock-11935', 'runtime-prism-10826')) {
    if (-not (Test-Path (Join-Path $root $dir))) {
        New-Item -ItemType Directory -Force -Path (Join-Path $root $dir) | Out-Null
        Note "created $dir\"
    }
}

# --- llama.cpp builds --------------------------------------------------------
$stock = Join-Path $root 'runtime-stock-11935/llama-server.exe'
$prism = Join-Path $root 'runtime-prism-10826/llama-server.exe'
if (-not (Test-Path $stock)) { Problem 'runtime-stock-11935\llama-server.exe missing: download the ggml-org/llama.cpp b11935 Windows CUDA build (docs/MODELS.md).' }
if (-not (Test-Path $prism)) { Problem 'runtime-prism-10826\llama-server.exe missing: download PrismML-Eng/llama.cpp release prism-b10826 (needed only for the ternary 27B).' }
if (-not (Test-Path (Join-Path $root 'runtime/ffmpeg.exe'))) { Note 'runtime\ffmpeg.exe missing: only video post-processing (concat/denoise/frames) needs it.' }

# --- weights -----------------------------------------------------------------
$ggufs = @(Get-ChildItem (Join-Path $root 'models') -Recurse -Filter *.gguf -ErrorAction SilentlyContinue)
if ($ggufs.Count -eq 0) {
    Problem 'no weights under models\: see docs/MODELS.md (smallest usable combination is listed there).'
} else {
    Say "==> weights: $($ggufs.Count) gguf file(s) found"
}
if (-not (Test-Path (Join-Path $root 'settings.json'))) { Note 'settings.json missing: the defaults in qwen_config.py apply; copy settings.example.json to change them.' }

# --- ComfyUI (optional) ------------------------------------------------------
$comfy = [IO.Path]::GetFullPath((Join-Path $root '../ComfyUI-BSAI_v010/ComfyUI/main.py'))
if (Test-Path -LiteralPath $comfy) {
    Say '==> ComfyUI found (image / video / upscale available)'
} else {
    Note "ComfyUI not found at $comfy - chat still works; image/video panels will report it as unavailable."
}

Write-Host ''
if ($problems.Count -eq 0) {
    Say 'Ready. Start the gateway with .\Start-Qwen.bat, then open http://127.0.0.1:8081' 'Green'
    exit 0
}
Say "$($problems.Count) problem(s) above must be fixed before the gateway can load a model." 'Red'
exit 1
