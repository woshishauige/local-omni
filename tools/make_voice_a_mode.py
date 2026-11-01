"""Make voice a peer mode of text/vision/generate/video.

Voice was a floating overlay with its own launcher button. It becomes a fifth entry in
the existing mode segment and is driven by the same mechanism as the image and video
panels (`activeTab` plus a `setActive` hook). Indentation in these files follows the
surrounding block, so every anchor below is written with the exact leading spaces the
file uses and must match exactly once.
"""

from pathlib import Path
import sys

MODE_JS = Path(r"D:\AI\Qwen-Local\web\mode-control.js")
VOICE_JS = Path(r"D:\AI\Qwen-Local\web\voice-panel.js")


def once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: anchor found {count} times, expected 1")
    return text.replace(old, new, 1)


def patch_mode_control(js):
    # 1. Microphone icon (icons table is indented 4).
    js = once(
        js,
        '    retry: [["path", { d: "M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8" }], ["path", { d: "M21 3v5h-5" }]],',
        '    voice: [["path", { d: "M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" }],\n'
        '            ["path", { d: "M19 10v1a7 7 0 0 1-14 0v-1" }],\n'
        '            ["line", { x1: "12", y1: "19", x2: "12", y2: "22" }]],\n'
        '    retry: [["path", { d: "M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8" }], ["path", { d: "M21 3v5h-5" }]],',
        "mic icon")

    # 2. Label, plus the confirmation shown before the card is taken.
    js = once(
        js,
        '  // Model picker strings; escaped so this block stays ASCII.',
        '  words.voice = "\\u8bed\\u97f3\\u5bf9\\u8bdd";\n'
        '  words.voiceConfirm = "\\u8bed\\u97f3\\u5bf9\\u8bdd\\u9700\\u8981\\u72ec\\u5360 9B \\u6a21\\u578b'
        '\\uff0c\\u4f1a\\u6682\\u65f6\\u91ca\\u653e\\u5f53\\u524d\\u6a21\\u578b\\u5360\\u7528\\u7684\\u663e\\u5b58'
        '\\u3002\\u7ee7\\u7eed\\u5417\\uff1f";\n\n'
        '  // Model picker strings; escaped so this block stays ASCII.',
        "voice words")

    # 3. Fifth segment, ordered with the other conversational views (indent 2).
    js = once(js, '  for (const mode of ["text", "vision", "generate", "video"]) {',
              '  for (const mode of ["text", "vision", "voice", "generate", "video"]) {', "mode list")

    # 4. A tab predicate like the other screen-holding views (indent 2).
    js = once(
        js,
        "  function videoTab() {",
        "  // The voice view holds the screen the way generate does, but entering it does\n"
        "  // not switch models: the panel loads the voice pair only when a turn starts, so\n"
        "  // the other views keep whatever model they were using.\n"
        "  function voiceTab() {\n"
        "    return activeTab === \"voice\";\n"
        "  }\n\n"
        "  function videoTab() {",
        "voiceTab")

    # 5. Selecting voice opens the view; the confirmation precedes taking the card.
    js = once(
        js,
        '    if (tab === "generate" || tab === "video") {',
        '    if (tab === "voice") {\n'
        '      if (!voiceTab() && !window.confirm(words.voiceConfirm)) return;\n'
        '      storeTab(tab);\n'
        '      render();\n'
        '      return;\n'
        '    }\n'
        '    if (tab === "generate" || tab === "video") {',
        "selectTab voice")

    # 6. Drive the panel the way the image and video panels are driven (indent 4).
    js = once(
        js,
        "    const videoPanel = window.VideoPanel;\n"
        "    if (videoPanel) videoPanel.setActive(Boolean(video && current), render);",
        "    const videoPanel = window.VideoPanel;\n"
        "    if (videoPanel) videoPanel.setActive(Boolean(video && current), render);\n"
        "    const voicePanel = window.VoicePanel;\n"
        "    if (voicePanel) voicePanel.setActive(Boolean(voice && current), render);",
        "render voice")

    # 7. Resolve the tab once, beside the others (indent 4).
    js = once(
        js,
        "    const generate = generateTab();\n    const video = videoTab();",
        "    const generate = generateTab();\n    const video = videoTab();\n    const voice = voiceTab();",
        "resolve voice")

    # 8. Pressed state for the new button (indent 6).
    js = once(
        js,
        '      const pressed = mode === "generate" ? generate : mode === "video" ? video\n'
        '        : !generate && !video && current?.mode === mode;',
        '      const pressed = mode === "generate" ? generate\n'
        '        : mode === "video" ? video\n'
        '        : mode === "voice" ? voice\n'
        '        : !generate && !video && !voice && current?.mode === mode;',
        "pressed state")
    return js


def patch_voice_panel(js):
    # Expose the same shape ImagePanel and VideoPanel expose, so the toolbar drives it.
    js = once(
        js,
        "    window.VoicePanel = { open, close, toggle: () => (state.open ? close() : open()), ask };",
        "    window.VoicePanel = {\n"
        "      setActive: on => { if (on) open(); else close(); },\n"
        "      isBusy: () => state.busy || state.recording,\n"
        "      open, close, ask,\n"
        "    };",
        "VoicePanel api")

    # Leaving voice returns to the text view, like leaving any other mode.
    js = once(js, "  closeBtn.addEventListener(\"click\", close);",
              "  closeBtn.addEventListener(\"click\", () => {\n"
              "    const modes = window.LocalModes;\n"
              "    if (modes && modes.select) modes.select(\"text\"); else close();\n"
              "  });", "close button")

    # It is a region of the page now, not a modal.
    js = once(js, '  panel.setAttribute("role", "dialog");\n  panel.setAttribute("aria-modal", "true");',
              '  panel.setAttribute("role", "region");', "panel role")

    js = once(js, "  function open() {\n    state.open = true;\n    panel.hidden = false;",
              "  function open() {\n    state.open = true;\n    panel.hidden = false;\n"
              "    panel.classList.add(\"is-open\");", "open class")
    js = once(js, "  function close() {\n    state.open = false;\n    panel.hidden = true;",
              "  function close() {\n    state.open = false;\n    panel.hidden = true;\n"
              "    panel.classList.remove(\"is-open\");", "close class")
    return js


def main():
    mode = MODE_JS.read_text(encoding="utf-8")
    if "function voiceTab()" in mode:
        print("mode-control.js already patched")
    else:
        MODE_JS.write_text(patch_mode_control(mode), encoding="utf-8")
        print("patched mode-control.js")

    voice = VOICE_JS.read_text(encoding="utf-8")
    if "setActive: on =>" in voice:
        print("voice-panel.js already patched")
    else:
        VOICE_JS.write_text(patch_voice_panel(voice), encoding="utf-8")
        print("patched voice-panel.js")
    return 0


if __name__ == "__main__":
    sys.exit(main())
