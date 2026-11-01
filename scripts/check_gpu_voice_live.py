"""Live acceptance for voice input and the GPU gate against the running gateway.

Run while the service is up:  python check_gpu_voice_live.py

The recognition leg posts the real sample WAV (made with Windows TTS) and checks the
words come back, because that is the only part of a speech path worth asserting. The
GPU leg checks the endpoint contract and that preemption is owner-only and reversible.
"""

import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import http.cookiejar

BASE = "http://127.0.0.1:8081"
ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "logs" / "asr-sample.wav"
results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {label:52s} {detail}")


def opener():
    link = (ROOT / "logs" / "admin-link.txt").read_text(encoding="ascii").strip()
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.open(link, timeout=25).read()
    return op


def call(op, method, path, body=None, raw=None, headers=None, timeout=180):
    hdrs = dict(headers or {})
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    request = urllib.request.Request(BASE + path, data=data, headers=hdrs, method=method)
    try:
        with op.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main():
    if not SAMPLE.is_file():
        print(f"缺少测试音频 {SAMPLE}（用 Windows TTS 生成一段中文即可）")
        return 1
    owner = opener()

    # --- voice -----------------------------------------------------------------
    status, payload = call(owner, "GET", "/local/voice/status")
    info = json.loads(payload)
    check("voice status reports a local model", status == 200 and info.get("ready") is True,
          f"model={info.get('model')} device={info.get('device')}")

    started = time.time()
    status, payload = call(owner, "POST", "/local/voice/transcribe?language=zh",
                           raw=SAMPLE.read_bytes(),
                           headers={"Content-Type": "audio/wav"})
    elapsed = time.time() - started
    heard = json.loads(payload) if status == 200 else {}
    text = heard.get("text", "")
    check("a real recording is transcribed", status == 200 and "图片" in text and "放大" in text,
          f"{elapsed:.1f}s wall · {heard.get('seconds')}s recognize · audio {heard.get('duration_seconds')}s")
    check("the transcript is simplified Chinese", "這" not in text and "圖" not in text, text)
    check("recognition ran on the CPU, not the GPU", info.get("device") == "cpu")

    status, _ = call(owner, "POST", "/local/voice/transcribe", raw=b"not audio")
    check("garbage audio is refused, not crashed", status == 400, f"HTTP {status}")

    status, payload = call(owner, "POST", "/local/voice/transcribe")
    check("an empty body is refused", status == 400 and "没有收到音频" in payload.decode("utf-8", "replace"))

    # --- live streaming: text must appear before the recording ends -----------
    import struct
    import wave

    with wave.open(str(SAMPLE), "rb") as handle:
        pcm = handle.readframes(handle.getnframes())
        rate = handle.getframerate()
    chunk = int(0.25 * rate) * 2
    first_text_bytes = None
    seen = ""
    started = time.time()
    for offset in range(0, len(pcm), chunk):
        piece = pcm[offset:offset + chunk]
        status, payload = call(owner, "POST", "/local/voice/stream?language=zh", raw=piece,
                               headers={"Content-Type": "application/octet-stream"}, timeout=60)
        if status != 200:
            break
        snapshot = json.loads(payload)
        if snapshot.get("text"):
            seen = snapshot["text"]
            first_text_bytes = offset + len(piece)
            break
        if offset + len(piece) >= len(pcm) // 2:
            # Half the clip is in: poll (an empty body is a poll, not audio) until the
            # running reading lands.
            for _ in range(40):
                time.sleep(0.25)
                status, payload = call(owner, "POST", "/local/voice/stream?language=zh", raw=b"",
                                       headers={"Content-Type": "application/octet-stream"}, timeout=60)
                if status != 200:
                    break
                snapshot = json.loads(payload)
                if snapshot.get("text"):
                    seen = snapshot["text"]
                    first_text_bytes = offset + len(piece)
                    break
            break
    check("live streaming returns text before the clip is finished",
          bool(seen) and first_text_bytes is not None and first_text_bytes < len(pcm),
          f"{first_text_bytes}/{len(pcm)} byte(s) sent · {time.time() - started:.1f}s · {seen!r}")

    # Send the rest and let the server settle the phrase by itself: no more audio is
    # needed for the last words to appear, which is the whole point of the idle finalizer.
    for offset in range(first_text_bytes or 0, len(pcm), chunk):
        call(owner, "POST", "/local/voice/stream?language=zh", raw=pcm[offset:offset + chunk],
             headers={"Content-Type": "application/octet-stream"}, timeout=60)
    settled = ""
    for _ in range(80):
        time.sleep(0.25)
        status, payload = call(owner, "POST", "/local/voice/stream?language=zh", raw=b"",
                               headers={"Content-Type": "application/octet-stream"}, timeout=60)
        if status != 200:
            break
        snapshot = json.loads(payload)
        settled = snapshot.get("text", "")
        if snapshot.get("finished"):
            break
    check("the server finalizes the phrase on its own after the audio stops",
          "输出目录" in settled, f"{settled!r}")

    status, payload = call(owner, "POST", "/local/voice/stream/finish?language=zh", raw=b"",
                           headers={"Content-Type": "application/octet-stream"}, timeout=120)
    final = json.loads(payload) if status == 200 else {}
    check("finishing the stream returns the whole transcript",
          status == 200 and "图片" in final.get("text", "") and "放大两倍" in final.get("text", ""),
          f"{final.get('runs')} reading(s) · {final.get('text')!r}")

    status, payload = call(owner, "POST", "/local/voice/stream/finish?language=zh", raw=b"")
    check("finishing twice says the recording is over", status == 409, f"HTTP {status}")

    # --- GPU gate --------------------------------------------------------------
    status, payload = call(owner, "GET", "/local/gpu")
    gpu = json.loads(payload)
    check("gpu status is readable by the owner", status == 200 and "threshold_mib" in gpu,
          f"free={gpu.get('free_mib')} MiB · threshold={gpu.get('threshold_mib')} MiB · tight={gpu.get('tight')}")
    check("gpu status names the preempt parameter", gpu.get("preempt_param") == "gpu")

    status, payload = call(owner, "POST", "/local/gpu/preempt", {"mode": "queue"})
    body = json.loads(payload)
    check("owner can hold visitors off the card", status == 200 and body.get("preempting") is True,
          f"mode={body.get('mode')}")

    status, payload = call(owner, "GET", "/local/gpu")
    check("the hold shows up in the status", json.loads(payload).get("preempting") is True)

    status, payload = call(owner, "POST", "/local/gpu/preempt", {"mode": "interrupt"})
    body = json.loads(payload)
    stopped = body.get("stopped") or {}
    check("interrupt mode reports what it stopped", status == 200 and "image" in stopped,
          f"image={stopped.get('image')} video={stopped.get('video')} queue={stopped.get('queue')} "
          f"in_flight={len(stopped.get('in_flight') or [])}")

    status, payload = call(owner, "POST", "/local/gpu/release")
    check("the owner can hand the card back", status == 200 and json.loads(payload).get("preempting") is False)

    status, _ = call(owner, "POST", "/local/gpu/preempt", {"mode": "whatever"})
    check("an unknown preempt mode is refused", status == 400, f"HTTP {status}")

    # The preempt parameter is what the console replays a refused request with. The
    # gateway is asked for text first: it takes no VRAM while the model already holds
    # it, so a 200 here proves the parameter is accepted and the request was admitted.
    body = json.dumps({"messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}).encode()
    status, payload = call(owner, "POST", "/v1/chat/completions?gpu=preempt", raw=body,
                           headers={"Content-Type": "application/json"})
    check("the preempt parameter is accepted on a real request", status == 200,
          f"HTTP {status} {payload[:90].decode('utf-8', 'replace')}")

    status, payload = call(owner, "POST", "/v1/chat/completions?gpu=maybe", raw=body,
                           headers={"Content-Type": "application/json"})
    check("a bogus preempt value is refused", status == 400,
          f"HTTP {status} {payload[:70].decode('utf-8', 'replace')}")

    # A caller with no owner standing must not reach the gate controls at all.
    anonymous = urllib.request.build_opener()
    status, _ = call(anonymous, "GET", "/local/gpu")
    check("the gate controls are owner-only", status in {401, 403}, f"HTTP {status}")

    status, _ = call(anonymous, "POST", "/v1/chat/completions?gpu=preempt", raw=body,
                     headers={"Content-Type": "application/json"})
    check("a visitor cannot preempt the card", status in {400, 401, 403},
          f"HTTP {status}")

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        # This script drives the streaming path, which starts a recognition worker in a
        # separate process. It must be stopped here or the check leaks one per run.
        sys.path.insert(0, str(ROOT))
        try:
            from app.voice import voice_input

            voice_input.shutdown_worker()
        except Exception:  # noqa: BLE001 - cleanup must not mask the result
            pass
    raise SystemExit(code)
