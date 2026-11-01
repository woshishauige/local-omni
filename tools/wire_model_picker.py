"""Wire the model picker to state: render integration and the change listener.

Inserted after changeModel exists so the listener can reference it. ASCII-only, for
the same mixed-encoding reason as complete_model_picker.py.
"""

from pathlib import Path
import sys

TARGET = Path(r"D:\AI\Qwen-Local\web\mode-control.js")

# Sync the option list and trade-offs from every accepted status, so a change made
# elsewhere (or a profile appearing after its download finishes) is reflected.
RENDER_ANCHOR = b"    mtp.title = current?.mtp_reason || \"\";"
RENDER_BLOCK = b"""    mtp.title = current?.mtp_reason || "";
    // The gateway owns the profile list, so refresh the picker from each status
    // rather than from anything cached in this file.
    syncModelOptions(current?.model_profiles);
    modelSelect.value = current?.model_profile || "";
    modelSelect.disabled = loading || busy || attachments || disconnected || !current;
    renderTradeoffs();
"""

# The listener lives after changeModel is defined; function declarations hoist, but
# keeping the order obvious avoids depending on that.
LISTENER_ANCHOR = b"  document.addEventListener(\"submit\", event => {"
LISTENER_BLOCK = b"""  modelSelect.addEventListener("change", () => {
    const wanted = modelSelect.value;
    if (wanted && wanted !== (current && current.model_profile)) void changeModel(wanted);
    else modelSelect.value = (current && current.model_profile) || "";
  });

"""


def main():
    data = TARGET.read_bytes()
    if b"modelSelect.addEventListener" in data:
        print("already wired; nothing to do")
        return 0
    for anchor, label in ((RENDER_ANCHOR, "render"), (LISTENER_ANCHOR, "listener")):
        if data.count(anchor) != 1:
            raise SystemExit(f"{label}: anchor found {data.count(anchor)} times")
    data = data.replace(RENDER_ANCHOR, RENDER_BLOCK, 1)
    data = data.replace(LISTENER_ANCHOR, LISTENER_BLOCK + LISTENER_ANCHOR, 1)
    TARGET.write_bytes(data)
    print("wired model picker")
    return 0


if __name__ == "__main__":
    sys.exit(main())
