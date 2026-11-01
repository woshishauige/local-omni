"""Real usage smoke test against the already running model, with an isolated DB.

The temporary gateway uses port 18083 and never starts or stops the user's model.
Production logs/prices/chat data are not modified. Run with the bundled Python.
"""
import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SOURCE = Path(__file__).resolve().parent
URL = "http://127.0.0.1:18083"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


async def serve(directory):
    import httpx
    import uvicorn
    import mode_server as module
    from fastapi import HTTPException
    from usage_store import UsageStore

    module.usage_store = UsageStore(Path(directory) / "usage.sqlite3")
    module.manager.state = "ready"
    module.manager.revision = 1
    module.manager.client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5), trust_env=False)
    def local_origin(request):
        if request.headers.get('origin') not in (None, URL, "http://localhost:18083"):
            raise HTTPException(403)
    module.require_local_origin = local_origin
    # The test has no ownership of the real backend: bypass production lifespan
    # and prevent UI mode/shutdown actions from altering the user's model.
    class ReadOnlyControls:
        def __init__(self, app):
            self.app = app
        async def __call__(self, scope, receive, send):
            if scope.get('path') in {'/local/mode', '/local/recover', '/local/shutdown'}:
                response = module.JSONResponse({'detail': 'Isolated usage test'}, status_code=403)
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)
    app = ReadOnlyControls(module.app)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=18083, lifespan="off", log_level="warning"))
    try:
        await server.serve()
    finally:
        await module.manager.client.aclose()


def request(path, payload=None):
    req = urllib.request.Request(URL + path, data=None if payload is None else json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=120) as response:
        raw = response.read()
        return json.loads(raw) if 'json' in response.headers.get('content-type', '') else raw


def main():
    import qwen_service
    status = qwen_service.gateway_status()
    if not status or status.get('state') != 'ready' or status.get('active_requests'):
        raise RuntimeError('Wait until the existing local model is ready and idle before this test.')
    with socket.socket() as connection:
        if connection.connect_ex(('127.0.0.1', 18083)) == 0:
            raise RuntimeError('Test port 18083 is already in use.')
    directory = Path(tempfile.mkdtemp(prefix='qwen-usage-live-'))
    result = {'test_directory': str(directory), 'checks': []}
    print(json.dumps(result), flush=True)
    with (directory / 'gateway.log').open('wb') as log:
        child = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()), '--serve', str(directory)],
                                 cwd=SOURCE, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    initial = request('/local/usage')
                    break
                except OSError:
                    if time.monotonic() >= deadline or child.poll() is not None:
                        raise RuntimeError('Test gateway did not start; see ' + str(directory))
                    time.sleep(0.2)
            assert initial['total'] == 0
            request('/local/usage/preferences', {'input_per_million': 10, 'output_per_million': 50})
            payload = {'model': 'qwen3.8-27b-local', 'messages': [{'role': 'user', 'content': '只回答：统计正常'}], 'max_tokens': 12, 'temperature': 0}
            reply = request('/v1/chat/completions', payload)
            assert reply['usage']['prompt_tokens'] > 0 and reply['usage']['completion_tokens'] > 0
            result['checks'].append('real non-streaming authoritative usage')
            stream = request('/v1/chat/completions', dict(payload, stream=True))
            assert b'[DONE]' in stream and b'"usage"' in stream
            result['checks'].append('real streaming final usage injected and captured')
            data = request('/local/usage')
            assert data['total'] == 2, data
            rows = data['rows']
            streamed = next(row for row in rows if row['stream'])
            regular = next(row for row in rows if not row['stream'])
            assert all(row['status'] == 'success' for row in rows), rows
            assert regular['input_tokens'] == reply['usage']['prompt_tokens']
            assert regular['output_tokens'] == reply['usage']['completion_tokens']
            final_usage = next(json.loads(line[6:])['usage'] for line in stream.decode().splitlines()
                               if line.startswith('data: {') and json.loads(line[6:]).get('usage'))
            assert streamed['input_tokens'] == final_usage['prompt_tokens']
            assert streamed['output_tokens'] == final_usage['completion_tokens']
            assert regular['ttft_ms'] is None and streamed['ttft_ms'] is not None
            assert all(row['cache_write_tokens'] is None for row in rows)
            assert data['summary']['total_tokens'] == sum(row['input_tokens'] + row['output_tokens'] for row in rows)
            assert data['summary']['cost'] > 0
            result['checks'].append('exact per-request counters, total, timing and estimated cost')
            request('/local/usage/preferences', {'input_per_million': 0, 'output_per_million': 0})
            assert request('/local/usage')['summary']['cost'] == data['summary']['cost']
            result['checks'].append('preference change preserves historical cost')
            try:
                request('/v1/chat/completions', [])
                raise AssertionError('Invalid body must be rejected')
            except urllib.error.HTTPError as error:
                assert error.code == 400
            errors = request('/local/usage?status=error')
            assert errors['total'] == 1 and errors['rows'][0]['http_status'] == 400
            assert errors['rows'][0]['input_tokens'] is None
            result['checks'].append('failed request logged with unknown usage')
            assert request('/local/usage?model=does-not-exist')['total'] == 0
            csv = request('/local/usage/export?status=success')
            assert csv.startswith(b'\xef\xbb\xbf') and b'qwen3.8-27b-local' in csv
            result['checks'].append('filtering and Excel-compatible CSV export')
            from usage_store import UsageStore
            reopened = UsageStore(directory / 'usage.sqlite3').query({})
            assert reopened['total'] == 3
            result['checks'].append('records persist across database reopen')
            result['rows'] = data['rows']
            result['summary'] = data['summary']
        finally:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--serve':
        asyncio.run(serve(sys.argv[2]))
    else:
        main()
