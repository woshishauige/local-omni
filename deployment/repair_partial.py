import argparse
import hashlib
import shutil
from pathlib import Path
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from download_models import FILES, MODELS

parser = argparse.ArgumentParser()
parser.add_argument('--file', type=int, choices=[0, 1], default=1)
parser.add_argument('--direct', action='store_true')
parser.add_argument('--include-small', action='store_true')
parser.add_argument('--fine-scan', action='store_true')
parser.add_argument('--scan-only', action='store_true')
args = parser.parse_args()
repo, revision, remote, local, size, sha = FILES[args.file]
target = MODELS / local
cache = (MODELS / '.cache/huggingface/download/text_encoders' if args.file == 1 else
         Path('D:/AI/deployment/hf-staging/.cache/huggingface/download/ref2va'))
sources = list(cache.glob(f'*.{sha}.incomplete'))
if len(sources) != 1 or sources[0].stat().st_size != size:
    raise RuntimeError('Expected exactly one full-length incomplete model file')
source = sources[0]
candidate = target.with_suffix(target.suffix + '.repair')
if not candidate.exists():
    print('Copying the stopped download for verified repair', flush=True)
    shutil.copyfile(source, candidate)

ranges = []
page_size = 4096 if args.fine_scan else 64 * 1024
with candidate.open('rb') as stream:
    offset = 0
    while block := stream.read(page_size):
        if block.count(0) == len(block):
            if ranges and ranges[-1][1] == offset:
                ranges[-1][1] = offset + len(block)
            else:
                ranges.append([offset, offset + len(block)])
        offset += len(block)
ranges = [item for item in ranges if args.include_small or item[1] - item[0] >= 1024 * 1024]
expanded = []
for start, end in ranges:
    start = max(0, start - 64 * 1024)
    end = min(size, end + 64 * 1024)
    if expanded and start <= expanded[-1][1]:
        expanded[-1][1] = max(expanded[-1][1], end)
    else:
        expanded.append([start, end])
ranges = expanded
print('Repair ranges:', len(ranges), 'bytes:', sum(end - start for start, end in ranges), flush=True)
if args.scan_only:
    print('First and last ranges:', ranges[:5], ranges[-5:], flush=True)
    raise SystemExit(0)

url = (f'https://modelscope.cn/api/v1/models/Comfy-Org/MiniMax-H3/repo?Revision=master&FilePath={quote(remote)}'
       if args.file == 1 else f'https://hf-mirror.com/{repo}/resolve/{revision}/{remote}')
with requests.Session() as session, candidate.open('r+b') as stream:
    session.trust_env = not args.direct
    session.mount('https://', HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1,
                  status_forcelist=[429, 500, 502, 503, 504])))
    for number, (start, end) in enumerate(ranges, 1):
        with session.get(url, headers={'Range': f'bytes={start}-{end - 1}'}, stream=True, timeout=(30, 90)) as response:
            response.raise_for_status()
            if response.status_code != 206 or response.headers.get('Content-Range') != f'bytes {start}-{end - 1}/{size}':
                raise RuntimeError('Mirror did not honor the exact repair range')
            url = response.url
            stream.seek(start)
            received = 0
            for block in response.iter_content(1024 * 1024):
                if received + len(block) > end - start:
                    raise RuntimeError('Repair range is too long')
                stream.write(block)
                received += len(block)
            if received != end - start:
                raise RuntimeError('Repair range is incomplete')
        if number % 25 == 0 or number == len(ranges):
            print('Filled ranges:', number, '/', len(ranges), flush=True)
print('Checking complete model SHA256', flush=True)
with candidate.open('rb') as stream:
    actual = hashlib.file_digest(stream, 'sha256').hexdigest()
if actual != sha:
    raise RuntimeError(f'Repaired file did not pass SHA256: {actual}; original download retained')
candidate.replace(target)
source.unlink()
print('Verified and installed:', target, flush=True)
