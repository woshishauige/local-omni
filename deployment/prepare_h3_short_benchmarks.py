import copy
import json
from pathlib import Path


ROOT = Path('D:/AI/deployment/logs')
SOURCE = ROOT / 'h3-current-ui-prompt.json'
OUT = ROOT / 'h3-short-bench-20260909'


def make_payload(label: str, steps: int, cache: bool) -> dict:
    prompt = json.loads(SOURCE.read_text(encoding='utf-8'))
    prompt['50']['inputs'].update(
        project_name=f'h3-short-{label}-20260909',
        storyboard=['40', 0],
        action='next_shot',
        shot_index=1,
        seconds_per_shot=5.0,
        steps=steps,
        seed=2026090706,
    )
    if cache:
        prompt['7']['inputs']['model'] = ['80', 0]
    else:
        prompt['7']['inputs']['model'] = ['1', 0]
        prompt.pop('80', None)
    return {'prompt': prompt, 'client_id': f'h3-short-{label}-20260909'}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    cases = [('native20', 20, False), ('cache20', 20, True), ('cache16', 16, True),
             ('native20-warm', 20, False)]
    for label, steps, cache in cases:
        path = OUT / f'{label}.json'
        path.write_text(json.dumps(make_payload(label, steps, cache), ensure_ascii=False, indent=2), encoding='utf-8')
        print(path)


if __name__ == '__main__':
    main()
