"""Long-lived TTS worker for the gateway: one JSON request per line, one WAV per reply.

Started once by mode_server and driven over a pipe. Keeping the engine in its own
process matters for two reasons: it loads llama.cpp and ONNX Runtime through ctypes
while the gateway already has those DLLs mapped for chat, and a synthesis crash
should not take the gateway with it.

Which device to use is decided by the caller, not here: the gateway measures free
VRAM and sets QWEN_TTS_CPU when the pair will not fit. On the card an end-to-end run
costs about 2.0 GB and takes a 9 s utterance from RTF 1.47 to 0.58; on the processor
nothing competes for VRAM at roughly double the time.

Protocol
    in : {"text": "...", "speaker": "uncle_fu", "language": "chinese",
          "instruct": "用温柔的语气说"}          # instruct optional
         {"text": "...", "voice": {"audio": "C:/.../ref.wav", "text": "参考音频说的话"}}
         {"op": "voice", "audio": "...", "text": "..."}   # load a cloned anchor, no synth
         {"op": "ping"}
    out: {"path": "C:/.../out.wav"}  or  {"error": "..."}  or  {"pong": true, "ready": bool}
         {"ok": true} for the voice op
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback

# The engine itself lives in the third-party Qwen3-TTS-GGUF checkout; point
# QWEN_TTS_PROJECT at wherever that was cloned.
PROJECT = Path(os.environ.get("QWEN_TTS_PROJECT") or
               Path(__file__).resolve().parent.parent / "tools" / "Qwen3-TTS-GGUF")
sys.path.insert(0, str(PROJECT))
os.chdir(PROJECT)

OUT_DIR = Path(tempfile.gettempdir()) / "qwen-voice-out"
OUT_DIR.mkdir(parents=True, exist_ok=True)

_engine = None
_stream = None
_PROTOCOL = None
# The anchor currently loaded, as the exact pair it was built from. Preparing a voice anchor
# encodes the reference through the codec, so redoing it per sentence would cost more than the
# sentence itself; the key is compared instead, and a changed reference reloads it.
_voice_key = None


def log(message):
    print(message, file=sys.stderr, flush=True)


def open_protocol_channel():
    """Reserve stdout for replies and send everything else to the log.

    The decoder and the speaker run as child processes and inherit this process's
    stdout, so their banner lines arrive interleaved with replies. The caller parses
    every line it is handed, so one banner was enough to make it read a perfectly good
    reply as bad JSON - and the caller's retry then asked for the same sentence twice.

    Replies therefore go to a private duplicate of the original stdout, while fd 1 is
    pointed at stderr. This worker's own logging already goes to stderr, and the child
    processes do not use stdout for anything, so nothing is lost by redirecting them.
    """
    global _PROTOCOL
    _PROTOCOL = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)


def ensure_engine():
    """Build the engine once. Returns None when it cannot be built."""
    global _engine, _stream
    if _engine is not None:
        return _engine
    from qwen3_tts_gguf.inference import TTSEngine, TTSConfig
    on_cpu = os.environ.get("QWEN_TTS_CPU", "").strip() not in ("", "0", "false")
    provider = "CPU" if on_cpu else "GPU"
    log(f"loading TTS engine (provider={provider})...")
    started = time.time()
    # These arguments are the ones this worker has always used. They were recovered from the
    # bytecode an earlier edit of this file left behind, after a rewrite made here had guessed at
    # them and got three things wrong: the stream comes from the engine's own factory rather than
    # from TTSStream(engine), `model-custom` is where the exports live (the default is "model"),
    # and local_playback must stay off or the decoder plays every sentence itself as well as
    # handing it to the gateway - which is heard as the same reply spoken twice.
    engine = TTSEngine(model_dir="model-custom", onnx_provider=provider,
                       llm_use_gpu=not on_cpu, verbose=False, local_playback=False)
    if not engine:
        log("engine reported not ready")
        return None
    _engine = engine
    _stream = engine.create_stream()
    if _stream is None:
        log("stream creation failed")
        return None
    # Warm-up, not politeness: the first synthesis of a session carries the lazy graph
    # builds and the first CUDA/DirectML uploads. Synthesising "你好" here drops the
    # first real sentence to RTF 0.78 and the rest to 0.63-0.74.
    try:
        _stream.custom(text="你好", speaker="uncle_fu", instruct=None,
                       language="chinese", config=_config())
    except Exception:  # noqa: BLE001 - a failed warm-up must not keep the worker down
        log("warm-up synthesis failed:\n" + traceback.format_exc())
    log(f"engine ready in {time.time() - started:.1f}s")
    return _engine


def _env_float(name, fallback):
    try:
        return float(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return fallback


def _env_int(name):
    """An explicit int, or None - None is the meaningful answer, not zero."""
    try:
        return int(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return None


def load_voice_anchor(audio, text):
    """Prepare a cloned voice anchor from a reference recording.

    Cloning needs the reference's own words as well as its sound: the anchor carries both the
    speaker embedding and the reference codes, and a synthesiser with only the embedding loses
    the delivery. Loading it here - before the first sentence - means a bad reference is
    reported once instead of failing every sentence of a reply.
    """
    global _voice_key
    if ensure_engine() is None:
        raise RuntimeError("TTS engine unavailable")
    key = (str(audio or ""), str(text or ""))
    if _voice_key == key:
        return True
    if not key[0] or not Path(key[0]).is_file():
        _voice_key = None
        raise ValueError(f"reference audio not found: {key[0]}")
    if not key[1].strip():
        _voice_key = None
        raise ValueError("the reference recording's own words are required to clone it")
    prepared = _stream.set_voice(key[0], text=key[1])
    if not prepared:
        _voice_key = None
        raise RuntimeError("the engine refused this reference recording")
    _voice_key = key
    log(f"cloned voice anchored from {Path(key[0]).name} ({len(key[1])} chars of reference text)")
    return True


def synthesize(text, speaker, language, instruct="", voice=None):
    global _stream
    if ensure_engine() is None:
        raise RuntimeError("TTS engine unavailable")
    config = _config()
    if voice and voice.get("audio"):
        # Cloned voice: the anchor holds the timbre, so the named speaker is not used at all.
        # `instruct` is not applied here - the vendored clone path takes no render instruction.
        load_voice_anchor(voice.get("audio"), voice.get("text"))
        result = _stream.clone(text, language=language or "chinese", config=config)
    else:
        result = _stream.custom(text=text, speaker=speaker, instruct=(instruct or None),
                                language=language or "chinese", config=config)
    if result is None:
        raise RuntimeError("the engine returned no audio")
    # A result can carry codec codes without decoded audio - the engine's own `play()` says so:
    # "此结果当前无音频数据，但检测到 Codec 特征。请先调用 .decode(engine.decoder) 进行解码渲染".
    # The vendored `save_wav` then logs an error and returns *without creating the file*, while a
    # caller that trusts the path it asked for finds nothing: that is what made every sentence cost
    # ~30 s and end in "本地合成失败" instead of audio. Decode first, exactly as the engine's GUI does.
    audio = getattr(result, "audio", None)
    if audio is None or len(audio) == 0:
        codes = getattr(result, "codes", None)
        if codes is not None and len(codes):
            ensure_engine().decode(result)          # writes the waveform back into result.audio
            audio = getattr(result, "audio", None)
    if audio is None or len(audio) == 0:
        raise RuntimeError("the engine produced codes but no audio to save")
    target = OUT_DIR / f"say-{int(time.time() * 1000)}.wav"
    result.save(str(target))
    # Never report a path that is not there. The gateway reads this file immediately, and a missing
    # one used to surface 30 s later as an unexplained failure with no trace of its cause.
    if not target.is_file():
        raise RuntimeError(f"the engine wrote no file at {target.name}")
    return target


_config_obj = None


def _config():
    """Sampling for speech that sounds like a person rather than a device.

    Every value here was previously either pinned or left at a default that works against
    that goal, and the engine's own documentation says so:

      * `seed` / `sub_seed` were fixed at 42/45. The dataclass documents a fixed seed as
        "使生成结果（语气/停顿）完全一致" - identical tone and pauses - which, applied to every
        sentence of every reply, is the mechanical sameness a listener hears immediately.
        Left None, the engine seeds from the clock per call (`llama.py`: `seed = int(time.time())`),
        so sentences vary the way speech does. Pin them again with QWEN_TTS_SEED /
        QWEN_TTS_SUB_SEED when reproducibility matters more than naturalness.
      * `min_p` was 0.0 while the dataclass recommends 0.05 to "过滤低概率噪声，减少电音".
      * `temperature` was 0.6, below the 0.9 default, and higher is documented as "情感起伏大".
        0.8 keeps most of that range while staying clear of the instability at the top.

    All of it is overridable from the environment, so a value that sounds wrong can be dialled
    back without editing code.
    """
    global _config_obj
    if _config_obj is None:
        from qwen3_tts_gguf.inference import TTSConfig
        _config_obj = TTSConfig(
            max_steps=400,
            temperature=_env_float("QWEN_TTS_TEMPERATURE", 0.8),
            top_p=_env_float("QWEN_TTS_TOP_P", 0.95),
            top_k=int(_env_float("QWEN_TTS_TOP_K", 50)),
            min_p=_env_float("QWEN_TTS_MIN_P", 0.05),
            repeat_penalty=_env_float("QWEN_TTS_REPEAT_PENALTY", 1.05),
            seed=_env_int("QWEN_TTS_SEED"),
            sub_temperature=_env_float("QWEN_TTS_SUB_TEMPERATURE", 0.6),
            sub_seed=_env_int("QWEN_TTS_SUB_SEED"),
            streaming=True,
        )
    return _config_obj


def main():
    open_protocol_channel()
    log("worker started, waiting for requests")
    while True:
        request = _read_request()
        if request is None:
            break
        op = request.get("op")
        if op == "ping":
            # Building the engine here is what makes this a readiness probe rather
            # than a liveness check. The gateway uses the answer to decide whether
            # synthesis is available at all, and a reply sent before the models exist
            # - or before a GPU load has had the chance to fail - answers a different
            # question than the one being asked.
            ready = ensure_engine() is not None
            _reply({"pong": True, "ready": ready})
            continue
        if op == "voice":
            # Registering a reference without speaking: the gateway uses this to answer
            # "is this recording usable as a voice" before it saves it.
            try:
                load_voice_anchor(request.get("audio"), request.get("text"))
                _reply({"ok": True})
            except Exception as exc:  # noqa: BLE001 - the pipe must survive any failure
                log("voice anchor error:\n" + traceback.format_exc())
                _reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            continue
        try:
            path = synthesize(request.get("text", ""), request.get("speaker", "uncle_fu"),
                              request.get("language", "chinese"),
                              request.get("instruct", ""), request.get("voice"))
            _reply({"path": str(path)})
        except Exception as exc:  # noqa: BLE001 - the pipe must survive any failure
            log("synthesis error:\n" + traceback.format_exc())
            _reply({"error": f"{type(exc).__name__}: {exc}"})
    log("stdin closed, exiting")
    return 0


def _reply(payload):
    _PROTOCOL.write(json.dumps(payload, ensure_ascii=False) + "\n")
    _PROTOCOL.flush()


def _read_request():
    """Next request object from stdin, skipping anything that is not one.

    Stdin is a pipe this process owns, but a request line can still be preceded by
    stray output, so only lines that parse as a JSON object are accepted. The reply
    direction is kept clean at the source instead: see open_protocol_channel().
    """
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            log(f"ignoring non-JSON line: {line[:120]}")
            continue
        if isinstance(payload, dict):
            return payload
        log(f"ignoring non-object JSON: {line[:120]}")
    return None


if __name__ == "__main__":
    raise SystemExit(main())
