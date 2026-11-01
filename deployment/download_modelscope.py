import argparse
from urllib.parse import quote

import requests

from download_models import FILES, MODELS, download

parser = argparse.ArgumentParser()
parser.add_argument('--files', type=int, nargs='+', default=[3, 2, 4])
args = parser.parse_args()
repo = 'Comfy-Org/MiniMax-H3'
response = requests.get(
    f'https://modelscope.cn/api/v1/models/{repo}/repo/files',
    params={'Recursive': 'true', 'PageSize': 100}, timeout=30)
response.raise_for_status()
metadata = {entry['Path']: entry for entry in response.json()['Data']['Files']}
for index in args.files:
    _, _, remote, local, size, sha = FILES[index]
    entry = metadata[remote]
    if entry['Size'] != size or entry['Sha256'] != sha:
        raise RuntimeError(f'Mirror metadata differs from the pinned source: {remote}')
    url = f'https://modelscope.cn/api/v1/models/{repo}/repo?Revision=master&FilePath={quote(remote)}'
    download(url, MODELS / local, size, sha)
