import copy
import json
from pathlib import Path

import requests

ROOT = Path('D:/AI')
COMFY = ROOT / 'ComfyUI-BSAI_v010/ComfyUI'
INFO = requests.get('http://127.0.0.1:8188/object_info', timeout=30).json()
OUTPUT = COMFY / 'user/default/workflows/H3-12GB-Local'
API = ROOT / 'deployment/api'
MODEL = 'minimax_h3\\MiniMax-H3-ref2va-curve-Q4_0.gguf'
CLIP = 'qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'
VIDEO_VAE = 'minimax\\minimax_h3_video_vae_fp16.safetensors'
AUDIO_VAE = 'minimax\\minimax_h3_audio_vae_fp32.safetensors'
QWEN = 'Qwen3.8-27B-ZeroRefusal-UD-IQ4_XS-V3-Final-MTP.gguf'
WIDTH, HEIGHT, FRAMES, STEPS = 768, 512, 192, 24
STORYBOARD = json.dumps({'shots': [
    {'prompt': 'integrated_multimodal_description: [Shot 1] A red toy car rolls slowly from left to right across a clean wooden table. Static close-up camera, natural daylight from a window, realistic materials. overall_soundscape: Soft wheel rolling and quiet room ambience. non_diegetic_music: None.'},
    {'prompt': 'integrated_multimodal_description: [Shot 2] The same red toy car continues rolling to the right across the same wooden table for the first second, then gradually slows to a gentle stop. Keep the same static camera, car design and daylight. overall_soundscape: Wheel rolling fades into quiet room ambience. non_diegetic_music: None.'},
    {'prompt': 'integrated_multimodal_description: [Shot 3] The same red toy car remains stopped on the same wooden table, then rolls slowly forward a short distance. The camera remains fixed and the daylight stays constant. overall_soundscape: Quiet room ambience and a short soft wheel rolling sound. non_diegetic_music: None.'},
]}, indent=2)
PROMPT = 'integrated_multimodal_description: [Shot 1] A red toy car rolls slowly from left to right across a clean wooden table in a bright room. Close-up, static camera, natural daylight, realistic materials. overall_soundscape: Soft wheel rolling sound and quiet room ambience. non_diegetic_music: None.'


class Graph:
    def __init__(self):
        self.prompt = {}
        self.positions = {}

    def add(self, key, kind, values, x, y, title=None):
        spec = INFO[kind]
        inputs = {}
        for name, definition in spec['input'].get('required', {}).items():
            opts = definition[1] if len(definition) > 1 else {}
            if name in values:
                inputs[name] = values[name]
            elif 'default' in opts:
                inputs[name] = opts['default']
            elif isinstance(definition[0], list) and definition[0]:
                inputs[name] = definition[0][0]
            elif opts.get('options'):
                first = opts['options'][0]
                inputs[name] = first['key'] if isinstance(first, dict) else first
            else:
                raise ValueError(f'Missing required input {kind}.{name}')
        inputs.update(values)
        self.prompt[str(key)] = {'class_type': kind, 'inputs': inputs, '_meta': {'title': title or spec.get('display_name', kind)}}
        self.positions[str(key)] = [x, y]
        return [str(key), 0]

    def save(self, name):
        nodes = []
        links = []
        by_id = {}
        origin = [min(0, *(position[axis] for position in self.positions.values())) for axis in (0, 1)]
        for order, (key, entry) in enumerate(self.prompt.items()):
            kind = entry['class_type']
            schema = INFO[kind]
            definitions = {**schema['input'].get('required', {}), **schema['input'].get('optional', {})}
            position = [self.positions[key][axis] - origin[axis] for axis in (0, 1)]
            node = {'id': int(key), 'type': kind, 'pos': position, 'size': [360, 320],
                    'flags': {}, 'order': order, 'mode': 0, 'inputs': [], 'outputs': [],
                    'properties': {'Node name for S&R': kind}, 'widgets_values': [], 'title': entry['_meta']['title']}
            multiline = 0
            for input_name, definition in definitions.items():
                input_type = definition[0]
                opts = definition[1] if len(definition) > 1 else {}
                primitive = isinstance(input_type, list) or input_type in ('STRING', 'INT', 'FLOAT', 'BOOLEAN', 'COMBO', 'COMFY_DYNAMICCOMBO_V3')
                socket_type = 'COMBO' if isinstance(input_type, list) else input_type
                value = entry['inputs'].get(input_name)
                connected = isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and value[0] in self.prompt
                if connected or not primitive or opts.get('forceInput'):
                    port = {'name': input_name, 'type': socket_type, 'link': None}
                    if primitive and not opts.get('forceInput'):
                        port['widget'] = {'name': input_name}
                    if connected:
                        link_id = len(links) + 1
                        port['link'] = link_id
                        links.append([link_id, int(value[0]), value[1], int(key), len(node['inputs']), socket_type])
                    node['inputs'].append(port)
                if primitive and not opts.get('forceInput'):
                    if connected or input_name not in entry['inputs']:
                        value = opts.get('default')
                        if value is None and isinstance(input_type, list):
                            value = input_type[0] if input_type else ''
                        if value is None and opts.get('options'):
                            first = opts['options'][0]
                            value = first['key'] if isinstance(first, dict) else first
                        if value is None:
                            value = {'STRING': '', 'INT': 0, 'FLOAT': 0.0, 'BOOLEAN': False}.get(input_type, 'auto')
                    node['widgets_values'].append(value)
                    if opts.get('control_after_generate'):
                        node['widgets_values'].append('fixed')
                    if opts.get('multiline'):
                        multiline += 1
            for index, output_type in enumerate(schema.get('output', [])):
                labels = schema.get('output_name', schema['output'])
                node['outputs'].append({'name': labels[index], 'type': output_type, 'links': [], 'slot_index': index})
            node['size'] = [430 if multiline else 330, max(180, 75 + len(node['widgets_values']) * 27 + multiline * 140)]
            if kind == 'PrimitiveStringMultiline':
                node['size'] = [520, 520]
            while any(position[0] < old['pos'][0] + old['size'][0] + 40
                      and position[0] + node['size'][0] + 40 > old['pos'][0]
                      and position[1] < old['pos'][1] + old['size'][1] + 45
                      and position[1] + node['size'][1] + 45 > old['pos'][1] for old in nodes):
                position[1] += 50
            by_id[int(key)] = node
            nodes.append(node)
        for link_id, origin, slot, target, target_slot, kind in links:
            by_id[origin]['outputs'][slot]['links'].append(link_id)
        workflow = {'last_node_id': max(by_id), 'last_link_id': len(links), 'nodes': nodes, 'links': links,
                    'groups': [], 'config': {}, 'extra': {'ds': {'scale': 0.65, 'offset': [35, 70]}}, 'version': 0.4}
        OUTPUT.mkdir(parents=True, exist_ok=True)
        API.mkdir(parents=True, exist_ok=True)
        (OUTPUT / f'{name}.json').write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding='utf-8')
        (API / f'{name}.json').write_text(json.dumps({'prompt': self.prompt, 'extra_data': {'extra_pnginfo': {'workflow': workflow}}}, ensure_ascii=False, indent=2), encoding='utf-8')
        print(name, len(nodes), 'nodes')


def qwen_nodes(g, prompt, system, x=0, y=0):
    g.add(101, 'QwenLT_ModelLoader', {
        '\u6a21\u578b\u7cfb\u5217': 'Qwen3.8-VL', '\u4e3b\u6a21\u578b': QWEN, '\u89c6\u89c9\u6295\u5f71mmproj': '\u65e0',
        '\u4e0a\u4e0b\u6587\u957f\u5ea6': 4096, 'GPU\u5c42\u6570': 36, 'KV\u7f13\u5b58K\u7c7b\u578b': 'q8_0', 'KV\u7f13\u5b58V\u7c7b\u578b': 'q8_0',
        'Flash Attention': '\u5f00\u542f', 'Qwen3.8\u63a8\u7406\u5f3a\u5ea6': 'low',
    }, x, y, 'Qwen 27B / 12GB')
    return g.add(102, 'QwenLT_ImageInfer', {
        'qwen\u6a21\u578b': ['101', 0], '\u8f93\u5165\u6a21\u5f0f': '\u6587\u672c', '\u63d0\u793a\u8bcd': prompt,
        '\u7cfb\u7edf\u63d0\u793a\u8bcd': system, '\u6700\u5927\u751f\u6210token': 512, '\u6700\u5927\u8fb9\u957f': 512,
        '\u8f93\u51fathink\u5757': False, '\u751f\u6210\u540e\u81ea\u52a8\u5378\u8f7d\u6a21\u578b': True,
    }, x + 460, y, 'Qwen Prompt / Auto Unload')


def h3_base(g, y=0):
    g.add(1, 'H3ModelLoaderAny', {'model_name': MODEL, 'activation_reserve_gb': 2.0}, 0, y, 'H3 Ref2VA Q4 / 768x512')
    g.add(2, 'CLIPLoader', {'clip_name': CLIP, 'type': 'minimax', 'device': 'default'}, 0, y + 230)
    g.add(3, 'VAELoader', {'vae_name': VIDEO_VAE}, 0, y + 460, 'H3 Video VAE')
    g.add(4, 'VAELoader', {'vae_name': AUDIO_VAE}, 0, y + 670, 'H3 Audio VAE')
    g.add(7, 'ModelAttentionBackend', {'model': ['1', 0], 'attention': 'comfy kitchen attention'}, 380, y)
    g.add(5, 'MiniMaxLowVRAMAttention', {'model': ['7', 0], 'head_chunks': 4}, 380, y + 220)
    g.add(6, 'MiniMaxChunkFeedForward', {'model': ['5', 0], 'chunks': 4, 'seq_threshold': 2048}, 380, y + 220)


def clip(g, base, prompt, y, context=None, frames=FRAMES, steps=STEPS):
    g.add(base, 'MiniMaxH3ReferenceToVideo', {'clip': ['2', 0], 'vae': ['3', 0], 'audio_vae': ['4', 0],
          'prompt': prompt, 'width': WIDTH, 'height': HEIGHT, 'length': frames, 'ref_image_size': 'match'}, 770, y)
    g.add(base + 1, 'H3FreeTextEncoder', {'conditioning': [str(base), 0], 'clip': ['2', 0]}, 1240, y)
    conditioning = [str(base + 1), 0]
    latent = [str(base), 1]
    if context:
        g.add(base + 2, 'MiniMaxH3MotionContext', {'conditioning': conditioning, 'vae': ['3', 0], 'latent': latent,
              'context_length': '22', 'audio_context_length': 24, 'context_latent': context}, 1240, y + 230)
        conditioning = [str(base + 2), 0]
    g.add(base + 3, 'BasicGuider', {'model': ['6', 0], 'conditioning': conditioning}, 1610, y)
    g.add(base + 4, 'RandomNoise', {'noise_seed': 20260906 + base}, 1610, y + 200)
    g.add(base + 5, 'KSamplerSelect', {'sampler_name': 'euler'}, 1610, y + 400)
    g.add(base + 6, 'BasicScheduler', {'model': ['6', 0], 'scheduler': 'simple', 'steps': steps, 'denoise': 1.0}, 1610, y + 600)
    g.add(base + 7, 'SamplerCustomAdvanced', {'noise': [str(base + 4), 0], 'guider': [str(base + 3), 0],
          'sampler': [str(base + 5), 0], 'sigmas': [str(base + 6), 0], 'latent_image': latent}, 1990, y)
    g.add(base + 8, 'VAEDecodeTiled', {'samples': [str(base + 7), 0], 'vae': ['3', 0],
          'tile_size': 256, 'overlap': 64, 'temporal_size': 32, 'temporal_overlap': 8}, 2370, y)
    g.add(base + 9, 'VAEDecodeAudio', {'samples': [str(base + 7), 0], 'vae': ['4', 0]}, 2370, y + 330)
    images, audio = [str(base + 8), 0], [str(base + 9), 0]
    if context:
        g.add(base + 10, 'MiniMaxH3MotionContextTrim', {'images': images, 'audio': audio,
              'trim_frames': [str(base + 2), 1], 'fps': 24.0, 'match_tail': True}, 2770, y)
        images, audio = [str(base + 10), 0], [str(base + 10), 1]
    return images, audio, [str(base + 7), 0]


def save_video(g, images, audio, prefix, y=0):
    g.add(201, 'CreateVideo', {'images': images, 'audio': audio, 'fps': 24.0}, 3170, y)
    g.add(202, 'SaveVideo', {'video': ['201', 0], 'filename_prefix': prefix.replace('H3-12GB/', 'H3-768x512/'), 'format': 'mp4', 'codec': 'auto'}, 3550, y)


g = Graph()
text = qwen_nodes(g, 'Please answer briefly in Chinese: 17 + 25 = ?', 'You are a helpful assistant running locally on this computer.')
g.add(103, 'ShowText|pysssss', {'text': text}, 950, 0, 'Qwen Response')
g.save('01_Qwen_Text_12GB')

g = Graph()
h3_base(g)
images, audio, latent = clip(g, 10, PROMPT, 0)
save_video(g, images, audio, 'video/H3-12GB/text')
g.save('02_H3_TextToVideo_12GB')

g = Graph()
h3_base(g)
images_a, audio_a, latent_a = clip(g, 10, PROMPT, 0)
images_b, audio_b, latent_b = clip(g, 30, PROMPT.replace('[Shot 1]', '[Shot 2]').replace('rolls slowly from left to right', 'rolls slowly forward and comes to a gentle stop'), 1000, context=latent_a)
g.add(203, 'H3ConcatAV', {'images_a': images_a, 'audio_a': audio_a, 'images_b': images_b, 'audio_b': audio_b, 'match_b': 'off'}, 3170, 850)
save_video(g, ['203', 0], ['203', 1], 'video/H3-12GB/motion-context', 400)
g.save('03_H3_MotionContext_12GB')

g = Graph()
text = qwen_nodes(g, 'Write one 8-second shot: a red toy car rolls across a wooden table in a bright room.',
                  'Write one concise English MiniMax H3 video prompt, under 140 words. Output only these sections: integrated_multimodal_description: [Shot 1], overall_soundscape:, non_diegetic_music:. Describe a visible subject, one simple action, camera, lighting, and sound. No commentary or code fences.', 0, -950)
h3_base(g)
images, audio, latent = clip(g, 10, text, 0)
save_video(g, images, audio, 'video/H3-12GB/qwen')
g.save('04_Qwen_H3_12GB')

g = Graph()
h3_base(g)
g.add(50, 'H3LongVideoDirector', {'model': ['6', 0], 'clip': ['2', 0], 'video_vae': ['3', 0], 'audio_vae': ['4', 0],
      'project_name': 'long-demo', 'storyboard': STORYBOARD, 'global_prompt': '',
      'width': WIDTH, 'height': HEIGHT, 'fps': 24, 'seconds_per_shot': 8.0,
      'seed': 20260906, 'steps': STEPS, 'resume': True}, 800, 0, 'H3 Long Video / Save and Resume')
g.save('05_H3_LongVideo_12GB')

g = Graph()
h3_base(g)
images, audio, latent = clip(g, 10, PROMPT, 0)
g.add(300, 'MiniMaxH3MotionContextSaveLatent', {'latent': latent, 'filename_prefix': 'h3_context/768x512/clip', 'clip_index': 1}, 2760, 650)
save_video(g, images, audio, 'video/H3-12GB/clip1')
g.save('06_H3_FirstClip_SaveLatent_12GB')

g = Graph()
h3_base(g)
g.add(301, 'MiniMaxH3MotionContextLoadLatent', {'latent_path': 'h3_context/768x512', 'clip_index': 1}, 770, 800)
images, audio, latent = clip(g, 10, PROMPT.replace('[Shot 1]', '[Shot 2]').replace('rolls slowly from left to right', 'rolls slowly forward and comes to a gentle stop'), 0, context=['301', 0])
g.add(300, 'MiniMaxH3MotionContextSaveLatent', {'latent': latent, 'filename_prefix': 'h3_context/768x512/clip', 'clip_index': 2}, 2760, 650)
save_video(g, images, audio, 'video/H3-12GB/continued')
g.save('07_H3_Continue_LoadLatent_12GB')

prompt_test = json.loads((API / '04_Qwen_H3_12GB.json').read_text(encoding='utf-8'))
prompt_test = {'prompt': {key: prompt_test['prompt'][key] for key in ('101', '102')}}
prompt_test['prompt']['103'] = {'class_type': 'ShowText|pysssss', 'inputs': {'text': ['102', 0]}}
(API / 'Qwen_H3_prompt_test.json').write_text(json.dumps(prompt_test, ensure_ascii=False, indent=2), encoding='utf-8')

test = json.loads((API / '02_H3_TextToVideo_12GB.json').read_text(encoding='utf-8'))
test['prompt']['16']['inputs']['steps'] = 2
test['prompt']['202']['inputs']['filename_prefix'] = 'video/H3-768x512/diagnostics/memory-probe'
test.pop('extra_data', None)
(API / 'H3_smoke_test.json').write_text(json.dumps(test, ensure_ascii=False, indent=2), encoding='utf-8')

encoder_test = {'prompt': {key: copy.deepcopy(test['prompt'][key]) for key in ('2', '3', '4', '10')}}
encoder_test['prompt']['300'] = {'class_type': 'MiniMaxH3MotionContextSaveLatent', 'inputs': {
    'latent': ['10', 1], 'filename_prefix': 'verification/encoder-empty', 'clip_index': 1}}
(API / 'H3_encoder_test.json').write_text(json.dumps(encoder_test, ensure_ascii=False, indent=2), encoding='utf-8')

combined_test = json.loads((API / '04_Qwen_H3_12GB.json').read_text(encoding='utf-8'))
combined_test['prompt']['202']['inputs']['filename_prefix'] = 'video/H3-768x512/qwen'
(API / 'Qwen_H3_smoke_test.json').write_text(json.dumps(combined_test, ensure_ascii=False, indent=2), encoding='utf-8')

multishot_test = json.loads((API / '05_H3_LongVideo_12GB.json').read_text(encoding='utf-8'))
multishot_test['prompt']['50']['inputs'].update(project_name='diagnostic-chain', steps=2,
    storyboard=json.dumps({'shots': json.loads(STORYBOARD)['shots'][:2]}))
multishot_test.pop('extra_data', None)
(API / 'H3_multishot_smoke_test.json').write_text(json.dumps(multishot_test, ensure_ascii=False, indent=2), encoding='utf-8')
