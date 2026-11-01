import base64
import io
import json
from pathlib import Path

from PIL import Image, ImageDraw
import requests

picture = Image.new('RGB', (512, 320), 'white')
draw = ImageDraw.Draw(picture)
draw.rectangle((55, 85, 205, 235), fill=(220, 25, 40))
draw.ellipse((305, 85, 455, 235), fill=(25, 70, 220))
buffer = io.BytesIO()
picture.save(buffer, format='PNG')
picture.save('D:/AI/deployment/qwen-vision-test.png')
payload = {
    'model': 'qwen3.8-27b-local',
    'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'Describe the two colored shapes and their positions. Answer briefly in Chinese.'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()}},
    ]}],
    'max_tokens': 96, 'temperature': 0.1,
}
response = requests.post('http://127.0.0.1:8081/v1/chat/completions', json=payload, timeout=300)
response.raise_for_status()
result = response.json()
Path('D:/AI/deployment/logs/qwen-vision-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=False, indent=2))
