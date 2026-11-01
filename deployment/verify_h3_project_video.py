import argparse
import hashlib
import json
from pathlib import Path

import av
import soundfile as sf

parser = argparse.ArgumentParser()
parser.add_argument('project', type=Path)
args = parser.parse_args()
manifest = json.loads((args.project / 'project.json').read_text(encoding='utf-8'))
settings, records = manifest['settings'], manifest['clips']
expected_frames = sum(row['frames'] for row in records)
expected_samples = round(expected_frames / 24 * records[0]['sample_rate'])
clip_pixels = hashlib.sha256()
for row in records:
    with av.open(str(args.project / row['video'])) as source:
        count = 0
        for frame in source.decode(video=0):
            clip_pixels.update(frame.to_ndarray(format='rgb24').tobytes())
            count += 1
        assert count == row['frames'], (row['index'], count)
pcm_samples = sum(sf.info(str(args.project / row['audio'])).frames for row in records)
assert abs(pcm_samples - expected_samples) <= 1, (pcm_samples, expected_samples)

master_pixels = hashlib.sha256()
with av.open(str(args.project / 'master.mp4')) as video:
    stream = video.streams.video[0]
    assert (stream.width, stream.height, float(stream.average_rate)) == (settings['width'], settings['height'], 24.0)
    count = 0
    for count, frame in enumerate(video.decode(video=0), 1):
        assert abs(float(frame.pts * frame.time_base) - (count - 1) / 24) < 0.0001
        master_pixels.update(frame.to_ndarray(format='rgb24').tobytes())
    assert count == expected_frames, (count, expected_frames)
    metadata = video.metadata
assert master_pixels.digest() == clip_pixels.digest(), 'Assembled pixels differ from the saved shots.'
with av.open(str(args.project / 'master.mp4')) as video:
    stream = video.streams.audio[0]
    audio_duration = float(stream.duration * stream.time_base)
    assert abs(audio_duration - expected_frames / 24) < 0.04, audio_duration
    decoded_samples = sum(frame.samples for frame in video.decode(audio=0))
    assert expected_samples <= decoded_samples <= expected_samples + 1024, (decoded_samples, expected_samples)
workflow = json.loads(metadata['workflow'])
prompt = json.loads(metadata['prompt'])
assert prompt['50']['inputs']['width'] == settings['width']
assert prompt['50']['inputs']['steps'] == settings['steps']
result = {
    'project': str(args.project), 'frames': expected_frames,
    'seconds': expected_frames / 24, 'audio_seconds': audio_duration,
    'pcm_samples': pcm_samples, 'aac_decoded_samples': decoded_samples,
    'pixels_identical_to_clips': True, 'continuous_video_timestamps': True,
    'embedded_workflow_node_count': len(workflow['nodes']),
    'embedded_prompt_nodes': list(prompt),
}
target = Path('D:/AI/deployment/logs/768x512') / (args.project.name + '-media-verification.json')
target.write_text(json.dumps(result, indent=2), encoding='utf-8')
print(json.dumps(result, indent=2))
