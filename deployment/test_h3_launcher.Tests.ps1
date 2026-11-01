$launcher = 'D:\AI\ComfyUI-BSAI_v010\Start-H3-12GB.ps1'
. $launcher

Describe 'H3 launcher' {
    It 'parses in Windows PowerShell and enables latent previews' {
        $tokens = $null
        $parseErrors = $null
        [System.Management.Automation.Language.Parser]::ParseFile($launcher, [ref]$tokens, [ref]$parseErrors) | Out-Null
        $parseErrors.Count | Should Be 0
        (Get-Content -LiteralPath $launcher -Raw) | Should Match '--preview-method latent2rgb'
    }

    It 'reuses a ready H3 server before checking or stopping Qwen' {
        Mock Invoke-RestMethod { [pscustomobject]@{ system = @{ argv = @('main.py', 'ComfyUI-H3-Multishot') } } }
        Mock Start-Process { }
        Mock Test-H3QwenRunning { throw 'Qwen must not be checked for reuse' }
        (Start-H3Local) | Should Be 0
        Assert-MockCalled Start-Process -Times 1 -Exactly -Scope It -ParameterFilter { $FilePath -eq 'http://127.0.0.1:8188' }
        Assert-MockCalled Test-H3QwenRunning -Times 0 -Exactly -Scope It
    }

    It 'honors NoBrowser when a ready H3 server is reused' {
        Mock Invoke-RestMethod { [pscustomobject]@{ system = @{ argv = @('ComfyUI-H3-Multishot') } } }
        Mock Start-Process { }
        (Start-H3Local -NoBrowser) | Should Be 0
        Assert-MockCalled Start-Process -Times 0 -Exactly -Scope It
    }

    It 'rejects a different service without touching Qwen' {
        Mock Invoke-RestMethod { [pscustomobject]@{ system = @{ argv = @('main.py', '--port', '8188') } } }
        Mock Test-H3QwenRunning { throw 'Qwen must not be checked for a port conflict' }
        { Start-H3Local -NoBrowser } | Should Throw 'Port 8188 is occupied'
        Assert-MockCalled Test-H3QwenRunning -Times 0 -Exactly -Scope It
    }

    It 'handles a non-WebException connection failure on an unused port' {
        Mock Invoke-RestMethod { throw [System.InvalidOperationException]::new('Cannot connect') }
        Mock Test-H3LocalPort { $false }
        (Open-H3ExistingServer -NoBrowser) | Should Be $false
    }

    It 'does not start a second server while the port is responding without an API' {
        Mock Invoke-RestMethod { throw 'HTTP 503' }
        Mock Test-H3LocalPort { $true }
        { Open-H3ExistingServer -NoBrowser } | Should Throw 'not ready'
    }
}

Describe 'Qwen activity detection for H3 startup' {
    It 'skips Qwen stop when there are no listeners or process records' {
        Mock Test-H3LocalPort { $false }
        Mock Test-Path { $false }
        (Test-H3QwenRunning 'D:\AI\Qwen-Local') | Should Be $false
    }

    It 'ignores stale Qwen PID records whose processes have exited' {
        Mock Test-H3LocalPort { $false }
        Mock Test-Path { $true }
        Mock Get-Content { '{"pid":2147483600,"created_unix_ms":1}' }
        Mock Get-Process { $null }
        (Test-H3QwenRunning 'D:\AI\Qwen-Local') | Should Be $false
    }

    It 'ignores a Qwen PID reused by a newer process' {
        Mock Test-H3LocalPort { $false }
        Mock Test-Path { $true }
        Mock Get-Content { '{"pid":1,"created_unix_ms":1}' }
        Mock Get-Process { [pscustomobject]@{ StartTime = [DateTime]::Now } }
        (Test-H3QwenRunning 'D:\AI\Qwen-Local') | Should Be $false
    }

    It 'requires the existing Qwen stop routine for a live gateway' {
        Mock Test-H3LocalPort { $true }
        (Test-H3QwenRunning 'D:\AI\Qwen-Local') | Should Be $true
    }

    It 'treats unreadable Qwen state conservatively' {
        Mock Test-H3LocalPort { $false }
        Mock Test-Path { $true }
        Mock Get-Content { throw [System.UnauthorizedAccessException]::new('Access denied') }
        (Test-H3QwenRunning 'D:\AI\Qwen-Local') | Should Be $true
    }
}
