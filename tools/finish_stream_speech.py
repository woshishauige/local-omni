"""Finish the streaming-speech rewrite in one atomic pass.

The panel was half-migrated: a producer/consumer pair had been added while the old
serial pump, its duplicate queue declaration and the now-unused whole-reply speaker
were still present, so a turn would both stall at every sentence and re-speak the
previous reply. This applies the remaining edits together and refuses to write unless
every anchor matches exactly once.
"""

from pathlib import Path
import re
import sys

TARGET = Path(r"D:\AI\Qwen-Local\web\voice-panel.js")


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: anchor found {count} times, expected 1")
    return text.replace(old, new, 1)


def main():
    js = TARGET.read_text(encoding="utf-8")
    original = js

    # 1. The pipeline's queue was declared a second time; the later declaration wins
    #    and the early one is dead.
    js = replace_once(js, "    const queue = [];\n    let buffer = \"\";",
                      "    let buffer = \"\";", "duplicate queue")

    # 2. Drop the now-unused serial flag.
    js = replace_once(js, "    let pumping = false;\n", "", "pumping flag")

    # 3. Feed the pipeline instead of the removed serial pump.
    js = replace_once(js, "      void pumpSpeech();", "      if (state.speakReplies) speechHandle.push(trimmed);",
                      "consume push") if "void pumpSpeech();" in js else js

    # The loop above pushes every sentence; ensure the flush line hands over the tail.
    js = js.replace("      if (buffer.length >= 60) { queue.push(buffer.trim()); buffer = \"\"; }",
                    "      if (buffer.length >= 60) { speechHandle.push(buffer.trim()); buffer = \"\"; }")

    # Every push goes through the handle so the two loops are the only consumers.
    js = re.sub(r"queue\.push\((trimmed|buffer\.trim\(\)|sentence)\)", r"speechHandle.push(\1)", js)

    # 4. Wait for both loops instead of the old serial pump.
    js = replace_once(js, "    if (buffer.trim()) { queue.push(buffer.trim()); buffer = \"\"; }\n    await pumpSpeech();",
                      "    if (buffer.trim()) speechHandle.push(buffer.trim());\n"
                      "    speechHandle.close();\n"
                      "    await Promise.all([synthesisTask, playbackTask]);", "pump await")

    # 5. Start the loops and stop anything still speaking from the previous turn.
    js = replace_once(js, "    const speechHandle = { push: s => queue.push(s), close: () => { pipelineClosed = true; } };",
                      "    const speechHandle = { push: s => queue.push(s), close: () => { pipelineClosed = true; } };\n"
                      "    const synthesisTask = synthesisLoop();\n"
                      "    const playbackTask = playbackLoop();", "start loops")

    # 6. `ask` must invalidate the previous turn's audio; without it the old queue keeps
    #    playing under the new reply, which is what sounded like a repeated answer.
    js = replace_once(js,
                      "    panel.dataset.phase = \"thinking\";\n    hint.textContent = \"正在思考…\";",
                      "    stopSpeaking();   // whatever is still queued belongs to the previous turn\n"
                      "    panel.dataset.phase = \"thinking\";\n    hint.textContent = \"正在思考…\";",
                      "stop previous speech")

    # 7. Speak one sentence: render it, or report that it needs the browser voice.
    js = replace_once(js, "  // Speak one already-split sentence, preferring the local voice and falling back\n"
                          "  // per-sentence so a failure never silences the rest of the reply.\n"
                          "  async function speakSentence(sentence) {",
                      "  // Render one sentence with the local voice. Returns a Blob on success and null\n"
                      "  // when the caller should fall back to the browser voice for that sentence.\n"
                      "  async function renderSentence(sentence) {", "renderSentence signature")
    js = replace_once(js, "  async function renderSentence(sentence) {\n"
                          "    if (!state.speakReplies || !sentence) return;\n"
                          "    const token = speechToken;",
                      "  async function renderSentence(sentence) {\n"
                      "    if (!sentence) return null;\n"
                      "    const token = speechToken;", "renderSentence guard")
    js = replace_once(js, "      if (token !== speechToken) return;\n"
                          "      if (response.ok) {\n"
                          "        const blob = await response.blob();\n"
                          "        if (blob.size > 512) {\n"
                          "          factRows.tts.textContent = state.voice;\n"
                          "          await playBlob(blob, token);\n"
                          "          return;\n"
                          "        }\n"
                          "      }\n"
                          "    } catch (failure) { /* fall back below */ }\n"
                          "    if (token !== speechToken) return;\n"
                          "    factRows.tts.textContent = \"浏览器\";\n"
                          "    await browserSpeakAsync(sentence, token);\n"
                          "  }",
                      "      if (token !== speechToken) return null;\n"
                      "      if (response.ok) {\n"
                      "        const blob = await response.blob();\n"
                      "        if (blob.size > 512) {\n"
                      "          factRows.tts.textContent = state.voice;\n"
                      "          return blob;\n"
                      "        }\n"
                      "      }\n"
                      "    } catch (failure) { /* the caller falls back */ }\n"
                      "    factRows.tts.textContent = \"浏览器\";\n"
                      "    return null;\n"
                      "  }", "renderSentence body")

    # 8. Remove the whole-reply speaker: the streaming path owns speech now, and leaving
    #    it in place would be a second way to speak the same reply.
    start = js.find("  async function speak(text) {")
    if start != -1:
        marker = "  function stopSpeaking() {"
        end = js.find(marker, start)
        if end == -1:
            raise SystemExit("could not locate the end of the dead speak()")
        js = js[:start] + js[end:]

    unused = "  let speechQueue = [];\n"
    js = js.replace(unused, "")

    if js == original:
        print("no changes were needed")
        return 0
    TARGET.write_text(js, encoding="utf-8")
    print(f"rewrote voice-panel.js: {len(original)} -> {len(js)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
