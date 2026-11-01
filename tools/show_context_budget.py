"""Print the per-mode context budget, so the numbers in the docs and the code agree."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import qwen_config as config  # noqa: E402

for name, profile in config.PROFILES.items():
    voice = profile.get("voice_context_size")
    print(f"{name}: 文字/识图={profile['context_size']}  语音={voice}  层={profile.get('gpu_layers')}")
settings = config.read_settings()
for name in config.PROFILES:
    chosen = {**settings, "model_profile": name}
    print(f"  {name}: expected_context={config.expected_context(chosen)}")
