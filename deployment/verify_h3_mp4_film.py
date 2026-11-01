import argparse
import hashlib
import json
from pathlib import Path

import av
import numpy as np
from PIL import Image, ImageDraw


parser = argparse.ArgumentParser()
parser.add_argument('project', type=Path)
args = parser.parse_args()
clips = [args.project / f'shot_{index:02d}.mp4' for index in range(1, 5)]
expected_files = {path.name for path in clips} | {'master.mp4'}
assert {path.name for path in args.project.iterdir()} == expected_files
records = []
clip_pixels = hashlib.sha256()
clip_hashes = []
for index, path in enumerate(clips, 1):
    with path.open('rb') as source:
        clip_hashes.append(hashlib.file_digest(source, 'sha256').hexdigest())
    with av.open(str(path)) as source:
        record = json.loads(source.metadata['h3_checkpoint'])
        assert record['format'] == 'h3_mp4_checkpoint_v1'
        assert (record['shot_index'], record['frames'], record['steps']) == (index, 180, 24)
        stream = source.streams.video[0]
        assert (stream.width, stream.height, float(stream.average_rate)) == (768, 512, 24)
        assert len(source.streams.audio) == 1
        count = 0
        for count, frame in enumerate(source.decode(video=0), 1):
            assert abs(float(frame.pts * frame.time_base) - (count - 1) / 24) < 0.0001
            clip_pixels.update(frame.to_ndarray(format='rgb24').tobytes())
        assert count == 180, (path.name, count)
    with av.open(str(path)) as source:
        stream = source.streams.audio[0]
        assert abs(float(stream.duration * stream.time_base) - 7.5) < 0.04
        assert 240000 <= sum(frame.samples for frame in source.decode(audio=0)) <= 241024
    records.append(record)

for index, record in enumerate(records[1:], 1):
    assert record['previous_sha256'] == clip_hashes[index - 1], index + 1
    assert record['reference_sha256'] == clip_hashes[0], index + 1

master_pixels = hashlib.sha256()
changes = []
previous = None
join_frames = [boundary + offset for boundary in (180, 360, 540) for offset in (-12, -1, 0, 1, 12)]
join_images = {}
with av.open(str(args.project / 'master.mp4')) as source:
    metadata = source.metadata
    assert json.loads(metadata['h3_film']) == records
    stream = source.streams.video[0]
    assert (stream.width, stream.height, float(stream.average_rate)) == (768, 512, 24)
    count = 0
    for count, frame in enumerate(source.decode(video=0), 1):
        assert abs(float(frame.pts * frame.time_base) - (count - 1) / 24) < 0.0001
        pixels = frame.to_ndarray(format='rgb24')
        master_pixels.update(pixels.tobytes())
        if count - 1 in join_frames:
            join_images[count - 1] = Image.fromarray(pixels).resize((256, 171), Image.Resampling.LANCZOS)
        small = pixels[::4, ::4].astype(np.float32)
        if previous is not None:
            changes.append(float(np.abs(small - previous).mean()))
        previous = small
    assert count == 720
assert master_pixels.digest() == clip_pixels.digest(), 'Assembly changed the decoded picture.'

with av.open(str(args.project / 'master.mp4')) as source:
    stream = source.streams.audio[0]
    audio_seconds = float(stream.duration * stream.time_base)
    assert abs(audio_seconds - 30) < 0.04, audio_seconds
    audio = np.concatenate([frame.to_ndarray() for frame in source.decode(audio=0)], axis=1)
    assert 960000 <= audio.shape[1] <= 961024
    assert float(np.sqrt(np.square(audio).mean())) > 0.001

workflow = json.loads(metadata['workflow'])
prompt = json.loads(metadata['prompt'])
assert prompt['50']['class_type'] == 'H3MP4ShotEditor'
scratch_root = Path('D:/AI/ComfyUI-BSAI_v010/ComfyUI/temp')
assert not list(scratch_root.glob('h3-shot-*'))
assert not list(scratch_root.glob('h3-assemble-*'))
result = {
    'project': str(args.project), 'files': sorted(expected_files),
    'width': 768, 'height': 512, 'fps': 24, 'frames': count,
    'seconds': count / 24, 'audio_seconds': audio_seconds,
    'audio_rms': float(np.sqrt(np.square(audio).mean())),
    'pixels_identical_to_clips': True, 'continuous_video_timestamps': True,
    'context_and_reference_hashes_current': True,
    'join_frame_changes': {str(frame): changes[frame - 1] for frame in (180, 360, 540)},
    'frame_change_p95': float(np.percentile(changes, 95)),
    'embedded_workflow_node_count': len(workflow['nodes']),
    'shot_generation_seconds': [record['seconds_to_generate'] for record in records],
    'temporary_media_cleaned': True,
}
target = Path('D:/AI/deployment/logs/768x512') / (args.project.name + '-media-verification.json')
target.write_text(json.dumps(result, indent=2), encoding='utf-8')
sheet = Image.new('RGB', (1280, 591), 'white')
draw = ImageDraw.Draw(sheet)
for tile, frame in enumerate(join_frames):
    x, y = (tile % 5) * 256, (tile // 5) * 197
    sheet.paste(join_images[frame], (x, y))
    draw.text((x + 5, y + 175), f'Frame {frame} / {frame / 24:.3f}s', fill='black')
sheet.save(target.with_name(args.project.name + '-joins.png'))
print(json.dumps(result, indent=2))
