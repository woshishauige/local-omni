from datetime import datetime
from pathlib import Path
import subprocess

import psutil
import requests

root = Path('D:/AI/ComfyUI-BSAI_v010')
queue = requests.get('http://127.0.0.1:8188/queue', timeout=10).json()
if queue['queue_running'] or queue['queue_pending']:
    raise RuntimeError('ComfyUI has queued work; restart cancelled.')
matches = []
for process in psutil.process_iter(['exe', 'cmdline']):
    argv = process.info['cmdline'] or []
    if process.info['exe'] and Path(process.info['exe']) == root / 'python/python.exe' and 'ComfyUI\\main.py' in argv and 'ComfyUI-H3-Multishot' in argv:
        matches.append(process)
if len(matches) != 1:
    raise RuntimeError(f'Expected one deployed H3 server, found {len(matches)}.')
matches[0].terminate()
matches[0].wait(timeout=30)
stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
log = Path(f'D:/AI/deployment/logs/comfy-768x512-{stamp}')
with log.with_suffix('.log').open('w', encoding='utf-8') as stdout, log.with_suffix('.err.log').open('w', encoding='utf-8') as stderr:
    process = subprocess.Popen([
        'C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe', '-NoProfile',
        '-ExecutionPolicy', 'Bypass', '-File', str(root / 'Start-H3-12GB.ps1'), '-NoBrowser',
    ], cwd=root, stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
print(f'Launcher PID {process.pid}, logs {log}')
