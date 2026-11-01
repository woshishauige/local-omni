import os
import argparse
import hashlib
import json
from pathlib import Path

os.environ['HF_XET_HIGH_PERFORMANCE'] = '1'
os.environ['HF_XET_NUM_CONCURRENT_RANGE_GETS'] = '32'
os.environ['HF_XET_CHUNK_CACHE_SIZE_BYTES'] = '0'
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
os.environ['HF_HOME'] = 'D:/AI/deployment/huggingface'
os.environ['HF_XET_CACHE'] = 'D:/AI/deployment/xet'

from huggingface_hub import hf_hub_download
from huggingface_hub._local_folder import get_local_download_paths
from download_models import FILES, MODELS

parser = argparse.ArgumentParser()
parser.add_argument('--files', type=int, nargs='+', default=[1])
args = parser.parse_args()
for index in args.files:
    repo, revision, remote, local, size, sha256 = FILES[index]
    target = MODELS / local
    if target.exists():
        path = target
    else:
        folder = MODELS if index == 1 else Path('D:/AI/deployment/hf-staging')
        partial = target.with_name(target.name + '.partial')
        state_path = target.with_name(target.name + '.ranges.json')
        if index == 0 and partial.exists() and state_path.exists():
            state = json.loads(state_path.read_text())
            prefix = state['prefix']
            part = 0
            while part in state['complete']:
                prefix = min(size, prefix + 128 * 1024 * 1024)
                part += 1
            resume = get_local_download_paths(folder, remote).incomplete_path(sha256)
            if resume.exists():
                raise RuntimeError('Both resume files exist')
            with partial.open('r+b') as stream:
                stream.truncate(prefix)
            partial.replace(resume)
            print('Preserved contiguous bytes:', prefix, flush=True)
        print('Xet download:', remote, flush=True)
        path = Path(hf_hub_download(repo_id=repo, filename=remote, revision=revision, local_dir=str(folder)))
    print('Checking SHA256:', path.name, flush=True)
    with path.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if path.stat().st_size != size or actual != sha256:
        raise RuntimeError(f'Model integrity check failed: {path}')
    if path != target:
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
    print('Verified and installed:', target, flush=True)
