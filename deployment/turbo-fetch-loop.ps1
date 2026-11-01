$env:PYTHONIOENCODING = 'utf-8'
for ($round = 1; $round -le 20; $round++) {
  Write-Output ("=== round $round " + (Get-Date -Format 'HH:mm:ss'))
  & 'D:\AI\ComfyUI-BSAI_v010\python\python.exe' -X utf8 -B 'D:\AI\deployment\fetch_turbo_uncensored.py' 2>&1 | Select-Object -Last 4
  $verify = & 'D:\AI\ComfyUI-BSAI_v010\python\python.exe' -X utf8 -B 'D:\AI\deployment\fetch_turbo_uncensored.py' --verify 2>&1
  if (-not ($verify -match 'missing')) { Write-Output 'ALL FILES COMPLETE'; break }
  Start-Sleep -Seconds 20
}
