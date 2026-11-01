import argparse
import hashlib
import json
from pathlib import Path
import time

import requests

ROOT = Path('D:/AI')
MODELS = ROOT / 'ComfyUI-BSAI_v010/ComfyUI/models'
FILES = [
    ('joeygambino/MiniMax-H3-curve-GGUF', 'defd1c4e4e5617fae5ddff0f4668f9ac20ef5348',
     'ref2va/MiniMax-H3-ref2va-curve-Q4_0.gguf', 'diffusion_models/minimax_h3/MiniMax-H3-ref2va-curve-Q4_0.gguf',
     11464827552, '1cdaee7bf8cf609af7c2eedb70fb372136f2111eafcf15c50f18b7ba8f5b5ec8'),
    ('Comfy-Org/MiniMax-H3', '4cc1d817b6184899b41293954329f576cb5ae86b',
     'text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors', 'text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors',
     15687142551, '35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6'),
    ('Comfy-Org/MiniMax-H3', '4cc1d817b6184899b41293954329f576cb5ae86b',
     'vae/minimax_h3_video_vae_fp16.safetensors', 'vae/minimax/minimax_h3_video_vae_fp16.safetensors',
     5207808496, '7c1f131492e7eddacaac9069a61b81bdd39de5cc96561e677c5eab1cdce5e522'),
    ('Comfy-Org/MiniMax-H3', '4cc1d817b6184899b41293954329f576cb5ae86b',
     'vae/minimax_h3_audio_vae_fp32.safetensors', 'vae/minimax/minimax_h3_audio_vae_fp32.safetensors',
     605254808, '8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48'),
    ('Comfy-Org/MiniMax-H3', '4cc1d817b6184899b41293954329f576cb5ae86b',
     'loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors', 'loras/MiniMaxH3/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors',
     1956193000, '2339acdf19bfe123f46b971ea35d367a84adb85de43627e1eceafa5a5b2b111e'),
]


def download(url, target, size=None, sha256=None):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + '.partial')
    if target.exists():
        if size and target.stat().st_size != size:
            raise RuntimeError(f'Existing file has unexpected size: {target}')
        if not sha256 or hashlib.file_digest(target.open('rb'), 'sha256').hexdigest() == sha256:
            print(f'Already verified: {target.name}', flush=True)
            return
        raise RuntimeError(f'Existing file failed SHA256: {target}')
    for attempt in range(6):
        try:
            offset = partial.stat().st_size if partial.exists() else 0
            if size is not None and offset == size:
                break
            headers = {'Range': f'bytes={offset}-'} if offset else {}
            with requests.get(url, headers=headers, stream=True, timeout=(30, 90)) as response:
                response.raise_for_status()
                if offset and response.status_code != 206:
                    raise RuntimeError('Server did not honor the resume request')
                if offset and not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                    raise RuntimeError('Invalid Content-Range')
                total = size or offset + int(response.headers.get('Content-Length', 0))
                last_report = time.monotonic()
                started = last_report
                received = 0
                with partial.open('ab' if offset else 'wb') as stream:
                    for block in response.iter_content(8 * 1024 * 1024):
                        stream.write(block)
                        offset += len(block)
                        received += len(block)
                        if time.monotonic() - last_report >= 20:
                            print(f'{target.name}: {offset / 1e9:.2f}/{total / 1e9:.2f} GB, '
                                  f'{received / 1e6 / (time.monotonic() - started):.1f} MB/s', flush=True)
                            last_report = time.monotonic()
            if size and partial.stat().st_size != size:
                raise RuntimeError('Incomplete download')
            break
        except (requests.RequestException, RuntimeError) as error:
            print(f'Download attempt {attempt + 1}: {error}', flush=True)
            if attempt == 5:
                raise
            time.sleep(3)
    if sha256:
        print(f'Checking SHA256: {target.name}', flush=True)
        with partial.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != sha256:
            raise RuntimeError(f'SHA256 mismatch: {actual}')
    partial.replace(target)
    print(f'Complete: {target}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--file', type=int)
    parser.add_argument('--url')
    parser.add_argument('--target')
    args = parser.parse_args()
    if args.url:
        download(args.url, args.target)
    else:
        manifest = []
        for index, (repo, revision, remote, local, size, sha) in enumerate(FILES):
            manifest.append(dict(repo=repo, revision=revision, remote=remote, path=str(MODELS / local), size=size, sha256=sha))
            if args.file is None or args.file == index:
                download(f'https://huggingface.co/{repo}/resolve/{revision}/{remote}', MODELS / local, size, sha)
        (ROOT / 'deployment/model-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
