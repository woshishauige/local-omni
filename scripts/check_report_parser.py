"""Check the report parser against the payload the director really returns."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from app.video import video_routes  # noqa: E402

REAL = {
    "50": {
        "video_path": [
            r"D:\AI\ComfyUI-BSAI_v010\ComfyUI\output\video\H3-30s\tianmen-duel-30s\shot_01.mp4"
        ],
        "checkpoint_report": [
            '{\n  "video": "D:\\\\AI\\\\ComfyUI-BSAI_v010\\\\ComfyUI\\\\output\\\\video'
            '\\\\H3-30s\\\\tianmen-duel-30s\\\\shot_01.mp4",\n  "completed_shots": [\n    1\n  ],'
            '\n  "next_shot": 2,\n  "seconds": 7.5,\n  "kept_media": [\n    "shot_01.mp4"\n  ]\n}'
        ],
    },
    "51": {"text": ["shot 1/4 done"]},
}

# The shape seen in the failed run: counts arrive as ints and the report holds the path.
INTS_FIRST = {
    "50": {"checkpoint_report": ['{"video": "X:/out/master.mp4", "completed_shots": [1]}']},
}

# Only the report, no video_path slot at all.
REPORT_ONLY = {
    "50": {"checkpoint_report": ['{"video": "X:/out/movie.mp4", "next_shot": 2}']},
}

failures = []
for label, payload, want in (
    ("video_path present", REAL, "shot_01.mp4"),
    ("report carries the path", REPORT_ONLY, "movie.mp4"),
    ("ints must not become the report", INTS_FIRST, "master.mp4"),
):
    video, report = video_routes._read_report(payload)
    ok = bool(video) and video.endswith(want)
    print(f"  {label:<32} video={str(video)[-34:]!r} ok={ok}")
    if not ok:
        failures.append(label)
    if report is None:
        failures.append(f"{label}: report missing")

# A payload with no path anywhere must report no video rather than guessing.
video, _ = video_routes._read_report({"50": {"checkpoint_report": ["shots=2"]}})
print(f"  {'no path anywhere':<32} video={video!r} ok={video is None}")
if video is not None:
    failures.append("a report without a path must not invent one")

if failures:
    print("\nFAILURES:", failures)
    raise SystemExit(1)
print("\nreport parser OK")

