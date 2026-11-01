"""Insert an ASCII-only block into mode-control.js at a byte level.

The file is mixed-encoding: the first ~37 lines escape their Chinese as \\uXXXX,
but the later lines carry raw GBK bytes (Get-Content decodes them as mojibake).
Reading it as UTF-8 text and writing it back would re-encode those GBK bytes and
corrupt every one of those lines. So this script never decodes the file: it splits
on an ASCII anchor, splices in ASCII-only source, and writes the bytes back.
"""

from pathlib import Path
import sys

TARGET = Path(r"D:\AI\Qwen-Local\web\mode-control.js")

ANCHOR = b"  function updateHeight() {"
BLOCK = b"""  // Model picker. Chinese is written as \\uXXXX escapes so this block stays pure
  // ASCII and the surrounding file's mixed encoding is left untouched.
  const picker = element("label", "local-model-picker");
  const pickerLabel = element("span", "local-model-picker-label", words.model);
  const modelSelect = element("select", "local-model-select");
  modelSelect.setAttribute("aria-label", words.model);
  let modelOptions = "";
  let showingTradeoffs = false;

  const tradeoffs = element("section", "local-model-tradeoffs");
  tradeoffs.hidden = true;
  const tradeoffsTitle = element("h2", "local-model-tradeoffs-title");
  const tradeoffsBody = element("div", "local-model-tradeoffs-body");
  const tradeoffsClose = element("button", "local-model-tradeoffs-close");
  tradeoffsClose.type = "button";
  tradeoffsClose.textContent = words.close;
  tradeoffsClose.addEventListener("click", () => { showingTradeoffs = false; renderTradeoffs(); });
  tradeoffs.append(tradeoffsTitle, tradeoffsClose, tradeoffsBody);

  const tradeoffsButton = element("button", "local-model-info");
  tradeoffsButton.type = "button";
  tradeoffsButton.setAttribute("aria-label", words.tradeoffs);
  tradeoffsButton.title = words.tradeoffs;
  tradeoffsButton.textContent = "?";
  tradeoffsButton.addEventListener("click", event => {
    event.preventDefault();
    showingTradeoffs = !showingTradeoffs;
    renderTradeoffs();
  });

  picker.append(pickerLabel, modelSelect, tradeoffsButton);

  // Fill the options from the gateway's own report, so a model added server-side
  // shows up without editing this file.
  function syncModelOptions(profiles) {
    if (!Array.isArray(profiles) || !profiles.length) return;
    const signature = profiles.map(profile => profile.name + ":" + profile.installed).join("|");
    if (signature === modelOptions) return;
    modelOptions = signature;
    modelSelect.textContent = "";
    for (const profile of profiles) {
      const option = document.createElement("option");
      option.value = profile.name;
      // Listed but unselectable while its weights download: hiding it would make
      // the picker look broken rather than incomplete.
      option.disabled = !profile.installed;
      option.textContent = profile.installed ? profile.label : profile.label + words.notInstalled;
      modelSelect.append(option);
    }
  }

  function activeProfile() {
    const list = Array.isArray(current && current.model_profiles) ? current.model_profiles : [];
    return list.find(profile => profile.name === (current && current.model_profile)) || null;
  }

  function renderTradeoffs() {
    const profile = activeProfile();
    tradeoffs.hidden = !showingTradeoffs || !profile;
    tradeoffsButton.setAttribute("aria-expanded", String(showingTradeoffs));
    if (tradeoffs.hidden) return;
    tradeoffsTitle.textContent = profile.label + words.tradeoffsSuffix;
    tradeoffsBody.textContent = "";
    for (const [heading, items, className] of [
      [words.pros, profile.pros || [], "is-pros"],
      [words.cons, profile.cons || [], "is-cons"],
    ]) {
      const block = element("div", "local-model-tradeoff-group " + className);
      block.append(element("h3", "", heading));
      const list = element("ul", "");
      for (const item of items) list.append(element("li", "", String(item)));
      block.append(list);
      tradeoffsBody.append(block);
    }
  }

"""


def main():
    data = TARGET.read_bytes()
    if b"local-model-picker" in data:
        print("already inserted; nothing to do")
        return 0
    count = data.count(ANCHOR)
    if count != 1:
        print(f"anchor count = {count} (expected 1); refusing to edit")
        return 1
    data = data.replace(ANCHOR, BLOCK + ANCHOR, 1)
    # The tradeoffs panel is a sibling of the mode row, so it is appended after it.
    row_append = b"  toolbar.append(row, error);"
    if data.count(row_append) != 1:
        print(f"row anchor count = {data.count(row_append)}; refusing")
        return 1
    data = data.replace(row_append, row_append + b"\n  toolbar.append(tradeoffs);", 1)
    # Mount the picker inside the existing flex row, before the mode buttons.
    picker_anchor = b"  row.append(brand, modes, status, retry);"
    if data.count(picker_anchor) != 1:
        print(f"picker anchor count = {data.count(picker_anchor)}; refusing")
        return 1
    data = data.replace(picker_anchor, b"  row.append(brand, picker, modes, status, retry);", 1)
    TARGET.write_bytes(data)
    print("inserted model picker block")
    return 0


if __name__ == "__main__":
    sys.exit(main())
