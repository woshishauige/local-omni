import argparse
import json
from pathlib import Path

import av
import numpy as np
from PIL import Image, ImageDraw

parser = argparse.ArgumentParser()
parser.add_argument('path')
parser.add_argument('--join-frame', type=int)
parser.add_argument('--name')
args = parser.parse_args()
path = Path(args.path)
thumbnails = []
changes = []
deviations = []
previous = None
with av.open(str(path)) as container:
    stream = container.streams.video[0]
    report = dict(path=str(path), width=stream.width, height=stream.height,
                  fps=float(stream.average_rate), codec=stream.codec_context.name)
    for frame in container.decode(video=0):
        pixels = frame.to_ndarray(format='rgb24')
        deviations.append(float(pixels.std()))
        if previous is not None:
            changes.append(float(np.abs(pixels.astype(np.float32) - previous).mean()))
        previous = pixels.astype(np.float32)
        thumbnail = Image.fromarray(pixels)
        thumbnail.thumbnail((256, 144))
        thumbnails.append(thumbnail)
if not thumbnails:
    raise RuntimeError('The video contains no decodable frames')
report.update(frames=len(thumbnails), duration=len(thumbnails) / report['fps'],
              mean_pixel_std=float(np.mean(deviations)),
              mean_frame_change=float(np.mean(changes)) if changes else 0)
if args.join_frame is not None:
    report['join_frame'] = args.join_frame
    report['join_frame_change'] = changes[args.join_frame - 1]
    report['frame_change_p95'] = float(np.percentile(changes, 95))

audio_count = 0
audio_sum_squares = 0.0
audio_peak = 0.0
with av.open(str(path)) as container:
    report['audio_tracks'] = len(container.streams.audio)
    if container.streams.audio:
        report['audio_sample_rate'] = container.streams.audio[0].codec_context.sample_rate
        for frame in container.decode(audio=0):
            samples = frame.to_ndarray()
            scale = np.iinfo(samples.dtype).max if np.issubdtype(samples.dtype, np.integer) else 1.0
            samples = samples.astype(np.float32) / scale
            audio_count += samples.size
            audio_sum_squares += float(np.square(samples).sum())
            audio_peak = max(audio_peak, float(np.abs(samples).max()))
report.update(audio_rms=(audio_sum_squares / audio_count) ** 0.5 if audio_count else 0,
              audio_peak=audio_peak)

sheet = Image.new('RGB', (1024, 336), 'white')
draw = ImageDraw.Draw(sheet)
for tile, index in enumerate(np.linspace(0, len(thumbnails) - 1, 8, dtype=int)):
    x, y = (tile % 4) * 256, (tile // 4) * 168
    sheet.paste(thumbnails[index], (x, y))
    draw.text((x + 5, y + 146), f'Frame {index} / {index / report["fps"]:.2f}s', fill='black')
output = Path('D:/AI/deployment/logs') / (args.name or path.stem)
sheet.save(output.with_suffix('.png'))
output.with_suffix('.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2), flush=True)
print('Contact sheet:', output.with_suffix('.png'), flush=True)
