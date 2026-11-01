from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import threading
import time

import requests
from download_models import FILES, MODELS

CHUNK = 128 * 1024 * 1024
WORKERS = 8


def fetch_file(item):
    repo, revision, remote, local, size, sha256 = item
    target = MODELS / local
    partial = target.with_name(target.name + '.partial')
    state_path = target.with_name(target.name + '.ranges.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        with target.open('rb') as stream:
            if target.stat().st_size == size and hashlib.file_digest(stream, 'sha256').hexdigest() == sha256:
                print(f'Already verified: {target.name}', flush=True)
                return
        raise RuntimeError(f'Unexpected existing file: {target}')
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state['size'] != size or state['sha256'] != sha256:
            raise RuntimeError('Resume metadata does not match model')
    else:
        initial = partial.stat().st_size if partial.exists() else 0
        state = {'size': size, 'sha256': sha256, 'prefix': initial, 'complete': []}
        state_path.write_text(json.dumps(state))
    with partial.open('ab') as stream:
        stream.truncate(size)
    ranges = [(i, start, min(start + CHUNK, size) - 1)
              for i, start in enumerate(range(state['prefix'], size, CHUNK))]
    lock = threading.Lock()
    progress = {'bytes': state['prefix'] + sum(end - start + 1 for i, start, end in ranges if i in state['complete']), 'last': time.monotonic()}
    began = time.monotonic()
    baseline = progress['bytes']
    url = f'https://huggingface.co/{repo}/resolve/{revision}/{remote}'

    def fetch(part):
        index, start, end = part
        if index in state['complete']:
            return
        for attempt in range(6):
            received = 0
            try:
                with requests.get(url, headers={'Range': f'bytes={start}-{end}'}, stream=True, timeout=(30, 90)) as response:
                    response.raise_for_status()
                    expected_range = f'bytes {start}-{end}/{size}'
                    if response.status_code != 206 or response.headers.get('Content-Range') != expected_range:
                        raise RuntimeError(f'Invalid range response: {response.status_code} {response.headers.get("Content-Range")}')
                    with partial.open('r+b', buffering=0) as stream:
                        stream.seek(start)
                        for block in response.iter_content(1024 * 1024):
                            if received + len(block) > end - start + 1:
                                raise RuntimeError('Range response too long')
                            stream.write(block)
                            received += len(block)
                            with lock:
                                progress['bytes'] += len(block)
                                now = time.monotonic()
                                if now - progress['last'] >= 20:
                                    print(f'{target.name}: {progress["bytes"] / 1e9:.2f}/{size / 1e9:.2f} GB; '
                                          f'{(progress["bytes"] - baseline) / 1e6 / (now - began):.1f} MB/s; {WORKERS} connections', flush=True)
                                    progress['last'] = now
                if received != end - start + 1:
                    raise RuntimeError('Incomplete range')
                with lock:
                    state['complete'].append(index)
                    temp = state_path.with_suffix('.tmp')
                    temp.write_text(json.dumps(state))
                    temp.replace(state_path)
                return
            except (requests.RequestException, RuntimeError) as error:
                with lock:
                    progress['bytes'] -= received
                print(f'Range {index}, attempt {attempt + 1}: {error}', flush=True)
                if attempt == 5:
                    raise
                time.sleep(3)

    print(f'Downloading {target.name}: {size / 1e9:.2f} GB, {WORKERS} connections', flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        for future in as_completed([executor.submit(fetch, part) for part in ranges]):
            future.result()
    print(f'Checking SHA256: {target.name}', flush=True)
    with partial.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != sha256:
        raise RuntimeError(f'SHA256 mismatch for {target.name}: {actual}')
    partial.replace(target)
    print(f'Verified: {target.name}', flush=True)


if __name__ == '__main__':
    for entry in FILES:
        fetch_file(entry)
