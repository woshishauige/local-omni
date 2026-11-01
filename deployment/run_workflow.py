import argparse
import json
from pathlib import Path
import time

import requests

parser = argparse.ArgumentParser()
parser.add_argument('path')
parser.add_argument('--max-seconds', type=int, default=3600)
args = parser.parse_args()
path = Path(args.path)
payload = json.loads(path.read_text(encoding='utf-8'))
response = requests.post('http://127.0.0.1:8188/prompt', json=payload, timeout=60)
result = response.json()
print('Queue result:', json.dumps(result, ensure_ascii=False), flush=True)
response.raise_for_status()
prompt_id = result['prompt_id']
began = time.monotonic()
while time.monotonic() - began < args.max_seconds:
    history = requests.get(f'http://127.0.0.1:8188/history/{prompt_id}', timeout=30).json()
    if prompt_id in history:
        record = history[prompt_id]
        out = Path('D:/AI/deployment/logs') / (path.stem + '-result.json')
        out.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Status:', json.dumps(record.get('status'), ensure_ascii=False), flush=True)
        print('Outputs:', json.dumps(record.get('outputs'), ensure_ascii=False), flush=True)
        print('Seconds:', round(time.monotonic() - began, 1), flush=True)
        if record.get('status', {}).get('status_str') != 'success':
            raise SystemExit(1)
        break
    time.sleep(5)
else:
    raise TimeoutError(f'Workflow {prompt_id} is still running; inspect the ComfyUI queue')
