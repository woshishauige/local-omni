"""Export the Base model's speaker encoder to ONNX, so a recording can name a voice.

Why this was missing: `export_config.py` pins `model = Models.custom`, and the CustomVoice
checkpoint has no speaker encoder (`tts.model.speaker_encoder is None`), so script 12 printed
"该模型不包含 Speaker Encoder" and stopped. Meanwhile voice.py:48 refuses to read a voice out
of a recording unless *both* encoders exist:

    if engine.codec_encoder is None or engine.speaker_encoder is None:
        return None

so `set_voice("any.wav")` was silently a no-op. The weights here come from the Base
checkpoint - 24 MB of its 3.86 GB file, fetched by range request (tools/fetch_speaker_encoder.py).

What the network actually is, because it decides what cloning can carry: ECAPA-TDNN, a
*speaker verification* model. It maps a mel spectrogram to a single 2048-d identity vector,
and it is trained to be invariant to what was said - same person, different words, same
vector. So it transfers timbre: vocal-tract character, recording closeness, breathiness. It
has no representation for an event like a lick, which is a moment, not an identity.

Usage:
    python tools/export_speaker_encoder.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import torch

PROJECT = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF")
WEIGHTS = Path(r"D:\AI\Qwen-Local\models\tts\Qwen3-TTS-12Hz-1.7B-Base-partial\speaker_encoder.safetensors")
OUT_DIR = PROJECT / "model-custom"
PREFIX = "speaker_encoder."

sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "Qwen3-TTS-main"))


def build():
    from qwen_tts.core.models.configuration_qwen3_tts import Qwen3TTSSpeakerEncoderConfig
    from qwen_tts.core.models.modeling_qwen3_tts import Qwen3TTSSpeakerEncoder

    # Only enc_dim differs from the class defaults (the Base config states 2048, the default
    # is 1024); the tensor shapes confirm it - fc.weight is [2048, 3072, 1].
    config = Qwen3TTSSpeakerEncoderConfig(mel_dim=128, enc_dim=2048)
    model = Qwen3TTSSpeakerEncoder(config).eval()

    from safetensors.torch import load_file
    raw = load_file(str(WEIGHTS))
    state = {key[len(PREFIX):]: value.float() for key, value in raw.items()
             if key.startswith(PREFIX)}
    missing, unexpected = model.load_state_dict(state, strict=False)
    print("loaded %d tensor(s); missing %s; unexpected %s"
          % (len(state), list(missing), list(unexpected)))
    if missing:
        raise SystemExit("the module expects tensors the checkpoint does not have")
    return model


def export(model, dtype, target: Path):
    from qwen3_tts_gguf.export.codec_export import SpeakerEncoderExportWrapper

    wrapper = SpeakerEncoderExportWrapper(model).eval()
    if dtype == torch.float16:
        wrapper = wrapper.half()
    dummy = torch.randn(1, 100, 128, dtype=dtype)
    torch.onnx.export(
        wrapper, (dummy,), str(target),
        input_names=["mels"], output_names=["spk_emb"],
        dynamic_axes={"mels": {0: "batch_size", 1: "sequence_length"},
                      "spk_emb": {0: "batch_size"}},
        opset_version=18, do_constant_folding=True,
    )
    print("wrote %s (%.2f MB)" % (target, target.stat().st_size / 1e6))


def validate(model, target: Path, dtype) -> bool:
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.log_severity_level = 3
    session = ort.InferenceSession(str(target), sess_options=options,
                                   providers=["CPUExecutionProvider"])
    print("   %s provider=%s inputs=%s" % (target.name, session.get_providers()[0],
                                           [(i.name, i.type) for i in session.get_inputs()]))
    rng = np.random.default_rng(3)
    for length in (100, 260):
        mels = rng.standard_normal((1, length, 128)).astype(np.float32)
        feed = mels.astype(np.float16) if dtype == torch.float16 else mels
        got = session.run(["spk_emb"], {"mels": feed})[0]
        with torch.no_grad():
            want = model(torch.from_numpy(feed).to(dtype)).float().numpy()
        # Cosine similarity is the meaningful comparison: it is how speaker embeddings are
        # ever compared, and the absolute scale of an embedding carries no information.
        a, b = got.reshape(-1).astype(np.float64), want.reshape(-1).astype(np.float64)
        cosine = float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))
        print("   T=%-4d shape %-10s cosine %.6f  max|diff| %.3e  norm %.3f"
              % (length, got.shape, cosine, float(np.max(np.abs(a - b))), float(np.linalg.norm(a))))
        if cosine < 0.999:
            return False
    return True


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("building ECAPA-TDNN from the partial checkpoint ...")
    model = build()
    ok = True
    for dtype, name in ((torch.float32, "qwen3_tts_speaker_encoder.fp32.onnx"),
                        (torch.float16, "qwen3_tts_speaker_encoder.fp16.onnx")):
        target = OUT_DIR / name
        export(model, dtype, target)
        ok = validate(model, target, dtype) and ok
    print("\nall exports agree with torch:", ok)
    print("engine.py looks for qwen3_tts_speaker_encoder.fp16.onnx in model-custom/ - it is there now.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
