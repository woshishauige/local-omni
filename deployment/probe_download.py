from concurrent.futures import ThreadPoolExecutor
import time
import requests

path = 'joeygambino/MiniMax-H3-curve-GGUF/resolve/defd1c4e4e5617fae5ddff0f4668f9ac20ef5348/ref2va/MiniMax-H3-ref2va-curve-Q4_0.gguf'


def probe(settings):
    host, direct = settings
    label = host + (' direct' if direct else ' system-proxy')
    start = time.monotonic()
    total = 0
    try:
        session = requests.Session()
        session.trust_env = not direct
        with session.get(f'https://{host}/{path}', headers={'Range': 'bytes=209715200-218103807'}, stream=True, timeout=(15, 20)) as response:
            print(label, response.status_code, response.headers.get('Content-Range'), response.url.split('/')[2], flush=True)
            response.raise_for_status()
            for block in response.iter_content(1024 * 1024):
                total += len(block)
                if total >= 8 * 1024 * 1024:
                    break
        print(label, total, round(time.monotonic() - start, 1), 'seconds', flush=True)
    except requests.RequestException as error:
        print(label, str(error), flush=True)


with ThreadPoolExecutor(max_workers=2) as executor:
    list(executor.map(probe, [('hf-mirror.com', True), ('hf-mirror.com', False)]))
