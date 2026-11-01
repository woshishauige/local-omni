"""Show each spoken reply as a playable voice bar with a transcript toggle.

The reply already exists as audio, so rendering it as a wall of text wastes the thing
that was actually produced. Each assistant turn becomes a bar — play button, waveform,
duration — that replays the cached sentences on click, with the transcript one tap
away. The transcript is kept hidden on the bar and revealed on demand.

Anchors are matched exactly once, so a partial application cannot leave the panel in a
half-migrated state.
"""

from pathlib import Path
import sys

TARGET = Path(r"D:\AI\Qwen-Local\web\voice-panel.js")


def once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: anchor found {count} times, expected 1")
    return text.replace(old, new, 1)


VOICE_BAR_HELPERS = '''
  // ---------------------------------------------------------------- voice bar

  function waveformBars(seed) {
    const box = el("div", "local-voice-bar-wave");
    // A stable pseudo-random profile per reply, so a bar looks like a waveform without
    // claiming to be a real amplitude plot of the audio.
    let state = 0;
    for (let i = 0; i < seed.length; i += 1) state = (state * 31 + seed.charCodeAt(i)) >>> 0;
    for (let i = 0; i < 24; i += 1) {
      state = (state * 1103515245 + 12345) >>> 0;
      const height = 6 + (state % 18);
      const bar = el("span", "");
      bar.style.height = height + "px";
      box.append(bar);
    }
    return box;
  }

  function createVoiceBar(text, segments, totalMs) {
    // `segments` are the rendered pieces in order: audio blobs to replay, or the text of
    // sentences that had to be spoken by the browser.
    const bar = el("div", "local-voice-bar");
    bar.tabIndex = 0;
    bar.setAttribute("role", "button");
    bar.title = "点击重播";
    bar.append(el("span", "local-voice-bar-play"), waveformBars(text));
    bar.append(el("span", "local-voice-bar-time", (totalMs / 1000).toFixed(1) + "″"));

    const toggle = el("button", "local-voice-bar-toggle", "转文字");
    toggle.type = "button";
    const transcript = el("div", "local-voice-transcript", text);
    transcript.hidden = true;
    toggle.addEventListener("click", event => {
      event.stopPropagation();
      transcript.hidden = !transcript.hidden;
      toggle.textContent = transcript.hidden ? "转文字" : "收起文字";
    });

    let replaying = false;
    const replay = async () => {
      if (replaying) return;
      replaying = true;
      bar.classList.add("is-playing");
      try {
        for (const segment of segments) {
          if (segment.blob) await playBlob(segment.blob, speechToken);
          else if (segment.text) await browserSpeakAsync(segment.text, speechToken);
        }
      } finally {
        replaying = false;
        bar.classList.remove("is-playing");
      }
    };
    bar.addEventListener("click", () => void replay());
    bar.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); void replay(); }
    });

    const column = el("div", "local-voice-bar-text");
    column.append(bar, toggle, transcript);
    return column;
  }

'''

# The bar replaces the plain text bubble for assistant turns.
ADD_BUBBLE_OLD = '''  function addAssistantBubble() {
    empty.remove();
    const row = el("div", "local-voice-turn is-assistant");
    row.append(el("div", "local-voice-avatar", "AI"));
    const column = el("div", "");
    const bubble = el("div", "local-voice-bubble", "");
    column.append(bubble);
    row.append(column);
    log.append(row);
    return bubble;
  }'''

ADD_BUBBLE_NEW = '''  // While the reply streams, only a placeholder is shown: the text is not the artefact
  // being produced, so it stays off screen until the turn finishes and becomes a bar.
  function addAssistantBubble() {
    empty.remove();
    const row = el("div", "local-voice-turn is-assistant");
    row.append(el("div", "local-voice-avatar", "AI"));
    const column = el("div", "");
    const bubble = el("div", "local-voice-bubble", "…");
    column.append(bubble);
    row.append(column);
    log.append(row);
    return bubble;
  }'''


def main():
    js = TARGET.read_text(encoding="utf-8")
    if "function createVoiceBar" in js:
        print("already patched")
        return 0

    # 1. Helpers, inserted before the streaming entry point that uses them.
    js = once(js, "  async function askStreaming(text) {",
              VOICE_BAR_HELPERS + "  async function askStreaming(text) {", "bar helpers")

    # 2. Assistant turns render as a bar, not as text.
    js = once(js, ADD_BUBBLE_OLD, ADD_BUBBLE_NEW, "addAssistantBubble")

    # 3. Keep the rendered audio so the bar can replay it without re-synthesising.
    js = once(js, "        if (blob) {\n          speechBlobs.push({ blob, duration: await wavDurationMs(blob) });",
              "        if (blob) {\n"
              "          const duration = await wavDurationMs(blob);\n"
              "          speechBlobs.push({ blob, duration });\n"
              "          spokenSegments.push({ blob, duration });", "collect audio")
    js = once(js, "          speechBlobs.push({ text: sentence, duration: 1500 });",
              "          speechBlobs.push({ text: sentence, duration: 1500 });\n"
              "          spokenSegments.push({ text: sentence, duration: 1500 });", "collect fallback")

    # 4. Declare the collector beside the other per-turn state.
    js = once(js, "    const speechBlobs = [];\n    let pipelineClosed = false;",
              "    const speechBlobs = [];\n"
              "    // Everything that was actually spoken this turn, in order, for replay.\n"
              "    const spokenSegments = [];\n"
              "    let pipelineClosed = false;", "declare collector")

    # 5. The bar is built after the audio pipeline drains, so it knows the real length.
    js = once(js, "    const finalText = (full.trim() || \"（模型没有返回内容）\");",
              "    if (bubble.parentElement) {\n"
              "      const totalMs = spokenSegments.reduce((sum, s) => sum + (s.duration || 0), 0);\n"
              "      const bar = createVoiceBar(full.trim() || \"（模型没有返回内容）\", spokenSegments, totalMs);\n"
              "      // Swap the streaming placeholder for the playable bar in place.\n"
              "      bubble.replaceWith(bar);\n"
              "    }\n"
              "    const finalText = (full.trim() || \"（模型没有返回内容）\");", "build bar")
    return_code = 0

    TARGET.write_text(js, encoding="utf-8")
    print("patched voice-panel.js for voice bars")
    return return_code


if __name__ == "__main__":
    sys.exit(main())
