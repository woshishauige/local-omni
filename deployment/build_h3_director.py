import json

from build_workflows import API, OUTPUT, Graph, h3_base, qwen_nodes


def save_director(graph, name):
    graph.save(name)
    path = API / (name + '.json')
    payload = json.loads(path.read_text(encoding='utf-8'))
    workflow = payload['extra_data']['extra_pnginfo']['workflow']
    by_id = {node['id']: node for node in workflow['nodes']}
    by_id[50].update(pos=[820, 0], size=[560, 1080])
    by_id[51].update(pos=[1440, 0], size=[570, 650])
    if 101 in by_id:
        by_id[101]['pos'] = [2080, 0]
        by_id[102].update(pos=[2520, 0], size=[600, 1100])
    for shot in range(1, 5):
        y = 1280 + (shot - 1) * 560
        prompt_id = 39 + shot
        if prompt_id in by_id:
            by_id[prompt_id].update(pos=[0, y], size=[520, 470])
        for slot in range(5):
            by_id[60 + (shot - 1) * 5 + slot].update(pos=[580 + slot * 360, y], size=[330, 450])
        workflow['groups'].append({'id': shot, 'title': f'Shot {shot} / {(shot - 1) * 7.5:g}-{shot * 7.5:g}s / 5 Optional References',
            'bounding': [-25, y - 65, 2430, 560], 'color': ['#3f789e', '#467d62', '#8a6e3f', '#745d82'][shot - 1],
            'font_size': 24, 'flags': {}})
    for index, key in enumerate((44, 45, 46)):
        if key in by_id:
            by_id[key].update(pos=[2500, 1300 + index * 150], flags={'collapsed': True})
    workflow['extra']['ds'] = {'scale': 0.65, 'offset': [45, 70]}
    (OUTPUT / (name + '.json')).write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding='utf-8')
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

GLOBAL = (
    'A cinematic live-action Chinese xianxia martial arts film, photorealistic faces and practical flowing costumes, '
    'realistic movement and physically grounded sword choreography enhanced by restrained supernatural effects. '
    'Two adult East Asian opponents: a young swordswoman with long black hair tied in a high ponytail, '
    'ivory-white flowing robes, a teal silk sash and a straight silver jian sword; '
    'a tall male warrior in black and bronze lamellar armor, a dark crimson cape, carrying a long bronze spear. '
    'They fight on a broad circular weathered stone platform floating above a vast bright cloud sea, '
    'beneath a colossal broken jade temple gate, distant mountain peaks, warm sunlight and cool atmospheric shadows. '
    'Both fighters stay visible, anatomically clear and consistent. Rich jade, silver and crimson colors, '
    'natural film contrast, detailed fabric and metal, readable action, full-frame composition, no text or subtitles.'
)
SHOTS = [
    'integrated_multimodal_description: [Shot 1] A low, wide cinematic shot reveals the enormous temple gate above the clouds. '
    'The white-robed swordswoman stands on the left and the armored spear warrior on the right, facing each other across the platform. '
    'The camera slowly pushes closer as wind sweeps their robes. They dash toward each other and meet at the center; '
    'her silver sword blocks his bronze spear with a sharp burst of golden sparks. End with the two weapons pressed together, '
    'both fighters braced in a clear side-on composition. overall_soundscape: High mountain wind, rushing cloth, footsteps on stone, '
    'one resonant metallic clash. non_diegetic_music: Epic low strings, deep ceremonial drums and distant brass, no vocals.',
    'integrated_multimodal_description: [Shot 2] Sustain the same central weapon lock and camera framing for the first second. '
    'The swordswoman pivots smoothly to her left and deflects the spear; the armored warrior turns and sweeps the spear in a broad horizontal arc. '
    'She ducks under the sweep and counters with one precise rising sword slash, leaving a thin cyan energy trail. '
    'The camera makes a measured lateral arc, keeping both full bodies and the contact of the weapons readable. '
    'End with them separating by three paces, facing each other with weapons raised. overall_soundscape: Two clear metal impacts, '
    'spear whoosh, cloth and sliding boots. non_diegetic_music: Continue the same cinematic low strings and driving ceremonial drums, no vocals.',
    'integrated_multimodal_description: [Shot 3] Sustain the same crossed sword and spear for the first second: '
    'the armored warrior on the left and the white-robed swordswoman on the right, costumes and lighting unchanged. '
    'Amber energy gathers along his spear and brilliant cyan energy flows along her sword toward their point of contact. '
    'One powerful cyan-and-gold energy collision erupts between the locked weapons. A broad luminous circular shockwave races '
    'across the stone platform and into the cloud sea, lifting small stone fragments and streaming their robes backward. '
    'Both slide backward three paces while keeping their feet on the stone. The camera pulls wider to reveal the enormous jade gate '
    'above the expanding energy ring. End with both upright on opposite sides amid drifting dust and fading sparks. overall_soundscape: Rising energy hum, '
    'a deep impact and stone fragments scattering, wind. non_diegetic_music: Rising orchestral brass, strings and heavy drums, no vocals.',
    'integrated_multimodal_description: [Shot 4] Hold the previous wide composition and crossed weapons for the first second: '
    'the white-robed swordswoman on the left and the armored warrior on the right beneath the jade gate. '
    'She twists her sword in one decisive downward parry, knocking the long spear out of his hands onto the stone. '
    'The warrior sinks to one knee beside the fallen spear. She takes one calm step backward and lowers her silver sword, its cyan light fading. '
    'The camera slowly cranes back into a majestic wide shot: the two small figures beneath the immense broken jade gate, clouds moving below, '
    'sunlight breaking through. Hold this resolved final composition. overall_soundscape: One final metallic strike, cloth settling and mountain wind. '
    'non_diegetic_music: A final orchestral chord with deep drums fading into sustained strings, no vocals.',
]

g = Graph()
h3_base(g)
g.prompt['1']['inputs']['activation_reserve_gb'] = 3.0
links = []
for i, shot in enumerate(SHOTS):
    links.append(g.add(40 + i, 'PrimitiveStringMultiline', {'value': shot}, 800 + (i % 2) * 580,
                       (i // 2) * 620, f'Shot {i + 1} / {i * 7.5:g}-{(i + 1) * 7.5:g}s'))
left = g.add(44, 'StringConcatenate', {'string_a': links[0], 'string_b': links[1], 'delimiter': '\n---\n'}, 1990, 0)
right = g.add(45, 'StringConcatenate', {'string_a': links[2], 'string_b': links[3], 'delimiter': '\n---\n'}, 1990, 360)
text = g.add(46, 'StringConcatenate', {'string_a': left, 'string_b': right, 'delimiter': '\n---\n'}, 2450, 0)
reference_links = {}
for shot in range(1, 5):
    for slot in range(1, 6):
        node_id = 60 + (shot - 1) * 5 + slot - 1
        reference_links[f'shot_{shot:02d}_ref_{slot:02d}'] = g.add(
            node_id, 'H3OptionalReferenceImage', {'image': '__none__'},
            800 + (slot - 1) * 390, 1280 + (shot - 1) * 430,
            f'Shot {shot} / Optional Reference {slot}')
g.add(50, 'H3MP4ShotEditor', {
    'model': ['6', 0], 'clip': ['2', 0], 'video_vae': ['3', 0], 'audio_vae': ['4', 0],
    'project_name': 'tianmen-duel-30s', 'storyboard': text, 'global_prompt': GLOBAL,
    'action': 'next_shot', 'shot_index': 1, 'width': 768, 'height': 512, 'fps': 24,
    'seconds_per_shot': 7.5, 'steps': 24, 'seed': 2026090706, 'use_first_shot_reference': True,
    **reference_links,
}, 2930, 0, 'H3 30s / MP4 Checkpoints / One Shot Per Run')
g.add(51, 'ShowText|pysssss', {'text': ['50', 1]}, 3410, 0, 'Shot Checkpoint / Next and Review')
save_director(g, '08_H3_LongVideo_768x512')

for i in range(1, 5):
    payload = json.loads((API / '08_H3_LongVideo_768x512.json').read_text(encoding='utf-8'))
    payload['prompt']['50']['inputs'].update(action='redo_shot', shot_index=i)
    for node in payload['extra_data']['extra_pnginfo']['workflow']['nodes']:
        if node['id'] == 50:
            node['widgets_values'][3:5] = ['redo_shot', i]
    (API / f'H3_Xianxia_Shot_{i:02d}.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')

editor_inputs = g.prompt['50']['inputs'].copy()
for use_qwen in (False, True):
    compact = Graph()
    h3_base(compact)
    compact.prompt['1']['inputs']['activation_reserve_gb'] = 3.0
    text = '\n---\n'.join(SHOTS)
    if use_qwen:
        text = qwen_nodes(compact,
            'Return only valid JSON: {"shots":[{"prompt":"..."},{"prompt":"..."},{"prompt":"..."},{"prompt":"..."}]}. '
            'Write four cinematic Chinese xianxia fight shots, each 7.5 seconds long: confrontation, sword versus spear exchanges, '
            'cyan and amber energy collision, and a decisive disarm beneath a huge jade gate above the clouds. '
            'The two adults are a white-robed swordswoman and a black-armored spear warrior. '
            'Start continuations by sustaining the previous ending for one second. The answer must start with { and end with }.',
            'Output one JSON object with exactly four shots. Each prompt uses integrated_multimodal_description: [Shot N], '
            'overall_soundscape:, non_diegetic_music:. Use ASCII English and under 70 words per shot, no commentary.', 800, 1000)
        compact.prompt['102']['inputs'].update({'\u6700\u5927\u751f\u6210token': 1024, '\u6e29\u5ea6': 0.2})
    reference_links = {}
    for shot in range(1, 5):
        for slot in range(1, 6):
            node_id = 60 + (shot - 1) * 5 + slot - 1
            reference_links[f'shot_{shot:02d}_ref_{slot:02d}'] = compact.add(
                node_id, 'H3OptionalReferenceImage', {'image': '__none__'},
                800 + (slot - 1) * 390, 720 + (shot - 1) * 300,
                f'Shot {shot} / Optional Reference {slot}')
    compact.add(50, 'H3MP4ShotEditor', {**editor_inputs, 'storyboard': text,
                'project_name': 'qwen-xianxia-30s' if use_qwen else 'tianmen-duel-30s',
                **reference_links}, 1800 if use_qwen else 800, 0)
    compact.add(51, 'ShowText|pysssss', {'text': ['50', 1]}, 2300 if use_qwen else 1300, 0, 'Shot Checkpoint / Next and Review')
    save_director(compact, '09_Qwen_H3_LongVideo_768x512' if use_qwen else '05_H3_LongVideo_12GB')

qwen_test = {'prompt': {key: compact.prompt[key] for key in ('101', '102')}}
qwen_test['prompt']['103'] = {'class_type': 'ShowText|pysssss', 'inputs': {'text': ['102', 0]}}
(API / 'Qwen_H3_30s_storyboard_test.json').write_text(json.dumps(qwen_test, ensure_ascii=False, indent=2), encoding='utf-8')
