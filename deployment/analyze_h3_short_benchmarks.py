import json
from pathlib import Path

import av
import numpy as np
from PIL import Image, ImageDraw


BASE = Path('D:/AI/ComfyUI-BSAI_v010/ComfyUI/output/video/H3-30s')
CASES = ['h3-short-native20-20260909', 'h3-short-cache20-20260909', 'h3-short-cache16-20260909',
         'h3-short-native20-warm-20260909']
OUT = Path('D:/AI/deployment/logs/h3-short-bench-20260909')


def read_case(case: str) -> dict:
    path = BASE / case / 'master.mp4'
    checkpoint_path = BASE / case / 'shot_01.mp4'
    with av.open(str(checkpoint_path)) as container:
        video = container.streams.video[0]
        meta = json.loads(container.metadata.get('h3_checkpoint', '{}'))
    with av.open(str(path)) as container:
        frames = list(container.decode(video=0))
    picks = [0, len(frames) // 2, len(frames) - 1]
    images = [Image.fromarray(frames[i].to_ndarray(format='rgb24')) for i in picks]
    thumb_w = 384
    thumbs = []
    for image in images:
        ratio = thumb_w / image.width
        thumbs.append(image.resize((thumb_w, round(image.height * ratio)), Image.Resampling.LANCZOS))
    sheet = Image.new('RGB', (thumb_w * 3, thumbs[0].height + 34), 'white')
    draw = ImageDraw.Draw(sheet)
    for i, thumb in enumerate(thumbs):
        sheet.paste(thumb, (i * thumb_w, 34))
        draw.text((i * thumb_w + 8, 8), f'frame {picks[i]}', fill='black')
    frame_path = OUT / f'{case}-frames.jpg'
    sheet.save(frame_path, quality=92)
    return {
        'case': case,
        'video': str(path),
        'video_seconds': meta['frames'] / meta['fps'],
        'frames': meta['frames'],
        'generated_frames': meta['generated_frames'],
        'steps': meta['steps'],
        'seconds_to_generate': meta['seconds_to_generate'],
        'sec_per_output_frame': meta['seconds_to_generate'] / meta['frames'],
        'realtime_factor': (meta['frames'] / meta['fps']) / meta['seconds_to_generate'],
        'frame_sheet': str(frame_path),
    }


def main() -> None:
    results = [read_case(case) for case in CASES]
    # Same seed and prompt are used for all cases; quantify broad frame-level drift
    # without treating pixel identity as a quality score.
    videos = {}
    for case in CASES:
        with av.open(str(BASE / case / 'master.mp4')) as container:
            videos[case] = [frame.to_ndarray(format='rgb24').astype(np.float32)
                            for frame in container.decode(video=0)]
    baseline = videos[CASES[0]]
    for result in results[1:]:
        current = videos[result['case']]
        diffs = [np.mean(np.abs(a - b)) for a, b in zip(baseline, current)]
        result['mean_abs_pixel_delta_vs_native20'] = round(float(np.mean(diffs)), 3)
        result['max_abs_pixel_delta_vs_native20'] = round(float(np.max(diffs)), 3)
    report = OUT / 'results.json'
    report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(report)


if __name__ == '__main__':
    main()
