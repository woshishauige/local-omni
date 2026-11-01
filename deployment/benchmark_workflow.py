import argparse
import ctypes
import json
from pathlib import Path
import subprocess
import time

import psutil
import requests

parser = argparse.ArgumentParser()
parser.add_argument('path', type=Path)
parser.add_argument('--max-seconds', type=int, default=14400)
args = parser.parse_args()
payload = json.loads(args.path.read_text(encoding='utf-8'))
response = requests.post('http://127.0.0.1:8188/prompt', json=payload, timeout=60)
print(response.text, flush=True)
response.raise_for_status()
prompt_id = response.json()['prompt_id']
record_name = args.path.stem + '-' + prompt_id[:8]
root = Path('D:/AI/deployment/logs/768x512')
root.mkdir(exist_ok=True)
began = time.monotonic()
samples = []
last_report = 0
try:
    while time.monotonic() - began < args.max_seconds:
        battery = psutil.sensors_battery()
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if battery is None or battery.power_plugged else 0x80000000)
        elapsed = time.monotonic() - began
        gpu = subprocess.check_output([
            'nvidia-smi', '--query-gpu=memory.used,utilization.gpu,power.draw,temperature.gpu',
            '--format=csv,noheader,nounits'], text=True, creationflags=subprocess.CREATE_NO_WINDOW).strip()
        row = dict(zip(('vram_mib', 'gpu_percent', 'watts', 'temperature'), map(float, gpu.split(','))))
        row.update(seconds=round(elapsed, 1), ram_available_gib=round(psutil.virtual_memory().available / 2**30, 2))
        samples.append(row)
        if elapsed - last_report >= 30:
            print(json.dumps(row), flush=True)
            last_report = elapsed
        history = requests.get(f'http://127.0.0.1:8188/history/{prompt_id}', timeout=30).json()
        if prompt_id in history:
            record = history[prompt_id]
            (root / (record_name + '-result.json')).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({'elapsed': elapsed, 'status': record['status'], 'outputs': record['outputs'],
                              'peak_vram_mib': max(s['vram_mib'] for s in samples)}, ensure_ascii=False), flush=True)
            if record['status']['status_str'] != 'success':
                raise SystemExit(1)
            break
        time.sleep(5)
    else:
        raise TimeoutError(f'Job {prompt_id} is still queued; inspect before interrupting.')
finally:
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    (root / (record_name + '-telemetry.json')).write_text(json.dumps(samples, indent=2), encoding='utf-8')
