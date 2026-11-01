from download_models import FILES, MODELS, download

for index in (3, 2, 4):
    repo, revision, remote, local, size, sha256 = FILES[index]
    download(f'https://huggingface.co/{repo}/resolve/{revision}/{remote}', MODELS / local, size, sha256)
