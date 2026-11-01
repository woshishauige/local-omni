import copy
import json
from pathlib import Path

ROOT = Path('D:/AI/deployment/logs')
prompt = json.loads((ROOT / 'h3-current-ui-prompt.json').read_text(encoding='utf-8'))
assert prompt['50']['class_type'] == 'H3MP4ShotEditor'
assert prompt['80']['class_type'] == 'ApplyMiniMaxH3FirstBlockCache'
assert prompt['1']['inputs']['model_name'].endswith('MiniMax-H3-ref2va-curve-Q4_0.gguf')
prompt['50']['inputs'].update(storyboard=['40', 0], steps=20, action='next_shot', shot_index=1)
directory = ROOT / 'h3-cache-ab-20260909'
directory.mkdir(exist_ok=True)
for label, cache in [('native', False), ('cache', True)]:
    candidate = copy.deepcopy(prompt)
    candidate['50']['inputs']['project_name'] = 'h3-cache-ab-20260909-' + label
    if not cache:
        candidate['7']['inputs']['model'] = ['1', 0]
        del candidate['80']
    payload = {'prompt': candidate, 'client_id': 'h3-ab-' + label}
    (directory / (label + '.json')).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(directory / (label + '.json'))
