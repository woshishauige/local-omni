"""Does set_voice(recording) work now - and what does it actually carry?

Two questions, and the second one matters more than the first.

  1. Capability. Before this, voice.py:48 returned None for any audio file because the
     CustomVoice export had no speaker encoder, so set_voice("x.wav") was a silent no-op.
     With the Base model's ECAPA-TDNN exported into model-custom/, does a recording now
     name a voice?

  2. What transfers. The encoder is a *speaker verification* network: one 2048-d vector per
     utterance, trained to be invariant to content. So the honest expectation is that it
     carries timbre - vocal tract, closeness, breathiness - and not events. That is testable
     rather than arguable: clone from a plain reference and from a deliberately "wet" one
     (same speech with mouth sounds spliced in and the 2-8 kHz band lifted) and compare what
     comes out.

If the wet reference really does produce a measurably wetter voice, then the mouth-sound
character can be baked into a cloned voice, which is the closest thing to "learning" the
sound that this pipeline can offer. If the two outputs come out the same, that is the answer,
and it is better to have measured it than to have promised it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

PROJECT = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF")
SFX = Path(r"D:\AI\Qwen-Local\models\sfx\mouth")
LAB = Path(r"D:\AI\Qwen-Local\tools\bench-out\voice-lab")
RATE = 24000
sys.path.insert(0, str(PROJECT))

REFERENCE_LINE = "今天的天气很好，我们一起出去走走吧。"
TARGET_LINE = "明天早上八点叫我起床，谢谢。"


def cosine(a, b) -> float:
    a = np.asarray(a, dtype=np.float64).reshape(-1)
    b = np.asarray(b, dtype=np.float64).reshape(-1)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


def timbre(audio: np.ndarray) -> dict:
    import librosa
    centroid = float(np.mean(librosa.feature.spectral_centroid(y=audio, sr=RATE)))
    spectrum = np.abs(librosa.stft(audio.astype(np.float32), n_fft=1024))
    freqs = librosa.fft_frequencies(sr=RATE, n_fft=1024)
    band = (freqs >= 2000) & (freqs <= 8000)
    total = float(np.sum(spectrum)) + 1e-12
    return {"centroid_hz": round(centroid, 1),
            "high_band_ratio": round(float(np.sum(spectrum[band]) / total), 5)}


def main() -> int:
    import os
    os.chdir(PROJECT)
    import soundfile as sf
    from qwen3_tts_gguf.inference import TTSEngine, TTSConfig

    engine = TTSEngine(model_dir="model-custom", onnx_provider="GPU", llm_use_gpu=True,
                       verbose=False, local_playback=False)
    if not engine:
        print("engine not ready")
        return 1
    print("codec encoder  :", type(engine.codec_encoder).__name__ if engine.codec_encoder else None)
    print("speaker encoder:", type(engine.speaker_encoder).__name__ if engine.speaker_encoder else None)
    if engine.speaker_encoder is None:
        print("still missing - nothing below can work")
        return 1

    config = TTSConfig(max_steps=600, temperature=0.6, seed=42, sub_seed=45, streaming=True)

    # ---- 1. a reference recording, and a deliberately wetter one ----------------------
    stream = engine.create_stream()
    stream.custom(text="你好", speaker="uncle_fu", instruct=None, language="chinese", config=config)
    plain = stream.custom(text=REFERENCE_LINE, speaker="uncle_fu", instruct=None,
                          language="chinese", config=config)
    if plain is None or plain.audio is None:
        print("could not synthesise the reference")
        return 1
    ref_plain = np.asarray(plain.audio, dtype=np.float32)
    sf.write(str(LAB / "12-reference-plain.wav"), ref_plain, RATE)

    import librosa
    from scipy.signal import butter, sosfilt
    lick, _ = librosa.load(str(SFX / "licking.24k.wav"), sr=RATE, mono=True)
    wet_lick = lick + 1.6 * sosfilt(
        butter(2, [1800 / (RATE / 2), 8000 / (RATE / 2)], btype="band", output="sos"), lick)
    ref_wet = ref_plain.copy()
    for when in (0.6, 1.7, len(ref_wet) / RATE - 0.6):
        start = int(when * RATE)
        end = min(len(ref_wet), start + len(wet_lick))
        if end > start:
            ref_wet[start:end] += 0.55 * wet_lick[:end - start].astype(np.float32)
    peak = float(np.max(np.abs(ref_wet)))
    ref_wet = (ref_wet / peak * 0.9).astype(np.float32) if peak > 0.95 else ref_wet.astype(np.float32)
    sf.write(str(LAB / "13-reference-wet.wav"), ref_wet, RATE)

    emb_plain = engine.speaker_encoder.encode_audio(ref_plain)
    emb_wet = engine.speaker_encoder.encode_audio(ref_wet)
    print("\nreference embeddings: cosine(plain, wet) = %.4f  <- how much the mouth sounds "
          "moved the identity vector" % cosine(emb_plain, emb_wet))
    print("reference timbre    : plain %s   wet %s" % (timbre(ref_plain), timbre(ref_wet)))

    # ---- 2. clone from each, and compare ------------------------------------------------
    print("\n%-10s %-12s %-14s %-14s %s" % ("clone of", "embed cos", "centroid", "high-band", "vs plain"))
    print("-" * 78)
    outputs = {}
    for label, reference in (("plain", ref_plain), ("wet", ref_wet)):
        path = LAB / ("%s-reference.wav" % label)
        sf.write(str(path), reference, RATE)
        voice_stream = engine.create_stream()
        prepared = voice_stream.set_voice(path, text=REFERENCE_LINE)
        if not prepared:
            print("%-10s set_voice returned False - cloning refused" % label)
            continue
        result = voice_stream.clone(text=TARGET_LINE, language="chinese", config=config)
        if result is None or result.audio is None:
            print("%-10s clone synthesis failed" % label)
            continue
        audio = np.asarray(result.audio, dtype=np.float32)
        out = LAB / ("14-clone-from-%s.wav" % label)
        sf.write(str(out), audio, RATE)
        outputs[label] = audio
        facts = timbre(audio)
        out_embedding = engine.speaker_encoder.encode_audio(audio)
        ref_embedding = engine.speaker_encoder.encode_audio(reference)
        print("%-10s %-12.4f %-14.0f %-14.5f %s"
              % (label, cosine(ref_embedding, out_embedding),
                 facts["centroid_hz"], facts["high_band_ratio"], out.name))

    if len(outputs) == 2:
        a, b = outputs["plain"], outputs["wet"]
        n = min(len(a), len(b))
        delta = timbre(b[:n])["high_band_ratio"] - timbre(a[:n])["high_band_ratio"]
        print("\nhigh-band energy of the wet clone minus the plain clone: %+.5f" % delta)
        print("centroid: %.0f Hz vs %.0f Hz" % (timbre(a[:n])["centroid_hz"], timbre(b[:n])["centroid_hz"]))
        if abs(delta) < 1e-4:
            print("-> the mouth character did NOT transfer: the encoder is content-invariant,")
            print("   so a wet reference names the same voice as a dry one.")
        else:
            print("-> the wet reference produced a measurably different voice.")
    engine.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
