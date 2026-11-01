"""Measure what the 9B can actually read out of a video, if anything.

The backend advertises `modalities: {"video": true}` and the gateway forwards `video_url` /
`input_video`, so "can it" is an empirical question and this answers it with clips whose
contents are known in advance. Nothing is guessed: each clip carries a fact the model either
reports or does not, and the raw replies are printed so the reading can be checked by eye.

    & ..\\ComfyUI-BSAI_v010\\python\\python.exe -X utf8 probe_video_vision.py
    & ..\\ComfyUI-BSAI_v010\\python\\python.exe -X utf8 probe_video_vision.py --keep   # keep the clips

Clips (8 fps, 1 s per item so a low sampling rate still catches every item):

    digits.mp4   0 1 2 3 4 5, one per second      -> "read the digits"
    colors.mp4   red, green, blue, yellow, purple -> "list the colours in order"
    motion.mp4   a red disc travelling left→right -> "which way does it move"

The last one is the interesting case: a stills-per-second model can name the colours but
usually cannot report motion or order reliably, and that distinction is exactly what
"can it recognise video" should mean.
"""

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = "http://127.0.0.1:8082"
CLIPS = ROOT / "logs" / "video-probe"
WIDTH, HEIGHT, FPS = 448, 448, 8

COLORS = {
    "red": (0, 0, 255), "green": (0, 200, 0), "blue": (255, 0, 0),
    "yellow": (0, 220, 220), "purple": (200, 0, 160), "white": (245, 245, 245),
    "black": (0, 0, 0),
}


def post(path, payload, timeout=600):
    request = urllib.request.Request(UPSTREAM + path,
                                     data=json.dumps(payload).encode("utf-8"), method="POST")
    request.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return {"error": error.read().decode("utf-8", "replace")[:400], "status": error.code}


# ----------------------------------------------------------------- clip making

def write_clip(path, frames):
    """Write frames (numpy BGR arrays) as an mp4 next to nothing else."""
    import cv2
    import numpy as np

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (WIDTH, HEIGHT))
    if not writer.isOpened():
        raise RuntimeError("OpenCV could not open a video writer for " + str(path))
    for frame in frames:
        writer.write(frame)
    writer.release()
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("no clip was written to " + str(path))
    return path


def blank(color=(0, 0, 0)):
    import numpy as np
    return np.full((HEIGHT, WIDTH, 3), color, dtype=np.uint8)


def digits_clip():
    import cv2
    frames = []
    for digit in "012345":
        for _ in range(FPS):
            frame = blank(COLORS["black"])
            cv2.putText(frame, digit, (int(WIDTH * 0.34), int(HEIGHT * 0.68)),
                        cv2.FONT_HERSHEY_SIMPLEX, 5.0, COLORS["white"], 14, cv2.LINE_AA)
            frames.append(frame)
    return frames


def colors_clip():
    order = ["red", "green", "blue", "yellow", "purple"]
    frames = []
    for name in order:
        for _ in range(FPS):
            frames.append(blank(COLORS[name]))
    return frames, order


def motion_clip():
    import cv2
    frames = []
    total = FPS * 6
    for index in range(total):
        frame = blank(COLORS["black"])
        x = int(WIDTH * (0.15 + 0.7 * index / (total - 1)))
        cv2.circle(frame, (x, HEIGHT // 2), 46, COLORS["red"], -1)
        frames.append(frame)
    return frames


# ----------------------------------------------------------------- asking

def as_data_url(path):
    mime = "video/mp4" if path.suffix.lower() == ".mp4" else "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def ask_video(path, question, max_tokens=420, raw=False):
    """One video question. `raw=True` sends a file path instead of a data URL.

    The content type is `input_video`, not `video_url`: the backend answers
    `unsupported content[].type` for the latter, and that error is what "it cannot read video"
    looked like until it was probed properly.

    A generous token budget matters for reading the answer: this model narrates the timeline
    ("从 0m2.00s 到 0m4.00s ..."), so a small limit truncates a *correct* reply mid-sentence
    and makes the model look like it failed. The first run of this probe did exactly that.
    """
    reference = str(path) if raw else as_data_url(path)
    payload = {
        "messages": [{"role": "user", "content": [
            {"type": "input_video", "input_video": {"url": reference}},
            {"type": "text", "text": question},
        ]}],
        "max_tokens": max_tokens, "temperature": 0.2, "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    started = time.monotonic()
    body = post("/v1/chat/completions", payload)
    elapsed = round(time.monotonic() - started, 1)
    if "error" in body:
        return None, elapsed, str(body.get("error"))[:200]
    choices = body.get("choices") or [{}]
    message = (choices[0] or {}).get("message") or {}
    text = (message.get("content") or message.get("reasoning_content") or "").strip()
    return text, elapsed, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep the generated clips")
    parser.add_argument("--only", default="", help="run only this clip: digits|colors|motion")
    args = parser.parse_args()

    try:
        import cv2  # noqa: F401
    except ImportError as exc:
        print("OpenCV is required to write the test clips:", exc)
        return 2

    CLIPS.mkdir(parents=True, exist_ok=True)
    made = {}
    print("生成测试片…")
    if args.only in ("", "digits"):
        made["digits"] = (write_clip(CLIPS / "digits.mp4", digits_clip()), ["0", "1", "2", "3", "4", "5"])
    if args.only in ("", "colors"):
        frames, order = colors_clip()
        made["colors"] = (write_clip(CLIPS / "colors.mp4", frames), order)
    if args.only in ("", "motion"):
        made["motion"] = (write_clip(CLIPS / "motion.mp4", motion_clip()), ["left", "right"])
    for name, (path, _) in made.items():
        print(f"  {name:7s} {path.stat().st_size / 1024:.0f} KiB  {path}")

    questions = {
        "digits": "这段视频里依次出现了哪些数字？按顺序逐个写出。",
        "colors": "这段视频里依次出现了哪些颜色？按出现顺序用中文列出。",
        "motion": "视频里那个红色圆点在移动吗？如果是，它是从左往右还是从右往左？",
    }
    expected = {
        "digits": lambda text: [d for d in "012345" if d in text],
        "colors": lambda text: [name for name in ("红", "绿", "蓝", "黄", "紫") if name in text],
        "motion": lambda text: ("右" in text, "左" in text),
    }

    print("\n=== 逐个提问 ===")
    results = {}
    for name, (path, truth) in made.items():
        text, elapsed, error = ask_video(path, questions[name])
        results[name] = {"reply": text, "seconds": elapsed, "error": error, "truth": truth}
        print(f"\n[{name}] {elapsed}s")
        if error:
            print("  失败:", error)
            continue
        print("  Q:", questions[name])
        print("  A:", (text or "(空)")[:300].replace("\n", " ⏎ "))
        if name == "colors":
            found = expected[name](text)
            print(f"  真值 {truth} → 命中顺序 {found}  完全一致={found == ['红','绿','蓝','黄','紫']}")
        elif name == "digits":
            found = expected[name](text)
            print(f"  真值 {truth} → 提到 {found}  完全一致={found == truth}")
        else:
            right, left = expected[name](text)
            print(f"  真值 从左往右 → 说对了向右={right}  (提到向左={left})")

    # The same question with a bare file path, so "failed" can be told apart from
    # "the transport shape was wrong". Measured: a path is rejected ("Invalid base64 value"),
    # so the shape that works is a data URL.
    if "colors" in made:
        text, elapsed, error = ask_video(made["colors"][0], questions["colors"], raw=True)
        print(f"\n[colors / 文件路径形式] {elapsed}s  （预期失败：本后端只接受 data URL）")
        print("  A:", (text or error or "(空)")[:200].replace("\n", " ⏎ "))

    report = ROOT / "logs" / "video-probe-report.json"
    report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n原始回复已写入 {report}")
    if not args.keep:
        for path, _ in made.values():
            path.unlink(missing_ok=True)
        print("测试片已删除（--keep 可保留）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
