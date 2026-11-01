"""Complete the model-comparison UI: the four missing strings and their styles.

The earlier text edit landed the renderer and the call sites but not the `words.*`
assignments it referenced, so the panel would have rendered "undefined" labels, and
the CSS block never landed at all. Both files mix encodings, so this works on bytes
with Chinese written as `\\uXXXX` escapes and refuses to write unless every anchor is
found exactly once.
"""

from pathlib import Path
import sys

JS = Path(r"D:\AI\Qwen-Local\web\mode-control.js")
CSS = Path(r"D:\AI\Qwen-Local\web\mode-control.css")

JS_ANCHOR = b'  words.modelFailed = "\\u6a21\\u578b\\u5207\\u6362\\u5931\\u8d25";'
JS_BLOCK = (
    b"\n"
    b"  // Model-comparison panel strings. Escaped like the block above.\n"
    b'  words.modelCompare = "\\u6a21\\u578b\\u5bf9\\u6bd4";\n'
    b'  words.inUse = "\\u5f53\\u524d\\u4f7f\\u7528";\n'
    b'  words.switchTo = "\\u5207\\u6362\\u5230\\u6b64\\u6a21\\u578b";\n'
    b'  words.notInstalledNote = "\\u672a\\u4e0b\\u8f7d\\u5b8c\\u6210\\uff0c\\u6682\\u4e0d\\u53ef\\u7528\\u3002";'
)

CSS_ANCHOR = b"/* --- global tool permission"
CSS_BLOCK = b"""/* --- model comparison ------------------------------------------------------ */

.local-model-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(258px, 1fr)); gap: 14px; }
.local-model-card {
  display: flex;
  flex-direction: column;
  gap: 11px;
  padding: 15px 16px;
  border: 1px solid var(--border, #d8ded9);
  border-radius: 14px;
  background: var(--background, #fff);
}
.local-model-card[data-active="true"] { border-color: #479c76; box-shadow: 0 0 0 1px #479c76 inset; }
.local-model-card-head { display: flex; align-items: flex-start; gap: 9px; }
.local-model-card-title { display: flex; flex-direction: column; gap: 2px; min-width: 0; margin-right: auto; }
.local-model-card-title strong { font-size: 15px; font-weight: 650; }
.local-model-card-title span { color: var(--muted-foreground, #717973); font-size: 12px; }
.local-model-badge {
  padding: 3px 9px;
  border-radius: 999px;
  background: #479c76;
  color: #fff;
  font-size: 11px;
  white-space: nowrap;
}
.local-model-card-size { font-size: 12px; color: var(--muted-foreground, #717973); font-variant-numeric: tabular-nums; }

.local-model-bars { display: flex; flex-direction: column; gap: 5px; }
.local-model-bar { display: grid; grid-template-columns: 3.4em minmax(0, 1fr); align-items: center; gap: 8px; font-size: 12px; }
.local-model-bar > span { color: var(--muted-foreground, #717973); }
.local-model-bar-track { height: 6px; border-radius: 3px; background: var(--border, #e6eae7); overflow: hidden; }
.local-model-bar-track > i { display: block; height: 100%; background: #479c76; border-radius: 3px; }

.local-model-card .local-model-tradeoff-group h3 { font-size: 11.5px; }
.local-model-card .local-model-tradeoff-group ul { margin: 0; padding-left: 17px; }
.local-model-card .local-model-tradeoff-group li { font-size: 12.5px; line-height: 1.6; }
.local-model-use {
  margin-top: auto;
  padding: 8px 13px;
  border: 1px solid #479c76;
  border-radius: 9px;
  background: transparent;
  color: #2f7a58;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
.local-model-use:hover { background: #479c76; color: #fff; }
.local-model-note { margin: 0; font-size: 12.5px; color: #b57b28; }

"""


def apply(data, anchor, replacement, label):
    found = data.count(anchor)
    if found != 1:
        raise SystemExit(f"{label}: anchor found {found} times, expected 1")
    return data.replace(anchor, replacement, 1)


def main():
    js = JS.read_bytes()
    if b"words.modelCompare = " in js:
        print("JS strings already present")
    else:
        js = apply(js, JS_ANCHOR, JS_ANCHOR + JS_BLOCK, "js words")
        JS.write_bytes(js)
        print(f"JS strings inserted -> {len(js)} bytes")

    css = CSS.read_bytes()
    if b".local-model-cards" in css:
        print("CSS styles already present")
    else:
        css = apply(css, CSS_ANCHOR, CSS_BLOCK + CSS_ANCHOR, "css styles")
        CSS.write_bytes(css)
        print(f"CSS styles inserted -> {len(css)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
