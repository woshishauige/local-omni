import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

import av
import requests

ROOT = Path('D:/AI/ComfyUI-BSAI_v010/ComfyUI')
URL = 'http://127.0.0.1:8188'
project_name = 'h3-reference-check-' + uuid.uuid4().hex[:10]
run = ROOT / 'output/video/H3-30s' / project_name
sample = ROOT / 'output/video/H3-30s/tianmen-duel-30s'
report = {'project': project_name, 'width': 768, 'height': 512, 'fps': 24, 'steps': 2, 'runs': []}


def hashes():
    values = {}
    for path in sample.glob('*.mp4'):
        with path.open('rb') as stream:
            values[path.name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return values


def execute(payload):
    queue = requests.get(URL + '/queue', timeout=10).json()
    assert not queue['queue_running'] and not queue['queue_pending'], 'ComfyUI is busy.'
    response = requests.post(URL + '/prompt', json=payload, timeout=60)
    response.raise_for_status()
    prompt_id = response.json()['prompt_id']
    began = time.monotonic()
    last_update = 0
    peak = 0
    print(json.dumps({'prompt_id': prompt_id, 'project': project_name}), flush=True)
    while time.monotonic() - began < 1800:
        used = float(subprocess.check_output(['nvidia-smi', '--query-gpu=memory.used',
            '--format=csv,noheader,nounits'], text=True, creationflags=subprocess.CREATE_NO_WINDOW).strip())
        peak = max(peak, used)
        elapsed = time.monotonic() - began
        if elapsed - last_update >= 30:
            print(json.dumps({'elapsed': round(elapsed), 'vram_mib': used, 'peak_vram_mib': peak}), flush=True)
            last_update = elapsed
        history = requests.get(URL + '/history/' + prompt_id, timeout=30).json()
        if prompt_id in history:
            record = history[prompt_id]
            report['runs'].append({'prompt_id': prompt_id, 'elapsed_seconds': round(elapsed, 2),
                'peak_vram_mib': peak, 'status': record['status']})
            if record['status']['status_str'] != 'success':
                raise RuntimeError(json.dumps(record['status']))
            return record
        time.sleep(5)
    raise TimeoutError('Reference render did not finish within 30 minutes.')


before = hashes()
try:
    with tempfile.TemporaryDirectory(prefix='h3-reference-check-', dir=ROOT / 'input') as temporary:
        input_dir = Path(temporary)
        with av.open(str(sample / 'shot_01.mp4')) as source:
            for index, frame in enumerate(source.decode(video=0)):
                if index in (0, 30, 60, 90, 120):
                    frame.to_image().save(input_dir / f'reference_{index // 30 + 1}.png')
        payload = json.loads(Path('D:/AI/deployment/api/05_H3_LongVideo_12GB.json').read_text(encoding='utf-8'))
        payload.pop('extra_data', None)
        inputs = payload['prompt']['50']['inputs']
        inputs.update(project_name=project_name, steps=2, action='next_shot',
            storyboard=json.dumps({'shots': [
                {'prompt': 'Two adult sword fighters stand beneath a jade temple gate above the clouds. Wind moves their robes.'},
                {'prompt': 'Continue the same camera framing. Use <Picture 1> through <Picture 5> for the characters and jade gate. They slowly raise their weapons.'},
            ]}))
        execute(payload)
        with av.open(str(run / 'shot_01.mp4')) as source:
            first = json.loads(source.metadata['h3_checkpoint'])
        assert first['reference_images'] == [] and first['reference_sha256'] is None
        assert first['frames'] == 180
        with (run / 'shot_01.mp4').open('rb') as source:
            first_hash = hashlib.file_digest(source, 'sha256').hexdigest()
        filled = copy.deepcopy(payload)
        for slot in range(1, 6):
            filled['prompt'][str(64 + slot)]['inputs']['image'] = f'{input_dir.name}/reference_{slot}.png'
        execute(filled)
        with av.open(str(run / 'shot_02.mp4')) as source:
            second = json.loads(source.metadata['h3_checkpoint'])
        assert len(second['reference_images']) == 6
        assert [row['picture'] for row in second['reference_images']] == [1, 2, 3, 4, 5, 6]
        assert [row['slot'] for row in second['reference_images'][:5]] == [f'shot_02_ref_{slot:02d}' for slot in range(1, 6)]
        assert second['reference_images'][-1]['slot'] == 'continuity_identity'
        assert second['previous_sha256'] == first_hash and second['reference_sha256'] == first_hash
        assert second['frames'] == 180 and second['trim_frames'] == 22
        with av.open(str(run / 'master.mp4')) as source:
            assert sum(1 for _ in source.decode(video=0)) == 360
        report.update(empty_inputs_rendered=True, five_uploaded_references_rendered=True,
            automatic_identity_additional=True, motion_context_used=True, frames_per_shot=180,
            first_checkpoint=first, second_checkpoint=second, existing_film_unchanged=hashes() == before)
        assert report['existing_film_unchanged']
finally:
    if run.exists():
        assert run.parent.resolve() == (ROOT / 'output/video/H3-30s').resolve()
        assert run.name == project_name and project_name.startswith('h3-reference-check-')
        shutil.rmtree(run)
    report['test_media_removed'] = not run.exists()
    Path('D:/AI/deployment/logs/h3-reference-render-verification.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({key: value for key, value in report.items() if 'checkpoint' not in key}, ensure_ascii=False), flush=True)
