"""Complete the model picker by inserting the five missing pieces.

The file mixes encodings (UTF-8 `\\uXXXX` escapes in the upper half, raw GBK bytes
lower down), so this operates on bytes only and inserts ASCII-only source. Chinese
strings are written as the literal two-character sequence backslash-u-XXXX, which
is exactly what JavaScript expects and keeps every inserted byte below 0x80.

Every insertion is anchored on a unique ASCII marker and refuses to act if its
anchor is missing or ambiguous, so a partial application cannot silently mangle the
file.
"""

from pathlib import Path
import sys

TARGET = Path(r"D:\AI\Qwen-Local\web\mode-control.js")

# 1. words entries, inserted before the closing brace of the words object.
WORDS_ANCHOR = b"  const toolbar = element(\"header\", \"local-mode-toolbar\");"
WORDS_BLOCK = (
    b"  // Model picker strings; escaped so this block stays ASCII.\n"
    b"  words.model = \"\\u6a21\\u578b\";\n"
    b"  words.tradeoffs = \"\\u6a21\\u578b\\u4f18\\u7f3a\\u70b9\";\n"
    b"  words.tradeoffsSuffix = \" \\u00b7 \\u4f18\\u7f3a\\u70b9\";\n"
    b"  words.pros = \"\\u4f18\\u70b9\";\n"
    b"  words.cons = \"\\u7f3a\\u70b9\";\n"
    b"  words.close = \"\\u5173\\u95ed\";\n"
    b"  words.notInstalled = \"\\uff08\\u672a\\u4e0b\\u8f7d\\u5b8c\\u6210\\uff09\";\n"
    b"  words.modelSwitching = \"\\u6a21\\u578b\\u5207\\u6362\\u4e2d\";\n"
    b"  words.modelFailed = \"\\u6a21\\u578b\\u5207\\u6362\\u5931\\u8d25\";\n\n"
)

# 2. state variables, beside the mode-switch ones they mirror.
VARS_ANCHOR = b"  let modeRequestVersion = 0;"
VARS_BLOCK = (
    b"  let modelRequestVersion = 0;\n"
    b"  let initialAlias;\n"
)

# 3. mount the picker inside the existing flex row.
ROW_ANCHOR = b"  row.append(brand, modes, status, retry);"
ROW_REPLACEMENT = b"  row.append(brand, picker, modes, status, retry);"

# 4. mount the trade-offs panel as a sibling of that row.
TOOLBAR_ANCHOR = b"  toolbar.append(row, error);"
TOOLBAR_REPLACEMENT = b"  toolbar.append(row, error);\n  toolbar.append(tradeoffs);"

# 5. the switch itself, next to the mode switch it mirrors.
CHANGE_ANCHOR = b"  document.addEventListener(\"submit\", event => {"
CHANGE_BLOCK = b"""  async function changeModel(profile) {
    // Same guards as a mode change: a running generation owns the backend, and a
    // model switch restarts it, so it must not interrupt work in flight.
    if (window.ImagePanel && window.ImagePanel.isBusy()) {
      localError = words.imageBusy;
      render();
      return;
    }
    if (!current || switching || recovering || current.active_requests > 0 || nativeAgentBusy()) return;
    if (hasAttachments()) { localError = words.attachments; render(); return; }
    try {
      saveDraft();
      modelRequestVersion += 1;
      switching = true;
      localError = "";
      modelSelect.disabled = true;
      render();
      const response = await fetch("/local/model", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile }), signal: AbortSignal.timeout(15000),
      });
      const next = await response.json();
      if (!response.ok) {
        if (next.mode) acceptStatus(next);
        throw new Error(response.status === 409 ? words.wait : next.error || words.modelFailed);
      }
      acceptStatus(next);
      // The switch is asynchronous (202). Poll until the new profile reports
      // ready, then acceptStatus sees the changed alias and reloads the page so
      // the native UI re-reads the backend's capabilities.
      const deadline = Date.now() + 300000;
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 900));
        const probe = await fetch("/local/status", { signal: AbortSignal.timeout(5000) });
        const status = await probe.json();
        if (status.model_profile === profile && status.state === "ready") { acceptStatus(status); return; }
        if (status.state === "error") { acceptStatus(status); throw new Error(status.error || words.modelFailed); }
      }
      throw new Error(words.modelFailed);
    } catch (failure) {
      localError = failure.message || words.modelFailed;
    } finally {
      switching = false;
      modelSelect.disabled = false;
      render();
      void pollStatus();
    }
  }

"""


def apply(data, anchor, replacement, label, count=1):
    found = data.count(anchor)
    if found != count:
        raise SystemExit(f"{label}: anchor found {found} times, expected {count}")
    return data.replace(anchor, replacement, count)


def main():
    data = TARGET.read_bytes()
    if b"async function changeModel" in data:
        print("already complete; nothing to do")
        return 0
    before = len(data)
    data = apply(data, WORDS_ANCHOR, WORDS_BLOCK + WORDS_ANCHOR, "words block")
    data = apply(data, VARS_ANCHOR, VARS_ANCHOR + b"\n" + VARS_BLOCK.rstrip(b"\n"), "state vars")
    data = apply(data, ROW_ANCHOR, ROW_REPLACEMENT, "picker mount")
    data = apply(data, TOOLBAR_ANCHOR, TOOLBAR_REPLACEMENT, "tradeoffs mount")
    data = apply(data, CHANGE_ANCHOR, CHANGE_BLOCK + CHANGE_ANCHOR, "changeModel")
    TARGET.write_bytes(data)
    print(f"completed model picker: {before} -> {len(data)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
