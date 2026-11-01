"""Voice input: audio in, text out, with every byte staying on this machine.

The browser records a clip, posts it here, and gets recognized text back to insert into
the field the owner was typing in.

Deliberately a **local** model rather than the browser's built-in speech recognition:
that one ships in Edge/Chrome but is only reliably local on some platforms and silently
uses a cloud service on others, which is the wrong trade for a machine whose whole point
is that the model runs here. faster-whisper runs the CTranslate2 build of Whisper on the
**CPU** on purpose — the GPU is the scarce resource this app arbitrates, and a
transcription must not be the thing that evicts a running generation.

The model is fetched from ModelScope (Hugging Face is unreachable from this machine and
hf-mirror times out) by ``fetch_asr_model.py``; nothing is downloaded implicitly at
request time, so a first click either works or says exactly what is missing.
"""

import asyncio
import logging
import os
from contextlib import suppress
from pathlib import Path
import tempfile
import time
import uuid

LOGGER = logging.getLogger("qwen.voice")

ROOT = Path(__file__).resolve().parent
MODEL_ROOT = ROOT / "models" / "asr"
# Smallest useful default for Chinese on CPU: ~460 MB, a few seconds per phrase.
DEFAULT_MODEL = "small"
# Bounds that keep one click from becoming a transcription job: a spoken prompt is
# seconds, not minutes, and 25 MB is far beyond what that produces.
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_SECONDS = 180
SAMPLE_RATE = 16000
# Weighted toward Chinese because that is what this machine's prompts are written in,
# and detection on a short clip is the least reliable part of the pipeline.
DEFAULT_LANGUAGE = "zh"
LANGUAGES = ("zh", "en", "ja", "ko", "auto")

# --- streaming ("text appears while you speak") ------------------------------
#
# The model itself is a batch recognizer: faster-whisper reads a whole clip and returns
# a transcript, so live text is built by re-reading a short tail of audio while the
# speaker continues. Two properties of this machine decide the parameters:
#
# * The model runs at ~1.25x realtime on this CPU, so only the *tail* is re-read. A
#   whole-session re-read would grow without bound and fall further behind each second.
# * One recognition already uses 12 of the CPU's threads, so runs are serialized. When a
#   run finishes, whatever arrived meanwhile is processed in one go — the queue is
#   measured in seconds and never accumulates.
STREAM_WINDOW_SECONDS = 4.0
# At most one recognition per this interval, so a fast talker cannot queue up work.
# Short on purpose: readings are dispatched in the background and the stream's own lock
# serializes them, so this only decides how soon after a reading the next one starts.
STREAM_MIN_INTERVAL = 0.35
# A gap this long ends the phrase: the tail is finalized verbatim and the buffer drops it.
STREAM_SILENCE_SECONDS = 0.45
# RMS below which the tail counts as silence (16-bit full scale is 32768).
STREAM_SILENCE_RMS = 320.0
# After this long without audio, the server finalizes the phrase by itself.
#
# Without it the last words are lost whenever the client stops sending: the browser's
# loop is throttled in a background tab, and a caller may simply stop mid-stream. Making
# the *server* responsible means the tail is read and committed on the audio's own
# schedule rather than the client's.
STREAM_IDLE_FINISH_SECONDS = 1.1
# What the rolling buffer keeps. Longer than the silence window so a phrase in progress
# is never cut in half before it is committed.
STREAM_BUFFER_SECONDS = 12.0
# Tail bounds for a single re-read. The first reading is deliberately early: the latency
# of the first text is what decides whether this feels live, and a short tail is the
# cheapest reading there is.
STREAM_MIN_TAIL = 0.5
STREAM_MAX_TAIL = 7.0
# Sessions are dropped after this long without audio, so an abandoned recording cannot
# hold buffers (or a recognition slot) forever.
STREAM_TTL_SECONDS = 30 * 60
STREAM_CHUNK_BYTES = 2  # 16-bit mono samples
# How many finalized phrases are kept so a client that polls after the idle timer fired
# still gets its transcript.
FINISHED_KEEP = 8

_MODEL = None
_MODEL_LOCK = asyncio.Lock()
_LOADING = False
_STREAMS: dict[str, "VoiceStream"] = {}
# Finalized phrases, so a poll that arrives after the idle timer fired still resolves.
_FINISHED: dict[str, "VoiceStream"] = {}
# One recognition at a time: each already saturates the CPU threads it was given.
_INFERENCE = asyncio.Semaphore(1)

# Live readings run in a **separate process**, not in the event loop's thread pool.
# faster-whisper's encoder holds the GIL inside CTranslate2 for most of a call, so a
# threaded reading starved the gateway's own async loop: measured, the first reading of a
# half-second tail did not come back until the whole 5.3 s clip had been fed in, and the
# polling between chunks stopped running on time. A dedicated worker keeps the loop free
# and makes the reading latency visible instead of hidden behind a starved loop.
_WORKER = None
_WORKER_LOCK = asyncio.Lock()


def _worker_model():
    """Load the model inside the worker process, once."""
    global _MODEL
    if _MODEL is None:
        directory = available_model()
        if directory is None:
            raise VoiceError(describe()["hint"], status=503)
        _MODEL = _load_blocking(directory)
    return _MODEL


def _worker_reading(pcm: bytes, language, prompt: str, silent: bool):
    """Runs in the worker process; must not touch the event loop."""
    return _recognize_pcm(_worker_model(), pcm, language, prompt, silent)


async def _reading_offload(pcm, language, prompt, silent):
    """Dispatch one reading to the worker process, rebuilding it if it died."""
    global _WORKER
    from concurrent.futures import ProcessPoolExecutor
    from concurrent.futures.process import BrokenProcessPool

    for attempt in (1, 2):
        if _WORKER is None:
            async with _WORKER_LOCK:
                if _WORKER is None:
                    _WORKER = ProcessPoolExecutor(max_workers=1)
                    LOGGER.info("voice: started the recognition worker process")
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(_WORKER, _worker_reading, pcm, language, prompt, silent)
        except BrokenProcessPool:
            # A worker can die (a killed process, a hard import failure inside it). Voice
            # input is a convenience, so it retries once on a fresh worker instead of
            # taking the gateway's request down with it.
            LOGGER.warning("voice: recognition worker died; starting a new one")
            with suppress(Exception):
                _WORKER.shutdown(wait=False, cancel_futures=True)
            _WORKER = None
            if attempt == 2:
                raise VoiceError("语音识别进程异常退出，请重试。", status=503)
    raise VoiceError("语音识别进程不可用。", status=503)


def shutdown_worker():
    """Stop the worker process (used on a clean gateway shutdown)."""
    global _WORKER
    worker, _WORKER = _WORKER, None
    if worker is None:
        return
    # The handle is taken first: shutdown() clears the executor's own bookkeeping, so
    # reading `_processes` afterwards found None and raised (it did, in this very code).
    processes = list(getattr(worker, "_processes", {}).values())
    with suppress(Exception):
        worker.shutdown(wait=False, cancel_futures=True)
    # ProcessPoolExecutor does not terminate its children: they survive a killed parent
    # and each one holds the model in memory. Measured, three orphans were left behind by
    # restarts during development, so termination is explicit rather than hoped for.
    for process in processes:
        with suppress(Exception):
            process.terminate()


def reap_orphan_workers():
    """Kill recognition workers left behind by a previous gateway.

    Called at startup, before this process has one of its own, so anything matching is an
    orphan by definition. Without this, every hard restart leaks a process holding a full
    copy of the speech model.

    Uses PowerShell rather than wmic: wmic is deprecated and on this machine answers every
    query with "invalid query", which silently turned this cleanup into a no-op.
    """
    if os.name != "nt":
        return 0
    import subprocess

    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*spawn_main*' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        listing = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    killed = 0
    for line in listing.split():
        if not line.strip().isdigit():
            continue
        try:
            subprocess.run(["taskkill", "/PID", line.strip(), "/F"],
                           capture_output=True, timeout=15, check=False)
            killed += 1
        except (OSError, subprocess.SubprocessError):
            continue
    if killed:
        LOGGER.info("voice: cleared %d orphaned recognition worker(s)", killed)
    return killed


class VoiceError(RuntimeError):
    """A refusal the caller should see verbatim, with an HTTP status."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def model_candidates():
    """Model directories to try, best first, so an upgrade is a drop-in."""
    directories = []
    configured = _configured_model()
    if configured:
        directories.append(MODEL_ROOT / configured)
    for size in (DEFAULT_MODEL, "base", "medium", "large-v3", "large-v3-turbo"):
        candidate = MODEL_ROOT / size
        if candidate not in directories:
            directories.append(candidate)
    return directories


def _configured_model():
    import qwen_config

    try:
        value = str(qwen_config.share_setting("asr_model") or "").strip()
    except Exception:  # noqa: BLE001 - a broken overlay must not break voice input
        return ""
    return value


def available_model():
    """The model directory to use, or None when nothing has been fetched yet."""
    for directory in model_candidates():
        if (directory / "model.bin").is_file() and (directory / "config.json").is_file():
            return directory
    return None


def describe():
    model = available_model()
    return {
        "ready": model is not None,
        "model": model.name if model else "",
        "model_path": str(model) if model else "",
        "expected": str(MODEL_ROOT / DEFAULT_MODEL),
        "languages": list(LANGUAGES),
        "default_language": DEFAULT_LANGUAGE,
        "max_seconds": MAX_SECONDS,
        "device": "cpu",
        "hint": "" if model else (
            "还没有本地语音模型。在本机运行 "
            f"python fetch_asr_model.py {DEFAULT_MODEL} 下载（约 460 MB，来自 ModelScope）。"
        ),
    }


async def load():
    """Load the model once, off the event loop."""
    global _MODEL, _LOADING
    if _MODEL is not None:
        return _MODEL
    directory = available_model()
    if directory is None:
        raise VoiceError(describe()["hint"], status=503)
    async with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        _LOADING = True
        try:
            _MODEL = await asyncio.to_thread(_load_blocking, directory)
        finally:
            _LOADING = False
    return _MODEL


def _load_blocking(directory):
    from faster_whisper import WhisperModel

    started = time.time()
    # More threads is what makes this usable: measured on this machine's 24-core CPU,
    # four threads ran the small model at 0.68x realtime and twelve at 1.25x, with no
    # further gain above that. Recognition stays on the CPU so it never competes for
    # the VRAM the arbiter is handing out.
    threads = min(12, (os.cpu_count() or 4))
    model = WhisperModel(str(directory), device="cpu", compute_type="int8", cpu_threads=threads)
    LOGGER.info("voice: loaded %s in %.1fs (%d threads)", directory.name, time.time() - started, threads)
    return model


def _ffmpeg():
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def convert_to_wav(payload: bytes, target: Path) -> None:
    """Decode whatever the browser recorded into 16 kHz mono PCM.

    Browsers hand over WebM/Opus, and Safari hands over MP4/AAC; both need decoding, and
    ffmpeg is already here for the video pipeline. ``-t`` caps a runaway recording.
    """
    import subprocess

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as handle:
        handle.write(payload)
        source = Path(handle.name)
    try:
        completed = subprocess.run(
            [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(source), "-t", str(MAX_SECONDS),
             "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "wav", str(target)],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if completed.returncode != 0 or not target.is_file() or target.stat().st_size <= 44:
            detail = (completed.stderr or "").strip().splitlines()
            raise VoiceError("音频解码失败：" + (detail[-1][:160] if detail else "无法识别的音频格式"))
    except FileNotFoundError as exc:
        raise VoiceError("找不到 ffmpeg，无法解码音频。") from exc
    except subprocess.SubprocessError as exc:
        raise VoiceError(f"音频解码超时或失败：{exc}") from exc
    finally:
        source.unlink(missing_ok=True)


def to_simplified(text):
    """Whisper writes Chinese in traditional characters; this UI is simplified.

    Left as-is when the converter is missing rather than failing the request: a
    traditional-character prompt is still usable, a 500 is not.
    """
    if not text:
        return text
    try:
        import zhconv

        return zhconv.convert(text, "zh-cn")
    except Exception:  # noqa: BLE001 - conversion is a nicety, not a requirement
        return text


def transcribe_wav(model, wav: Path, language=None):
    """Run recognition on the event loop's behalf; returns text plus what it cost."""
    wanted = (language or DEFAULT_LANGUAGE).strip().lower()
    if wanted not in LANGUAGES:
        wanted = DEFAULT_LANGUAGE
    started = time.time()
    segments, info = model.transcribe(
        str(wav),
        language=None if wanted == "auto" else wanted,
        beam_size=1,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,
    )
    text = to_simplified("".join(segment.text for segment in segments).strip())
    return {
        "text": text,
        "language": getattr(info, "language", wanted),
        "language_probability": round(float(getattr(info, "language_probability", 0.0) or 0.0), 3),
        "duration_seconds": round(float(getattr(info, "duration", 0.0) or 0.0), 2),
        "seconds": round(time.time() - started, 2),
    }


async def transcribe(payload: bytes, language=None):
    """Full path: validate, decode, recognize, clean up."""
    if not payload:
        raise VoiceError("没有收到音频数据。")
    if len(payload) > MAX_AUDIO_BYTES:
        raise VoiceError(f"录音超过 {MAX_AUDIO_BYTES // (1024 * 1024)} MB 上限，请缩短后重试。", status=413)
    model = await load()
    with tempfile.TemporaryDirectory(prefix="qwen-voice-") as folder:
        wav = Path(folder) / "clip.wav"
        await asyncio.to_thread(convert_to_wav, payload, wav)
        result = await asyncio.to_thread(transcribe_wav, model, wav, language)
    if not result["text"]:
        result["note"] = "没有识别到语音内容，请靠近麦克风再说一次。"
    return result


async def warmup():
    """Load in the background at startup so the first click is not the slow one.

    Two things are warmed, because both are slow and both would otherwise land on the
    first click: the gateway's own copy (used by the batch upload path) and the worker
    process that live readings run in. Spawning that worker imports torch and
    CTranslate2 inside a fresh interpreter, which measured over ten seconds here.
    """
    reap_orphan_workers()
    try:
        if available_model() is None:
            return
        await load()
    except (VoiceError, OSError) as exc:
        LOGGER.info("voice: warmup skipped (%s)", exc)
        return
    try:
        started = time.time()
        await _reading_offload(_silence_pcm(0.6), DEFAULT_LANGUAGE, "", True)
        LOGGER.info("voice: recognition worker warmed in %.1fs", time.time() - started)
    except (VoiceError, OSError, RuntimeError) as exc:
        LOGGER.info("voice: worker warmup skipped (%s)", exc)


def _silence_pcm(seconds):
    """Silent PCM of a given length, for warming the worker without any audio."""
    return b"\x00\x00" * int(seconds * SAMPLE_RATE)


# ---------------------------------------------------------------- streaming
class TooSoon(Exception):
    """Raised when a chunk arrived while the previous reading is still running.

    Not an error: the audio is already in the buffer and the next dispatch reads it.
    """


class VoiceStream:
    """One live recording: the audio so far, and the text already settled.

    The browser sends 16 kHz mono PCM as it records. Each reading re-reads only a short
    tail, so the cost per update is bounded no matter how long someone talks, and the
    text already committed is never re-derived — which is what keeps the visible text
    from flickering backwards.
    """

    def __init__(self, language=None, clock=time.time):
        self.id = uuid.uuid4().hex
        self.language = (language or DEFAULT_LANGUAGE).strip().lower()
        if self.language not in LANGUAGES:
            self.language = DEFAULT_LANGUAGE
        self.clock = clock
        self.buffer = bytearray()
        self.committed = ""
        self.pending = ""
        self.updated_at = clock()
        self.duration = 0.0
        # How much audio has been turned into settled text. The idle finalizer needs this
        # rather than "is the buffer empty": a live reading empties the buffer when it
        # commits, and calling that "nothing to do" would leave the phrase open forever.
        self.committed_seconds = 0.0
        self.last_seconds = 0.0
        self.runs = 0
        self.finished = False
        # Bumped every time a phrase is settled. The browser uses it to tell "the
        # recognizer is still revising this sentence" from "that sentence is final and a
        # new one started", which is what stops a resume after a pause from inserting the
        # whole transcript twice.
        self.seq = 0
        # True once a client has taken the final transcript, so a second "finish" for the
        # same phrase is an error rather than a silent replay.
        self.collected = False
        self.timer: asyncio.Task | None = None
        self._last_run = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- audio in
    def add(self, chunk: bytes) -> float:
        self.buffer.extend(chunk)
        self._trim()
        self.updated_at = self.clock()
        self.duration += len(chunk) / STREAM_CHUNK_BYTES / SAMPLE_RATE
        self.schedule_flush()
        return self.tail_seconds()

    def schedule_flush(self):
        """(Re)arm the idle finalization that reads the audio the client never finished."""
        self.cancel_flush()
        try:
            self.timer = asyncio.get_running_loop().call_later(
                STREAM_IDLE_FINISH_SECONDS, lambda: asyncio.create_task(idle_flush(self)),
            )
        except RuntimeError:
            # No loop (a unit test driving the object directly): the caller finishes.
            self.timer = None

    def cancel_flush(self):
        if self.timer is not None:
            with suppress(Exception):
                self.timer.cancel()
            self.timer = None

    def _trim(self):
        """Keep the buffer bounded: older audio is already committed text."""
        keep = int(STREAM_BUFFER_SECONDS * SAMPLE_RATE) * STREAM_CHUNK_BYTES
        if len(self.buffer) > keep:
            del self.buffer[:len(self.buffer) - keep]

    def tail_seconds(self) -> float:
        return len(self.buffer) / STREAM_CHUNK_BYTES / SAMPLE_RATE

    def should_run(self) -> bool:
        if self.tail_seconds() < STREAM_MIN_TAIL:
            return False
        return (self.clock() - self._last_run) >= STREAM_MIN_INTERVAL or self._silent_tail()

    def _tail_energy(self, seconds=STREAM_SILENCE_SECONDS) -> float:
        """RMS of the last moments, which is how a phrase end is spotted.

        Uses audioop when it is available (a C implementation) instead of summing tens of
        thousands of Python floats on the event loop.
        """
        count = int(seconds * SAMPLE_RATE) * STREAM_CHUNK_BYTES
        if len(self.buffer) < count:
            return 1.0
        tail = bytes(self.buffer[-count:])
        try:
            import audioop

            return float(audioop.rms(tail, 2))
        except ImportError:
            import array

            samples = array.array("h")
            samples.frombytes(tail)
            if not len(samples):
                return 0.0
            return (sum(float(value) * value for value in samples) / len(samples)) ** 0.5

    def _silent_tail(self) -> bool:
        return self._tail_energy() < STREAM_SILENCE_RMS

    # ------------------------------------------------------------- reading
    async def run(self, model=None, hint: str = "") -> None:
        """Refresh the hypothesis. This never settles text and never drops audio.

        An earlier version committed the transcript whenever the tail looked silent and
        then cleared the buffer. That lost audio in two measured ways: a tail that was
        still partly speech was thrown away (the visible text stopped at "…然后"), and a
        later read re-emitted the same words, so the sentence appeared twice. Settling is
        now the job of ``read_remaining``, which is the only place that consumes audio.
        """
        if self._lock.locked():
            raise TooSoon()
        async with self._lock:
            self.cancel_flush()
            self._last_run = self.clock()
            before = len(self.buffer)
            tail = bytes(self.buffer[-int(STREAM_MAX_TAIL * SAMPLE_RATE) * STREAM_CHUNK_BYTES:])
            result, seconds = await _reading_offload(
                tail, self.language, self.committed + hint, self._silent_tail(),
            )
            self.last_seconds = seconds
            self.runs += 1
            if result:
                self.pending = to_simplified(result)
            if self.buffer:
                # Audio is still waiting (either it arrived during the read or it was
                # never consumed), so nothing would close the phrase: the timer was
                # cleared on entry and no chunk will re-arm it.
                self.schedule_flush()
            elif before == 0 and self.committed_seconds >= self.duration and self.duration:
                # Nothing was in the buffer when this reading started and nothing arrived
                # since, so every byte that ever came in is settled text: the phrase is
                # over, which is the state the client's stop button asks for.
                self.finished = True

    def snapshot(self):
        return {
            "id": self.id,
            "committed": self.committed,
            "phrase": self.pending,
            "text": self.committed + self.pending,
            "seq": self.seq,
            "duration_seconds": round(self.duration, 2),
            "tail_seconds": round(self.tail_seconds(), 2),
            "runs": self.runs,
            "recognize_seconds": round(self.last_seconds, 2),
            "updated_at": self.updated_at,
            "finished": self.finished,
        }

    # ------------------------------------------------------------- finishing
    def grown_committed(self, result: str) -> str:
        """The transcript extended with everything in `result` that is not already there.

        A tail window overlaps what came before, so its transcript normally starts inside
        the committed text. The overlap is found at the character level rather than by
        trusting the recognizer to repeat its own words verbatim, because it usually does
        not: after committing "…两倍" the next reading of the same audio returned
        "两点,然后保存", which a naive append would turn into "…两倍两点,然后保存".
        """
        clean = to_simplified(str(result or "").strip())
        if not clean:
            return self.committed
        if not self.committed:
            return clean
        for size in range(min(len(clean), len(self.committed)), 0, -1):
            if self.committed.endswith(clean[:size]):
                return self.committed + clean[size:]
        return self.committed + clean

    def commit(self, result: str) -> None:
        self.committed = self.grown_committed(result)
        self.pending = ""
        self.committed_seconds = self.duration
        self.seq += 1

    async def read_remaining(self, model=None, only_if_silent: bool = False) -> dict:
        """Read whatever audio has not been settled yet, and return the transcript.

        This is the only place that consumes audio, and the only place that settles text.
        """
        self.cancel_flush()
        if self._lock.locked():
            # A reading is in flight; wait for it instead of racing it for the buffer.
            while self._lock.locked():
                await asyncio.sleep(0.1)
        if only_if_silent and not self._silent_tail():
            # The idle finalizer fired while the speaker was still talking (a slow browser
            # can stop sending for longer than that). There is nothing to close yet.
            self.schedule_flush()
            return self.snapshot()
        tail = bytes(self.buffer)
        self.buffer.clear()
        if tail:
            result, seconds = await _reading_offload(tail, self.language, self.committed, True)
            self.last_seconds = seconds
            if result:
                self.commit(result)
        return self.snapshot()

    # Named for what callers mean; both close the phrase.
    async def finish(self, model=None, only_if_silent: bool = False) -> dict:
        snapshot = await self.read_remaining(model, only_if_silent=only_if_silent)
        if not only_if_silent or self.settled():
            # Only a finish that actually consumed the audio may close the phrase: the
            # idle finalizer calls this with only_if_silent, and marking `finished` there
            # would end a sentence the speaker was still in the middle of.
            self.finished = True
        return snapshot

    def settled(self) -> bool:
        """Whether every byte that arrived has been turned into text."""
        return self.committed_seconds >= self.duration


def _recognize_pcm(model, pcm: bytes, language, prompt: str, silent: bool):
    """Recognize raw 16 kHz mono PCM in memory. Returns (text-or-None, seconds).

    The samples go to the model as an array, not through a temporary WAV. That is not a
    micro-optimization: measured on this machine, a half-second tail took **3.2 s via a
    file against 2.6 s as an array**, and the file path also hallucinated subtitles and
    credits on tails that short ("字幕by索兰娅", "AIRTUGEN HUNTERS") where the array path
    read the words correctly. A live reading is short by design, so it must not pay a
    decode step.
    """
    import array

    if len(pcm) < STREAM_CHUNK_BYTES * 8:
        return None, 0.0
    started = time.time()
    samples = array.array("h")
    samples.frombytes(pcm)
    kwargs = {
        "language": None if language == "auto" else language,
        "beam_size": 1,
        # VAD is skipped for live tails: it costs another pass over the audio, and a
        # sub-second tail is mostly speech anyway. Finalization turns it on, where a
        # trailing silence is worth trimming.
        "vad_filter": bool(silent),
        "condition_on_previous_text": False,
    }
    if prompt:
        # The prompt is what stops the model from re-writing text that is already on
        # screen; the repetition it can cause is stripped below.
        kwargs["initial_prompt"] = prompt[-120:]
    segments, _ = model.transcribe(_as_float_samples(samples), **kwargs)
    text = to_simplified("".join(segment.text for segment in segments).strip())
    seconds = time.time() - started
    if not text:
        return None, seconds
    if prompt:
        text = strip_repeat(prompt, text)
    return (text or None), seconds


def _as_float_samples(samples):
    """16-bit PCM as the float array the model wants, or a WAV path as a fallback."""
    try:
        import numpy

        return numpy.frombuffer(samples.tobytes(), dtype=numpy.int16).astype(numpy.float32) / 32768.0
    except ImportError:
        import wave

        folder = Path(tempfile.mkdtemp(prefix="qwen-stream-"))
        target = folder / "tail.wav"
        with wave.open(str(target), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(samples.tobytes())
        return str(target)


def strip_repeat(prompt: str, text: str) -> str:
    """Remove the part of `text` that only repeats the end of `prompt`.

    Whisper echoes its initial_prompt surprisingly often, so the echo is dropped instead
    of being shown twice. Overlap is matched on the tail of the prompt, longest first.
    """
    if not prompt or not text:
        return text
    cleaned = prompt.rstrip()
    for size in range(min(len(cleaned), len(text)), 0, -1):
        if cleaned.endswith(text[:size]):
            return text[size:]
    return text


def stream_for(session: str, language=None) -> VoiceStream:
    """The live recording for this session, creating it on first audio."""
    _prune_streams()
    stream = _STREAMS.get(session)
    if stream is None or (language and (language or "").strip().lower() != stream.language):
        stream = VoiceStream(language)
        _STREAMS[session] = stream
        LOGGER.info("voice: streaming session %s started (language=%s)", stream.id, stream.language)
    return stream


def stream_get(session: str) -> VoiceStream | None:
    return _STREAMS.get(session)


def stream_get_finished(session: str) -> VoiceStream | None:
    """A finalized phrase, kept only long enough for the caller to collect it."""
    return _FINISHED.get(session)


def stream_drop(session: str):
    stream = _STREAMS.pop(session, None)
    if stream is not None:
        stream.cancel_flush()
        if stream.finished or stream.committed:
            _FINISHED[session] = stream
            _prune_finished()
        LOGGER.info("voice: streaming session %s closed after %d reading(s)", stream.id, stream.runs)
    return stream


def _prune_finished():
    """Remember only the most recent finished phrases."""
    while len(_FINISHED) > FINISHED_KEEP:
        _FINISHED.pop(next(iter(_FINISHED)), None)


def _prune_streams():
    deadline = time.time() - STREAM_TTL_SECONDS
    for session in [key for key, value in _STREAMS.items() if value.updated_at < deadline]:
        _STREAMS.pop(session, None)


async def stream_feed(session: str, pcm: bytes, language=None) -> dict:
    """Take one chunk of live audio, refresh the hypothesis, and return it at once.

    The reading is not awaited: on this CPU a reading of the tail can take longer than
    the audio that arrived, so waiting would make every chunk slower than realtime and
    the stream would fall further behind with every word. Dispatching it in the
    background means the caller gets the latest text immediately and the next reading
    picks up everything that arrived meanwhile.

    An empty body is a poll, not audio: it returns the current state without touching the
    idle timer, which is what lets the browser ask "any text yet?" between chunks.
    """
    session = session or "anonymous"
    if not pcm:
        stream = stream_get(session) or stream_get_finished(session)
        if stream is None:
            raise VoiceError("这段录音已经结束了，请重新开始。", status=409)
        return stream.snapshot()
    if len(pcm) % STREAM_CHUNK_BYTES:
        raise VoiceError("音频分片不是 16 位采样。")
    stream = stream_for(session, language)
    model = await load()
    stream.add(pcm)
    if stream.should_run():
        asyncio.create_task(_run_reading(stream, model))
    return stream.snapshot()


async def _run_reading(stream: "VoiceStream", model) -> None:
    """One background reading; a failure must never kill the recording."""
    try:
        await stream.run(model)
    except TooSoon:
        pass
    except (VoiceError, OSError, ValueError, RuntimeError) as exc:
        LOGGER.warning("voice: live reading failed (%s)", exc)


async def idle_flush(stream: "VoiceStream") -> None:
    """Finalize a phrase the client has stopped sending audio for.

    The audio's own schedule, not the client's: a browser throttles timers in a
    background tab and a user may simply stop talking, and in both cases the words already
    spoken still have to end up in the field. It only closes a silence: a slow client that
    pauses mid-sentence for longer than this gets its timer re-armed instead of its
    sentence cut in half.
    """
    if stream.finished:
        return
    if not stream.duration:
        return
    if stream._lock.locked():
        # A reading owns the tail; the phrase is not settled yet, so try again.
        stream.timer = asyncio.get_running_loop().call_later(
            STREAM_IDLE_FINISH_SECONDS, lambda: asyncio.create_task(idle_flush(stream)),
        )
        return
    try:
        await stream.finish(only_if_silent=True)
        if stream.finished:
            LOGGER.info("voice: finalized %d char(s) after %.1fs of silence",
                        len(stream.committed), STREAM_IDLE_FINISH_SECONDS)
    except (VoiceError, OSError, ValueError, RuntimeError) as exc:
        LOGGER.warning("voice: idle finalization failed (%s)", exc)


async def stream_finish(session: str, language=None) -> dict:
    """End the recording: read what is left and hand back the transcript.

    Succeeds even when the idle timer already finalized the phrase, because by then the
    transcript is exactly what the caller wants; only a session that never existed is an
    error.
    """
    stream = stream_get(session) or stream_get_finished(session)
    if stream is None or (stream.finished and stream.collected):
        raise VoiceError("这段录音已经结束了，请重新开始。", status=409)
    model = await load()
    snapshot = await stream.finish(model)
    stream.collected = True
    stream_drop(session)
    if not snapshot["text"]:
        snapshot["note"] = "没有识别到语音内容，请靠近麦克风再说一次。"
    return snapshot
